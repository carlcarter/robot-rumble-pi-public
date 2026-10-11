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

    # DeveloperName of the Case record type robot cases should use. The dashboard
    # layout, Lightning record page and compact layout in the org are all keyed to
    # this record type, so cases must carry it to pick those up.
    ROBOT_RECORD_TYPE = "Robot"

    def __init__(self):
        self._last_case_raised_at = 0.0
        self._min_case_interval = 5.0  # Debounce: at most one case per 5s.
        self._robot_record_type_id: Optional[str] = None  # resolved lazily, then cached

    def _get_robot_record_type_id(self) -> Optional[str]:
        """
        Resolve the Robot Case record type Id by DeveloperName and cache it.

        Queried (never hardcoded) so the same code works across orgs. Returns None
        if the record type isn't found, in which case the case is created without
        one rather than failing — the org default applies and we don't block the demo.
        """
        if self._robot_record_type_id is not None:
            return self._robot_record_type_id

        auth = get_auth()
        query = (
            "SELECT Id FROM RecordType "
            "WHERE SobjectType = 'Case' "
            f"AND DeveloperName = '{self.ROBOT_RECORD_TYPE}' LIMIT 1"
        )
        path = f"/services/data/{self.API_VERSION}/query?q={query}"
        try:
            resp = auth.call_salesforce_api("GET", path)
            records = resp.json().get("records", [])
            if records:
                self._robot_record_type_id = records[0]["Id"]
        except Exception:
            # Don't let record-type resolution take down case creation on the day.
            self._robot_record_type_id = None
        return self._robot_record_type_id

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
            robot_id: e.g. "EDI" or "LON"
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
            "Robot_ID__c": robot_id,
        }

        # Stamp the Robot record type so the case picks up the Robot Rumble layout,
        # record page and compact layout. Skipped silently if it can't be resolved.
        record_type_id = self._get_robot_record_type_id()
        if record_type_id:
            body["RecordTypeId"] = record_type_id

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
