"""
Salesforce OAuth 2.0 client credentials flow authentication.

Handles token requests, caching, and refresh. Works for both Salesforce platform
and Data Cloud Ingestion API.
"""

import os
import time
import requests
from typing import Tuple


class SalesforceAuth:
    """
    OAuth 2.0 client credentials flow for headless auth (no user interaction).
    Caches the token and refreshes on 401.
    """

    def __init__(self, my_domain: str, client_id: str, client_secret: str):
        """
        Args:
            my_domain: e.g. "mycompany" (from mycompany.my.salesforce.com)
            client_id: from External Client App
            client_secret: from External Client App
        """
        self.my_domain = my_domain
        self.client_id = client_id
        self.client_secret = client_secret
        self._sf_token_cache: dict = {}
        self._dc_token_cache: dict = {}

    def _token_expired(self, cache: dict) -> bool:
        """Check if cached token is stale (within 60s of expiry)."""
        if not cache or "expires_at" not in cache:
            return True
        return time.time() > cache["expires_at"] - 60

    def get_salesforce_token(self, force_refresh: bool = False) -> str:
        """
        Get a fresh Salesforce access token via client credentials flow.

        Args:
            force_refresh: if True, bypass cache and request a new token.

        Returns:
            Bearer token string (without "Bearer " prefix).

        Raises:
            requests.RequestException: if the token request fails.
        """
        if not force_refresh and not self._token_expired(self._sf_token_cache):
            return self._sf_token_cache["access_token"]

        url = f"https://{self.my_domain}.my.salesforce.com/services/oauth2/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        resp = requests.post(url, data=data, timeout=10)
        resp.raise_for_status()
        result = resp.json()

        # Cache with expiry time.
        self._sf_token_cache = {
            "access_token": result["access_token"],
            "expires_at": time.time() + result.get("expires_in", 3600),
        }

        return self._sf_token_cache["access_token"]

    def get_datacloud_token(self, force_refresh: bool = False) -> Tuple[str, str]:
        """
        Swap a Salesforce token for a Data Cloud (Ingestion API) token.

        First gets a fresh Salesforce token, then exchanges it for a Data Cloud token
        at the /services/a360/token endpoint.

        Args:
            force_refresh: if True, always request a new Data Cloud token.

        Returns:
            (access_token, instance_url) tuple for Data Cloud Ingestion API calls.

        Raises:
            requests.RequestException: if either token request fails.
        """
        if not force_refresh and not self._token_expired(self._dc_token_cache):
            return (
                self._dc_token_cache["access_token"],
                self._dc_token_cache["instance_url"],
            )

        # Step 1: get Salesforce token
        sf_token = self.get_salesforce_token()

        # Step 2: exchange for Data Cloud token
        url = f"https://{self.my_domain}.my.salesforce.com/services/a360/token"
        data = {
            "grant_type": "urn:salesforce:grant-type:external:cdp",
            "subject_token": sf_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        }

        resp = requests.post(url, data=data, timeout=10)
        resp.raise_for_status()
        result = resp.json()

        # Cache with expiry time.
        self._dc_token_cache = {
            "access_token": result["access_token"],
            "instance_url": result["instance_url"],
            "expires_at": time.time() + result.get("expires_in", 7200),
        }

        return (
            self._dc_token_cache["access_token"],
            self._dc_token_cache["instance_url"],
        )

    def call_salesforce_api(
        self,
        method: str,
        path: str,
        json_body: dict = None,
        force_refresh: bool = False,
    ) -> requests.Response:
        """
        Make an authenticated call to the Salesforce REST API.

        Args:
            method: "GET", "POST", "PATCH", etc.
            path: e.g. "/services/data/v67.0/sobjects/Case"
            json_body: request body (for POST/PATCH)
            force_refresh: if True, refresh token on 401

        Returns:
            requests.Response object.

        Raises:
            requests.RequestException: if the call fails and token refresh didn't help.
        """
        token = self.get_salesforce_token()
        url = f"https://{self.my_domain}.my.salesforce.com{path}"
        headers = {"Authorization": f"Bearer {token}"}

        resp = requests.request(method, url, json=json_body, headers=headers, timeout=10)

        # On 401, refresh token and retry once.
        if resp.status_code == 401 and not force_refresh:
            token = self.get_salesforce_token(force_refresh=True)
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.request(
                method, url, json=json_body, headers=headers, timeout=10
            )

        resp.raise_for_status()
        return resp


# Module-level convenience instance (singleton pattern).
_auth: SalesforceAuth = None


def init_auth(my_domain: str, client_id: str, client_secret: str) -> None:
    """Initialize the global auth instance. Call this once at startup."""
    global _auth
    _auth = SalesforceAuth(my_domain, client_id, client_secret)


def get_auth() -> SalesforceAuth:
    """Get the global auth instance."""
    if _auth is None:
        raise RuntimeError("Auth not initialized. Call init_auth() first.")
    return _auth
