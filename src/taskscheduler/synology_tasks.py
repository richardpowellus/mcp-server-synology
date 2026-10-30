# src/taskscheduler/synology_tasks.py - Control Panel > Task Scheduler:
# user-defined script tasks, scheduled or triggered at boot-up/shutdown
#
# None of these APIs has a published guide. Every call below is the one DSM
# 7.4.1-90080's own UI makes, read from the JavaScript the NAS serves
# (webman/modules/AdminCenter/admin_center.js and
# webman/modules/TaskSchedulerUtils/TaskSchedulerUtils.js), and the response
# shapes were read from an RS1221+ on that build on 2026-10-29.
#
# DSM keeps two kinds of task behind one grid:
#
#   Triggered ("event_script" in the list; boot-up or shutdown)
#     SYNO.Core.EventScheduler v1, keyed by task_name:
#       list / get / create / set / delete / run / set_enable,
#       result_list {task_name} and result_get_file {task_name, result_id}.
#     A root-owned task is written through SYNO.Core.EventScheduler.Root.
#     owner is {"<uid>": "<name>"}, root being {"0": "root"}; the script is
#     `operation` with operation_type "script".
#
#   Scheduled ("script" in the list)
#     SYNO.Core.TaskScheduler, keyed by numeric id plus real_owner:
#       list v3, get v4 (id -1 plus type returns a new task's template),
#       create / set v4, run / delete v2 {tasks: [{id, real_owner}]},
#       get_history_status_list / get_history_log v1.
#     A root-owned task is written through SYNO.Core.TaskScheduler.Root.
#
# Writing a root-owned script task needs the caller's password re-entered:
# the UI opens its password dialog, calls SYNO.Core.User.PasswordConfirm auth
# v2 (v1 succeeds and returns no token), and sends the returned
# SynoConfirmPWToken with the create/set. This module does the same with the
# configured account's password, fetched per write and never returned.
#
# A task's stdout/stderr is kept only when Task Scheduler's "Save output
# results" setting is on (EventScheduler config_get/config_set, type
# "esynoscheduler", output_path relative to the volume root like "docker/logs").
# With it off, result_get_file answers 117 and there is nothing to read.

import json
import time
from typing import Any, Callable, Dict, List, Optional

from utils.synology_api import SynologyAPIClient

EVENT_TRIGGERS = ("bootup", "shutdown")
TRIGGERS = EVENT_TRIGGERS + ("schedule",)

# The list's `type` for the two kinds this module writes.
_EVENT_TYPE = "event_script"
_SCHEDULED_TYPE = "script"

_OUTPUT_CONFIG_TYPE = "esynoscheduler"

# schedule.repeat -> (date_type, repeat_date), from EditSchedulePanelV2.getData.
_REPEAT = {"daily": (0, 1001), "weekly": (0, 1002), "once": (1, 0)}

_PAGE = 50
_POLL_SECONDS = 2


def _data(result: Dict[str, Any]) -> Any:
    return result.get("data")


def _failed(step: str, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"success": False, "step": step, "error": result.get("error")}


def _refused(code: str, message: str) -> Dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": message}}


class SynologyTaskScheduler:
    """List, create/update, run, read results of and delete Task Scheduler tasks."""

    def __init__(
        self,
        base_url: str,
        session_id: str,
        verify_ssl: bool = False,
        syno_token: Optional[str] = None,
        password_provider: Optional[Callable[[], Optional[str]]] = None,
        username: str = "",
    ):
        self._api = SynologyAPIClient(base_url, session_id, verify_ssl, syno_token=syno_token)
        self._password_provider = password_provider
        # real_owner of a scheduled task: the UI sends the logged-in user
        # (_S("user")) when a grid row does not carry one.
        self._username = username

    def _real_owner(self, row: Dict[str, Any]) -> str:
        return row.get("real_owner") or self._username

    def _get(self, api: str, method: str, version: int = 1, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        extra = {k: json.dumps(v) for k, v in (params or {}).items()}
        return self._api.get(api, method, version, extra)

    def _post(self, api: str, method: str, version: int, params: Dict[str, Any]) -> Dict[str, Any]:
        extra = {k: json.dumps(v) for k, v in params.items()}
        return self._api.post(api, method, version, extra)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _grid(self) -> Dict[str, Any]:
        """Every task in the Task Scheduler grid, paging as the UI does."""
        tasks: List[Dict[str, Any]] = []
        offset = 0
        while True:
            result = self._get("SYNO.Core.TaskScheduler", "list", 3, {"offset": offset, "limit": _PAGE})
            if not result.get("success"):
                return result
            data = _data(result) or {}
            page = data.get("tasks") or []
            tasks.extend(page)
            offset += len(page)
            if not page or offset >= (data.get("total") or 0):
                return {"success": True, "data": tasks}

    def _event_get(self, name: str) -> Dict[str, Any]:
        return self._get("SYNO.Core.EventScheduler", "get", 1, {"task_name": name})

    def _scheduled_get(self, task_id: int, real_owner: str) -> Dict[str, Any]:
        return self._get("SYNO.Core.TaskScheduler", "get", 4, {"id": task_id, "real_owner": real_owner})

    @staticmethod
    def _shape_event(row: Dict[str, Any], d: Dict[str, Any]) -> Dict[str, Any]:
        owner = d.get("owner") or {}
        return {
            "name": d.get("task_name", row.get("name")),
            "id": row.get("id"),
            "kind": "triggered",
            "trigger": d.get("event"),
            "owner": next(iter(owner.values()), None) if isinstance(owner, dict) else owner,
            "enabled": d.get("enable"),
            "script": d.get("operation") if d.get("operation_type") == "script" else None,
            "operation_type": d.get("operation_type"),
            "depends_on": d.get("depend_on_task") or "",
            "notify": {"enabled": d.get("notify_enable"), "mail": d.get("notify_mail"), "only_on_error": d.get("notify_if_error")},
            "last_run": {
                "start": d.get("last_start_time"),
                "stop": d.get("last_stop_time"),
                "exit": d.get("last_exit_info"),
            },
            "running": bool((d.get("status") or {}).get("running")),
        }

    @staticmethod
    def _shape_scheduled(row: Dict[str, Any], d: Dict[str, Any]) -> Dict[str, Any]:
        extra = d.get("extra") or {}
        return {
            "name": d.get("name", row.get("name")),
            "id": d.get("id", row.get("id")),
            "kind": "scheduled",
            "trigger": "schedule",
            "owner": d.get("owner", row.get("owner")),
            "real_owner": d.get("real_owner", row.get("real_owner")),
            "enabled": d.get("enable", row.get("enable")),
            "script": extra.get("script"),
            "notify": {"enabled": extra.get("notify_enable"), "mail": extra.get("notify_mail"), "only_on_error": extra.get("notify_if_error")},
            "schedule": d.get("schedule"),
            "next_trigger_time": row.get("next_trigger_time"),
        }

    @staticmethod
    def _shape_other(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": row.get("name"),
            "id": row.get("id"),
            "kind": row.get("type"),
            "action": row.get("action"),
            "owner": row.get("owner"),
            "enabled": row.get("enable"),
            "next_trigger_time": row.get("next_trigger_time"),
        }

    def _describe(self, row: Dict[str, Any]) -> Dict[str, Any]:
        kind = row.get("type")
        if kind == _EVENT_TYPE:
            result = self._event_get(row["name"])
            if not result.get("success"):
                return _failed(f"read triggered task {row['name']!r}", result)
            return {"success": True, "data": self._shape_event(row, _data(result) or {})}
        if kind == _SCHEDULED_TYPE:
            result = self._scheduled_get(row["id"], self._real_owner(row))
            if not result.get("success"):
                return _failed(f"read scheduled task {row['name']!r}", result)
            return {"success": True, "data": self._shape_scheduled(row, _data(result) or {})}
        return {"success": True, "data": self._shape_other(row)}

    def _find(self, name: str, task_id: Optional[int] = None) -> Dict[str, Any]:
        """The one grid row named `name` (and `task_id`, if given)."""
        grid = self._grid()
        if not grid.get("success"):
            return _failed("list tasks", grid)
        rows = [r for r in grid["data"] if r.get("name") == name and (task_id is None or r.get("id") == task_id)]
        if not rows:
            return {"success": True, "data": None}
        if len(rows) > 1:
            return _refused(
                "ambiguous_name",
                f"{len(rows)} tasks are named {name!r} (ids {[r.get('id') for r in rows]}); pass id.",
            )
        return {"success": True, "data": rows[0]}

    def output_config(self) -> Dict[str, Any]:
        result = self._get("SYNO.Core.EventScheduler", "config_get", 1, {"type": _OUTPUT_CONFIG_TYPE})
        if not result.get("success"):
            return _failed("read output setting", result)
        d = _data(result) or {}
        return {"success": True, "data": {"save_output": bool(d.get("enable_output")), "output_path": d.get("output_path") or ""}}

    def list_tasks(self, name: Optional[str] = None) -> Dict[str, Any]:
        grid = self._grid()
        if not grid.get("success"):
            return _failed("list tasks", grid)
        tasks = []
        for row in grid["data"]:
            if name is not None and row.get("name") != name:
                continue
            described = self._describe(row)
            if not described.get("success"):
                return described
            tasks.append(described["data"])
        output = self.output_config()
        return {
            "success": True,
            "tasks": tasks,
            "output_saving": output.get("data") if output.get("success") else {"error": output.get("error")},
        }

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _confirm_token(self) -> Dict[str, Any]:
        """SynoConfirmPWToken for a root-owned script, as the UI's password dialog gets it."""
        password = self._password_provider() if self._password_provider else None
        if not password:
            return _refused(
                "password_unavailable",
                "Writing a root-owned script task needs the account password re-confirmed, and none is configured.",
            )
        result = self._post("SYNO.Core.User.PasswordConfirm", "auth", 2, {"password": password})
        token = (_data(result) or {}).get("SynoConfirmPWToken") if result.get("success") else None
        if not token:
            return {"success": False, "step": "confirm password", "error": result.get("error") or {"code": "no_token"}}
        return {"success": True, "token": token}

    def _uid(self, user: str) -> Dict[str, Any]:
        if user == "root":
            return {"success": True, "uid": 0}
        result = self._get("SYNO.Core.User", "list", 1, {"additional": ["uid"], "offset": 0, "limit": -1})
        if not result.get("success"):
            return _failed("list users", result)
        for u in (_data(result) or {}).get("users") or []:
            if u.get("name") == user and isinstance(u.get("uid"), int):
                return {"success": True, "uid": u["uid"]}
        return _refused("no_such_user", f"No DSM user named {user!r}.")

    @staticmethod
    def _schedule(template: Dict[str, Any], schedule: Dict[str, Any]) -> Dict[str, Any]:
        repeat = schedule.get("repeat", "daily")
        if repeat not in _REPEAT:
            raise ValueError(f"schedule.repeat must be one of {sorted(_REPEAT)}")
        hour, minute = schedule.get("hour", 0), schedule.get("minute", 0)
        if not (isinstance(hour, int) and 0 <= hour <= 23 and isinstance(minute, int) and 0 <= minute <= 59):
            raise ValueError("schedule.hour must be 0-23 and schedule.minute 0-59")
        date_type, repeat_date = _REPEAT[repeat]
        out = dict(template)
        out.update(date_type=date_type, repeat_date=repeat_date, hour=hour, minute=minute,
                   repeat_hour=0, repeat_min=0, last_work_hour=hour, monthly_week=[])
        if repeat == "once":
            date = schedule.get("date")
            if not isinstance(date, str) or date.count("/") != 2:
                raise ValueError("schedule.date (YYYY/M/D) is required when repeat is 'once'")
            out["date"] = date
        else:
            days = schedule.get("week_days", list(range(7)))
            if not days or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
                raise ValueError("schedule.week_days must be a non-empty list of 0 (Sunday) to 6")
            out["week_day"] = ",".join(str(d) for d in sorted(set(days)))
        for key in ("repeat_hour_store_config", "repeat_min_store_config"):
            if out.get(key) is None:
                out[key] = []
        return out

    def set_script_task(
        self,
        name: str,
        script: str,
        trigger: str,
        owner: str = "root",
        enable: bool = True,
        schedule: Optional[Dict[str, Any]] = None,
        notify_mail: Optional[str] = None,
        notify_only_on_error: bool = False,
        task_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create the task, or update the one already named `name`."""
        if trigger not in TRIGGERS:
            return _refused("invalid_argument", f"trigger must be one of {list(TRIGGERS)}")
        if not isinstance(script, str) or not script.strip():
            return _refused("invalid_argument", "script must be a non-empty string")
        if len(script) > 8192:
            return _refused("invalid_argument", "script is longer than the 8192 characters DSM's form allows")
        if trigger == "schedule" and not schedule:
            return _refused("invalid_argument", "a scheduled task needs schedule")
        if trigger != "schedule" and schedule:
            return _refused("invalid_argument", "schedule applies only to trigger 'schedule'")

        found = self._find(name, task_id)
        if not found.get("success"):
            return found
        row = found["data"]
        want_type = _SCHEDULED_TYPE if trigger == "schedule" else _EVENT_TYPE
        if row is not None and row.get("type") != want_type:
            return _refused(
                "kind_mismatch",
                f"{name!r} exists as a {row.get('type')} task; DSM cannot turn it into a {want_type} task in place. Delete it first.",
            )
        before = None
        if row is not None:
            described = self._describe(row)
            if not described.get("success"):
                return described
            before = described["data"]

        token = None
        if owner == "root":
            confirmed = self._confirm_token()
            if not confirmed.get("success"):
                return confirmed
            token = confirmed["token"]

        notify = {
            "notify_enable": bool(notify_mail),
            "notify_mail": notify_mail or "",
            "notify_if_error": bool(notify_mail) and notify_only_on_error,
        }
        if want_type == _EVENT_TYPE:
            written = self._write_event(name, script, trigger, owner, enable, notify, token, before)
        else:
            try:
                written = self._write_scheduled(name, script, owner, enable, schedule, notify, token, row)
            except ValueError as e:
                return _refused("invalid_argument", str(e))
        if not written.get("success"):
            return written

        after = self._find(name, written.get("id"))
        if not after.get("success") or after["data"] is None:
            return {"success": False, "error": {"code": "read_back_missing", "message": "DSM accepted the write but the task is not listed"}, "before": before}
        described = self._describe(after["data"])
        if not described.get("success"):
            return described
        after_task = described["data"]
        mismatch = [k for k, v in (("script", script), ("enabled", enable), ("owner", owner)) if after_task.get(k) != v]
        result = {"success": not mismatch, "action": "updated" if row else "created", "before": before, "after": after_task}
        if mismatch:
            result["error"] = {"code": "read_back_mismatch", "message": f"read-back differs in {mismatch}"}
        return result

    def _write_event(self, name, script, trigger, owner, enable, notify, token, before) -> Dict[str, Any]:
        if before is not None and before.get("trigger") != trigger:
            return _refused("trigger_mismatch", f"{name!r} is a {before.get('trigger')} task; DSM fixes the event once created. Delete it first.")
        uid = self._uid(owner)
        if not uid.get("success"):
            return uid
        params: Dict[str, Any] = {
            "task_name": name,
            "owner": {str(uid["uid"]): owner},
            "event": trigger,
            "enable": enable,
            "depend_on_task": before.get("depends_on", "") if before else "",
            "operation_type": "script",
            "operation": script,
            **notify,
        }
        if token is not None:
            params["SynoConfirmPWToken"] = token
        api = "SYNO.Core.EventScheduler.Root" if uid["uid"] == 0 else "SYNO.Core.EventScheduler"
        result = self._post(api, "set" if before else "create", 1, params)
        if not result.get("success"):
            return _failed("save triggered task", result)
        return {"success": True}

    def _write_scheduled(self, name, script, owner, enable, schedule, notify, token, row) -> Dict[str, Any]:
        if row is None:
            template = self._get("SYNO.Core.TaskScheduler", "get", 4, {"id": -1, "type": _SCHEDULED_TYPE, "real_owner": self._username})
        else:
            template = self._scheduled_get(row["id"], self._real_owner(row))
        if not template.get("success"):
            return _failed("read task template", template)
        t = _data(template) or {}
        params: Dict[str, Any] = {
            "name": name,
            "owner": owner,
            "real_owner": t.get("real_owner") or self._username,
            "enable": enable,
            "schedule": self._schedule(t.get("schedule") or {}, schedule),
            "extra": {**(t.get("extra") or {}), "script": script, **notify},
        }
        if row is None:
            params["type"] = _SCHEDULED_TYPE
        else:
            params["id"] = row["id"]
        if token is not None:
            params["SynoConfirmPWToken"] = token
        api = "SYNO.Core.TaskScheduler.Root" if owner == "root" else "SYNO.Core.TaskScheduler"
        result = self._post(api, "set" if row else "create", 4, params)
        if not result.get("success"):
            return _failed("save scheduled task", result)
        created_id = (_data(result) or {}).get("id") if isinstance(_data(result), dict) else None
        return {"success": True, "id": created_id if row is None else row["id"]}

    def set_output_saving(self, enable: bool, output_path: Optional[str] = None) -> Dict[str, Any]:
        before = self.output_config()
        if not before.get("success"):
            return before
        path = (output_path if output_path is not None else before["data"]["output_path"]).strip().strip("/")
        if enable and not path:
            return _refused("invalid_argument", "Saving output needs output_path, a shared-folder path such as 'docker/task-logs'.")
        result = self._post("SYNO.Core.EventScheduler", "config_set", 1,
                            {"type": _OUTPUT_CONFIG_TYPE, "output_path": path, "enable_output": enable})
        if not result.get("success"):
            return _failed("save output setting", result)
        after = self.output_config()
        if not after.get("success"):
            return after
        ok = after["data"]["save_output"] == enable and (not enable or after["data"]["output_path"] == path)
        out = {"success": ok, "before": before["data"], "after": after["data"]}
        if not ok:
            out["error"] = {"code": "read_back_mismatch", "message": "DSM accepted the change but the read-back differs"}
        return out

    # ------------------------------------------------------------------
    # Results, run, delete
    # ------------------------------------------------------------------

    def _event_results(self, name: str) -> Dict[str, Any]:
        result = self._get("SYNO.Core.EventScheduler", "result_list", 1, {"task_name": name})
        if not result.get("success"):
            return _failed("list results", result)
        rows = _data(result)
        return {"success": True, "data": rows if isinstance(rows, list) else []}

    def _scheduled_results(self, task_id: int) -> Dict[str, Any]:
        result = self._get("SYNO.Core.TaskScheduler", "get_history_status_list", 1, {"id": task_id})
        if not result.get("success"):
            return _failed("list results", result)
        rows = _data(result)
        return {"success": True, "data": rows if isinstance(rows, list) else []}

    def task_result(self, name: str, task_id: Optional[int] = None, result_id: Optional[int] = None) -> Dict[str, Any]:
        """The latest run (or the run `result_id` names), with its output when DSM kept it."""
        found = self._find(name, task_id)
        if not found.get("success"):
            return found
        row = found["data"]
        if row is None:
            return _refused("not_found", f"No task named {name!r}.")
        output = self.output_config()
        saving = output.get("data", {}).get("save_output") if output.get("success") else None

        if row.get("type") == _EVENT_TYPE:
            results = self._event_results(name)
            if not results.get("success"):
                return results
            runs = results["data"]
            if not runs:
                return {"success": True, "task": name, "runs": 0, "result": None, "message": "The task has not run yet."}
            pick = next((r for r in runs if r.get("result_id") == result_id), None) if result_id is not None else max(runs, key=lambda r: r.get("result_id", 0))
            if pick is None:
                return _refused("not_found", f"No result {result_id} for {name!r}.")
            out = {
                "success": True,
                "task": name,
                "runs": len(runs),
                "result": {
                    "result_id": pick.get("result_id"),
                    "trigger": pick.get("trigger_event"),
                    "start": pick.get("start_time"),
                    "stop": pick.get("stop_time"),
                    "exit": pick.get("exit_info"),
                },
            }
            log = self._get("SYNO.Core.EventScheduler", "result_get_file", 1, {"task_name": name, "result_id": pick.get("result_id")})
            if log.get("success"):
                d = _data(log) or {}
                out["result"]["script"] = d.get("script_in")
                out["result"]["output"] = d.get("script_out")
            else:
                out["result"]["output"] = None
                out["output_error"] = log.get("error")
            out["output_saving"] = saving
            if not saving:
                out["message"] = "DSM keeps task output only while 'Save output results' is on (synology_task_output_set); runs before that have none."
            return out

        if row.get("type") == _SCHEDULED_TYPE:
            results = self._scheduled_results(row["id"])
            if not results.get("success"):
                return results
            runs = results["data"]
            if not runs:
                return {"success": True, "task": name, "id": row["id"], "runs": 0, "result": None, "message": "The task has not run yet."}
            pick = next((r for r in runs if r.get("timestamp") == result_id), None) if result_id is not None else max(runs, key=lambda r: r.get("timestamp") or 0)
            if pick is None:
                return _refused("not_found", f"No result with timestamp {result_id} for {name!r}.")
            out = {
                "success": True,
                "task": name,
                "id": row["id"],
                "runs": len(runs),
                "result": {
                    "result_id": pick.get("timestamp"),
                    "start": pick.get("start_time"),
                    "stop": pick.get("stop_time"),
                    "exit": {"exit_code": pick.get("exit_code"), "exit_type": pick.get("exit_type")},
                },
                "output_saving": saving,
            }
            if saving:
                log = self._get("SYNO.Core.TaskScheduler", "get_history_log", 1, {"id": row["id"], "timestamp": pick.get("timestamp")})
                if log.get("success"):
                    d = _data(log) or {}
                    out["result"]["script"] = d.get("script_in")
                    out["result"]["output"] = d.get("script_out")
                else:
                    out["output_error"] = log.get("error")
            else:
                out["message"] = "DSM keeps task output only while 'Save output results' is on (synology_task_output_set)."
            return out

        return _refused("unsupported_task", f"{name!r} is a {row.get('type')} task; only script tasks have results here.")

    def run_task(self, name: str, task_id: Optional[int] = None, wait_seconds: int = 0) -> Dict[str, Any]:
        """Run now. With wait_seconds, wait for the run to finish and return its result."""
        found = self._find(name, task_id)
        if not found.get("success"):
            return found
        row = found["data"]
        if row is None:
            return _refused("not_found", f"No task named {name!r}.")
        if not row.get("can_run", True):
            return _refused("cannot_run", f"DSM does not allow running {name!r} on demand.")
        kind = row.get("type")
        if kind == _EVENT_TYPE:
            prior = self._event_results(name)
            seen = {r.get("result_id") for r in prior.get("data", [])} if prior.get("success") else set()
            result = self._post("SYNO.Core.EventScheduler", "run", 1, {"task_name": name})
        else:
            prior = self._scheduled_results(row["id"])
            seen = {r.get("timestamp") for r in prior.get("data", [])} if prior.get("success") else set()
            result = self._post("SYNO.Core.TaskScheduler", "run", 2, {"tasks": [{"id": row["id"], "real_owner": self._real_owner(row)}]})
        if not result.get("success"):
            return _failed("run task", result)
        out: Dict[str, Any] = {"success": True, "task": name, "started": True}
        if wait_seconds <= 0:
            out["message"] = "Started. Read the outcome with synology_task_result."
            return out
        deadline = time.monotonic() + min(wait_seconds, 300)
        while time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
            runs = self._event_results(name) if kind == _EVENT_TYPE else self._scheduled_results(row["id"])
            if not runs.get("success"):
                continue
            key = "result_id" if kind == _EVENT_TYPE else "timestamp"
            new = [r for r in runs["data"] if r.get(key) not in seen and r.get("stop_time")]
            if new:
                latest = max(new, key=lambda r: r.get(key) or 0)
                finished = self.task_result(name, row.get("id") if kind != _EVENT_TYPE else task_id, latest.get(key))
                out["finished"] = True
                out["result"] = finished.get("result")
                for k in ("output_saving", "message", "output_error"):
                    if k in finished:
                        out[k] = finished[k]
                return out
        out["finished"] = False
        out["message"] = f"Still running (or not yet recorded) after {wait_seconds}s; read it later with synology_task_result."
        return out

    def delete_task(self, name: str, task_id: Optional[int] = None) -> Dict[str, Any]:
        found = self._find(name, task_id)
        if not found.get("success"):
            return found
        row = found["data"]
        if row is None:
            return _refused("not_found", f"No task named {name!r}.")
        if not row.get("can_delete", True):
            return _refused("cannot_delete", f"DSM does not allow deleting {name!r}.")
        described = self._describe(row)
        before = described.get("data") if described.get("success") else self._shape_other(row)
        if row.get("type", "").startswith("event"):
            related = self._get("SYNO.Core.EventScheduler", "list_relate", 1, {"task_name": name, "relate_type": 1})
            dependents = sorted({r.get("task_name") for r in (_data(related) or []) if isinstance(r, dict)}) if related.get("success") else []
            if dependents:
                return _refused("has_dependents", f"{name!r} is depended on by {dependents}; delete or change those first.")
            result = self._post("SYNO.Core.EventScheduler", "delete", 1, {"task_name": name})
        else:
            result = self._post("SYNO.Core.TaskScheduler", "delete", 2, {"tasks": [{"id": row["id"], "real_owner": self._real_owner(row)}]})
        if not result.get("success"):
            return _failed("delete task", result)
        gone = self._find(name, row.get("id"))
        if not gone.get("success"):
            return {"success": False, "step": "read back", "error": gone.get("error"), "deleted": before}
        if gone["data"] is not None:
            return {"success": False, "error": {"code": "read_back_mismatch", "message": "DSM accepted the delete but the task is still listed"}, "deleted": before}
        return {"success": True, "deleted": before}
