"""
Service Cloud operations: create and query Cases.

The robot raises a Case when it crashes/stalls. This module handles the REST calls.
"""

import time
from typing import Optional
from sf_auth import get_auth


class CaseManager:
    """Service Cloud Case operations."""

    API_VERSION = "v67.0"

    def __init__(self):
        self._last_case_raised_at = 0.0
        self._min_case_interval = 5.0  # Debounce: at most one case per 5s.

    def create_case(
        self,
        robot_id: str,
        description: str,
        hazard: Optional[str] = None,
        heat_number: int = 0,
    ) -> str:
        """
        Create a Case in Salesforce.

        Args:
            robot_id: e.g. "EDI" or "LDN"
            description: what went wrong (e.g. "IMU deceleration spike; no forward progress for 3s")
            hazard: the active hazard name, if any (for the leaderboard metric "Fastest Fixers")
            heat_number: which heat this occurred in

        Returns:
            Case ID on success.

        Raises:
            RuntimeError: if debounce is active (too soon after last case)
            requests.RequestException: if the API call fails
        """
        now = time.time()
        if now - self._last_case_raised_at < self._min_case_interval:
            raise RuntimeError(
                f"Debounce active: case raised {now - self._last_case_raised_at:.1f}s ago"
            )

        auth = get_auth()
        path = f"/services/data/{self.API_VERSION}/sobjects/Case"

        body = {
            "Subject": f"RVR-{robot_id}: crash/stall detected",
            "Description": description,
            "Origin": "Robot",
            "Priority": "High",
            "Status": "New",
        }

        # Optional custom fields for metrics (adjust field names to your org).
        # These would be part of the "Fastest Fixers" leaderboard calculation.
        if hazard:
            body["Active_Hazard__c"] = hazard
        if heat_number:
            body["Heat_Number__c"] = heat_number

        resp = auth.call_salesforce_api("POST", path, json_body=body)
        result = resp.json()
        case_id = result.get("id")

        self._last_case_raised_at = now
        return case_id

    def get_cases_for_robot(self, robot_id: str, hours: int = 1) -> list:
        """
        Query recent Cases for a robot (for the leaderboard or diagnostics).

        Args:
            robot_id: e.g. "EDI"
            hours: look back this many hours

        Returns:
            List of Case records (dicts).
        """
        auth = get_auth()
        # Adjust the date offset for your org's timezone if needed.
        since = f"LAST_N_HOURS:{hours}"
        query = f"""
            SELECT Id, Subject, CreatedDate, Status, Description
            FROM Case
            WHERE Origin = 'Robot' AND Subject LIKE 'RVR-{robot_id}%'
            AND CreatedDate = {since}
            ORDER BY CreatedDate DESC
        """
        path = f"/services/data/{self.API_VERSION}/query"
        resp = auth.call_salesforce_api("GET", path + f"?q={query}")
        result = resp.json()
        return result.get("records", [])


# Module-level singleton.
_case_manager: CaseManager = None


def init_cases() -> None:
    """Initialize the CaseManager. Call once at startup."""
    global _case_manager
    _case_manager = CaseManager()


def get_case_manager() -> CaseManager:
    """Get the global CaseManager instance."""
    if _case_manager is None:
        raise RuntimeError("CaseManager not initialized. Call init_cases() first.")
    return _case_manager
