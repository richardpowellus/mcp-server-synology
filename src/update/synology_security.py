# src/update/synology_security.py - read-only security posture of a DSM NAS
#
# Each call below was run against an RS1221+ on DSM 7.3.2-86009 Update 5 on
# 2026-10-29. The Security Advisor calls are the ones its UI makes
# (SYNO.Core.SecurityScan.Status system_get / rule_get); none of these APIs has
# a published guide. Nothing here changes a setting.

import json
from typing import Any, Callable, Dict, List, Optional

from utils.synology_api import SynologyAPIClient

# Security Advisor rule statuses that are not findings.
_NOT_A_FINDING = frozenset({"pass", "skip"})


def _data(result: Dict[str, Any]) -> Dict[str, Any]:
    data = result.get("data")
    return data if isinstance(data, dict) else {}


class SynologySecurity:
    """Read-only security posture: exposure, lockout, accounts, 2FA, Security Advisor."""

    def __init__(
        self,
        base_url: str,
        session_id: str,
        verify_ssl: bool = False,
        syno_token: Optional[str] = None,
    ):
        self._api = SynologyAPIClient(
            base_url, session_id, verify_ssl, syno_token=syno_token
        )

    def _get(self, api: str, method: str, version: int = 1, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        extra = {k: json.dumps(v) for k, v in (params or {}).items()}
        return self._api.get(api, method, version, extra)

    def posture(self) -> Dict[str, Any]:
        """One read of every setting below; a failed section is reported, not hidden."""
        errors: List[Dict[str, Any]] = []

        def section(name: str, result: Dict[str, Any], shape: Callable[[Dict[str, Any]], Any]) -> Any:
            if not result.get("success"):
                errors.append({"section": name, "error": result.get("error")})
                return None
            return shape(_data(result))

        quickconnect = section(
            "quickconnect",
            self._get("SYNO.Core.QuickConnect", "get"),
            lambda d: {"enabled": d.get("enabled"), "server_alias": d.get("server_alias")},
        )
        auto_block = section(
            "auto_block",
            self._get("SYNO.Core.Security.AutoBlock", "get"),
            lambda d: {
                "enabled": d.get("enable"),
                "attempts": d.get("attempts"),
                "within_minutes": d.get("within_mins"),
                "expire_days": d.get("expire_day"),
            },
        )
        firewall = section(
            "firewall",
            self._get("SYNO.Core.Security.Firewall", "get"),
            lambda d: {"enabled": d.get("enable_firewall"), "profile": d.get("profile_name")},
        )
        two_factor = section(
            "two_factor",
            self._get("SYNO.Core.OTP.EnforcePolicy", "get"),
            lambda d: {"enforced_for": d.get("otp_enforce_option")},
        )

        def accounts(d: Dict[str, Any]) -> Dict[str, Any]:
            # `expired` is "now" for a disabled account and "normal" for an
            # enabled one; anything else is passed through as DSM gave it.
            by_name = {u.get("name"): u.get("expired") for u in d.get("users") or []}
            return {
                "admin_disabled": by_name.get("admin") == "now" if "admin" in by_name else None,
                "guest_disabled": by_name.get("guest") == "now" if "guest" in by_name else None,
                "enabled_users": sorted(n for n, e in by_name.items() if e != "now"),
            }

        users = section(
            "accounts",
            self._get("SYNO.Core.User", "list", 1, {"additional": ["expired"], "offset": 0, "limit": -1}),
            accounts,
        )
        administrators = section(
            "administrators",
            self._get("SYNO.Core.Group.Member", "list", 1, {"group": "administrators"}),
            lambda d: sorted(u.get("name") for u in d.get("users") or []),
        )

        def advisor_summary(d: Dict[str, Any]) -> Dict[str, Any]:
            items = d.get("items") or {}
            return {
                "overall": d.get("sysStatus"),
                "last_scan_time": d.get("lastScanTime"),
                "categories": {
                    name: {"severity": item.get("failSeverity"), "fail": item.get("fail")}
                    for name, item in items.items()
                },
            }

        advisor = section(
            "security_advisor",
            self._get("SYNO.Core.SecurityScan.Status", "system_get"),
            advisor_summary,
        )

        def findings(d: Dict[str, Any]) -> List[Dict[str, Any]]:
            return sorted(
                (
                    {
                        "id": rule.get("id"),
                        "category": rule.get("category"),
                        "severity": rule.get("severity"),
                        "status": rule.get("status"),
                        "str_id": rule.get("strId"),
                    }
                    for rule in (d.get("items") or {}).values()
                    if rule.get("status") not in _NOT_A_FINDING
                ),
                key=lambda r: (r["category"] or "", r["id"] or ""),
            )

        advisor_findings = section(
            "security_advisor_findings",
            self._get("SYNO.Core.SecurityScan.Status", "rule_get", 1, {"items": "ALL"}),
            findings,
        )
        schedule = section(
            "security_advisor_schedule",
            self._get("SYNO.Core.SecurityScan.Conf", "get"),
            lambda d: {
                "enabled": d.get("enableSchedule"),
                "weekday": d.get("weekday"),
                "hour": d.get("hour"),
                "minute": d.get("minute"),
            },
        )
        if isinstance(advisor, dict):
            advisor["findings"] = advisor_findings
            advisor["schedule"] = schedule

        return {
            "success": not errors,
            "quickconnect": quickconnect,
            "auto_block": auto_block,
            "firewall": firewall,
            "two_factor": two_factor,
            "accounts": users,
            "administrators": administrators,
            "security_advisor": advisor,
            "errors": errors,
        }
