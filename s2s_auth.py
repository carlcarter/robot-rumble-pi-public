"""
Authentication for the Data Cloud Server-to-Server (S2S) events endpoint.

The S2S connector needs an External Client App using the OAuth 2.0 JWT Bearer flow
(a signed assertion, no client secret). This is separate from sf_auth.py, which keeps
using client credentials for Service Cloud and the batch Ingestion API.

Flow:
1. Sign a short-lived JWT with the private key (RS256).
2. POST it to the My Domain token endpoint -> Salesforce access token.
3. Swap that for a Data Cloud token at /services/a360/token -> (token, tenant host).
"""

import time
import logging
from pathlib import Path
from typing import Tuple

import jwt
import requests


logger = logging.getLogger(__name__)

JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# The JWT bearer response has no expires_in, so assume a conservative session length.
# A 401 forces a refresh regardless.
DEFAULT_SF_TOKEN_TTL_S = 1800
DEFAULT_DC_TOKEN_TTL_S = 7000


class S2SAuth:
    """JWT bearer auth + Data Cloud token exchange, with caching."""

    def __init__(
        self,
        my_domain: str,
        client_id: str,
        username: str,
        private_key_path: str,
    ):
        """
        Args:
            my_domain: e.g. "mycompany" (from mycompany.my.salesforce.com)
            client_id: Consumer Key of the S2S External Client App
            username: Salesforce user the JWT is issued for (the JWT "sub" claim).
                Must be pre-authorised on the app (or the app set to self-authorise).
            private_key_path: path to the PKCS#8 private key (private.key). Kept outside
                the repo; never commit it.
        """
        self.my_domain = my_domain
        self.client_id = client_id
        self.username = username
        self.private_key_path = Path(private_key_path).expanduser()
        self._sf_token_cache: dict = {}
        self._dc_token_cache: dict = {}

    @property
    def _base_url(self) -> str:
        return f"https://{self.my_domain}.my.salesforce.com"

    @staticmethod
    def _expired(cache: dict) -> bool:
        """True if there's no cached token or it's within 60s of expiry."""
        if not cache or "expires_at" not in cache:
            return True
        return time.time() > cache["expires_at"] - 60

    def _build_assertion(self) -> str:
        """Create the signed JWT. Reads the key fresh so a rotated key is picked up."""
        private_key = self.private_key_path.read_text()
        now = int(time.time())
        claims = {
            "iss": self.client_id,
            "sub": self.username,
            # Audience is the My Domain URL, which also works for sandboxes/scratch orgs.
            "aud": self._base_url,
            "exp": now + 180,
        }
        return jwt.encode(claims, private_key, algorithm="RS256")

    def get_salesforce_token(self, force_refresh: bool = False) -> str:
        """Get a Salesforce access token via the JWT bearer flow."""
        if not force_refresh and not self._expired(self._sf_token_cache):
            return self._sf_token_cache["access_token"]

        resp = requests.post(
            f"{self._base_url}/services/oauth2/token",
            data={"grant_type": JWT_BEARER_GRANT, "assertion": self._build_assertion()},
            timeout=10,
        )
        if not resp.ok:
            # The error body ({"error": ..., "error_description": ...}) is what tells you
            # whether it's a bad cert, an unauthorised user, or an audience mismatch.
            logger.error(f"JWT bearer token request failed: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        result = resp.json()

        self._sf_token_cache = {
            "access_token": result["access_token"],
            "expires_at": time.time() + result.get("expires_in", DEFAULT_SF_TOKEN_TTL_S),
        }
        return self._sf_token_cache["access_token"]

    def get_datacloud_token(self, force_refresh: bool = False) -> Tuple[str, str]:
        """
        Swap the Salesforce token for a Data Cloud token.

        Returns:
            (access_token, instance_host). instance_host is the bare tenant host with
            no scheme, matching how datacloud.py builds its URLs.
        """
        if not force_refresh and not self._expired(self._dc_token_cache):
            return (
                self._dc_token_cache["access_token"],
                self._dc_token_cache["instance_host"],
            )

        sf_token = self.get_salesforce_token(force_refresh=force_refresh)

        resp = requests.post(
            f"{self._base_url}/services/a360/token",
            data={
                "grant_type": "urn:salesforce:grant-type:external:cdp",
                "subject_token": sf_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            },
            timeout=10,
        )
        if not resp.ok:
            logger.error(f"Data Cloud token exchange failed: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        result = resp.json()

        # Docs show instance_url with a scheme; this org returns a bare host. Accept both.
        host = result["instance_url"].removeprefix("https://").rstrip("/")

        self._dc_token_cache = {
            "access_token": result["access_token"],
            "instance_host": host,
            "expires_at": time.time() + result.get("expires_in", DEFAULT_DC_TOKEN_TTL_S),
        }
        return self._dc_token_cache["access_token"], host


# Module-level singleton, matching sf_auth.py.
_s2s_auth: S2SAuth = None


def init_s2s_auth(my_domain: str, client_id: str, username: str, private_key_path: str) -> None:
    """Initialise the global S2S auth instance. Call once at startup."""
    global _s2s_auth
    _s2s_auth = S2SAuth(my_domain, client_id, username, private_key_path)


def get_s2s_auth() -> S2SAuth:
    """Get the global S2S auth instance."""
    if _s2s_auth is None:
        raise RuntimeError("S2S auth not initialised. Call init_s2s_auth() first.")
    return _s2s_auth
