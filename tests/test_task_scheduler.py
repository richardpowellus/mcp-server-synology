"""Task Scheduler module tests. No NAS required.

Response shapes are the ones an RS1221+ on DSM 7.4.1-90080 Update 1 returned on
2026-10-29; the request shapes are what that build's admin_center.js and
TaskSchedulerUtils.js send.
"""

import asyncio
import copy
import json
from unittest.mock import MagicMock, patch

import pytest

from mcp_server import SynologyMCPServer, ToolFailureError
from taskscheduler import SynologyTaskScheduler
from taskscheduler import synology_tasks

BASE = "https://nas.example.com"
PASSWORD = "correct horse battery staple"

HDD_DB = {
    "depend_on_task": "",
    "description": "",
    "enable": True,
    "event": "bootup",
    "extra": {},
    "last_exit_info": {"exit_code": 0, "exit_type": "normal"},
    "last_start_time": "2026-10-29 22:07:16",
    "last_stop_time": "2026-10-29 22:07:21",
    "notify_enable": True,
    "notify_if_error": False,
    "notify_mail": "someone@example.com",
    "operation": "/volume1/scripts/syno_hdd_db.sh -n",
    "operation_type": "script",
    "owner": {"0": "root"},
    "run_the_same_time": False,
    "status": {"running": []},
    "task_name": "Synology_HDD_db",
}

TEMPLATE = {
    "action": "", "can_edit_name": True, "can_edit_owner": True, "enable": True,
    "extra": {"script": ""}, "id": -1, "name": "Task 4", "owner": "root", "real_owner": "mcp",
    "schedule": {"date": "2026/10/29", "date_type": 0, "hour": 0, "last_work_hour": 0, "minute": 0,
                 "monthly_week": [], "repeat_date": 1001, "repeat_hour": 0, "repeat_hour_store_config": None,
                 "repeat_min": 0, "repeat_min_store_config": None, "version": 4, "week_day": "0,1,2,3,4,5,6"},
    "type": "script",
}


def _response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    return response


def ok(data=None):
    out = {"success": True}
    if data is not None:
        out["data"] = data
    return out


class FakeTaskDSM:
    """A stateful Task Scheduler: event tasks by name, scheduled tasks by id."""

    def __init__(self, events=None, scheduled=None, output=None, results=None, dependents=None):
        self.events = {t["task_name"]: copy.deepcopy(t) for t in (events or [])}
        self.scheduled = {t["id"]: copy.deepcopy(t) for t in (scheduled or [])}
        self.output = output or {"type": "esynoscheduler"}
        self.results = results or {}
        self.dependents = dependents or {}
        self.history = {}
        self.calls = []
        self.next_id = 1001

    def __call__(self, url, params=None, data=None, **kwargs):
        sent = params if params is not None else data
        p = {k: json.loads(v) for k, v in sent.items() if k not in ("api", "method", "version", "_sid")}
        key = (sent["api"], sent["method"])
        self.calls.append((key, sent, p, "post" if data is not None else "get"))
        return _response(self.answer(key, int(sent["version"]), p))

    def called(self, api, method):
        return [c for c in self.calls if c[0] == (api, method)]

    def grid(self):
        rows = [{"action": "User-defined script", "can_delete": True, "can_edit": True, "can_run": True,
                 "enable": t["enable"], "id": 1000 + i, "name": n, "next_trigger_time": t["event"],
                 "owner": next(iter(t["owner"].values())), "type": "event_script"}
                for i, (n, t) in enumerate(sorted(self.events.items()))]
        rows += [{"action": "User-defined script", "can_delete": True, "can_edit": True, "can_run": True,
                  "enable": t["enable"], "id": i, "name": t["name"], "next_trigger_time": "2026-10-30 03:00",
                  "owner": t["owner"], "type": "script"} for i, t in sorted(self.scheduled.items())]
        return rows

    def answer(self, key, version, p):
        api, method = key
        if key == ("SYNO.Core.TaskScheduler", "list"):
            rows = self.grid()
            return ok({"tasks": rows[p["offset"]:p["offset"] + p["limit"]], "total": len(rows)})
        if key == ("SYNO.Core.EventScheduler", "get"):
            t = self.events.get(p["task_name"])
            return ok(t) if t else {"success": False, "error": {"code": 4800}}
        if key == ("SYNO.Core.EventScheduler", "config_get"):
            return ok(self.output)
        if key == ("SYNO.Core.EventScheduler", "config_set"):
            self.output = {"type": p["type"], "output_path": p["output_path"], "enable_output": p["enable_output"]}
            return ok()
        if key == ("SYNO.Core.User.PasswordConfirm", "auth"):
            if p.get("password") != PASSWORD:
                return {"success": False, "error": {"code": 3103}}
            return ok({"SynoConfirmPWToken": "TOKEN-XYZ"}) if version == 2 else ok()
        if key == ("SYNO.Core.User", "list"):
            return ok({"users": [{"name": "mcp", "uid": 1026}, {"name": "richard", "uid": 1027}]})
        if api in ("SYNO.Core.EventScheduler", "SYNO.Core.EventScheduler.Root") and method in ("create", "set"):
            if api.endswith(".Root") and p.get("SynoConfirmPWToken") != "TOKEN-XYZ":
                return {"success": False, "error": {"code": 105}}
            if method == "create" and p["task_name"] in self.events:
                return {"success": False, "error": {"code": 4801}, "errors": "DUPLICATE_PRIMARY_KEY"}
            t = {k: v for k, v in p.items() if k != "SynoConfirmPWToken"}
            t.setdefault("status", {"running": []})
            self.events[p["task_name"]] = t
            return ok()
        if key == ("SYNO.Core.EventScheduler", "run"):
            n = p["task_name"]
            runs = self.results.setdefault(n, [])
            rid = len(runs) + 1
            runs.append({"result_id": rid, "start_time": "2026-10-29 23:00:00", "stop_time": "2026-10-29 23:00:01",
                         "trigger_event": "on_demand", "exit_info": {"exit_code": 0, "exit_type": "normal"}, "task_name": n})
            return ok()
        if key == ("SYNO.Core.EventScheduler", "result_list"):
            return ok(self.results.get(p["task_name"], []))
        if key == ("SYNO.Core.EventScheduler", "result_get_file"):
            if not self.output.get("enable_output"):
                return {"success": False, "error": {"code": 117}}
            return ok({"script_in": self.events[p["task_name"]]["operation"], "script_out": "uid=0(root) gid=0(root)\n"})
        if key == ("SYNO.Core.EventScheduler", "list_relate"):
            return ok([{"task_name": n} for n in self.dependents.get(p["task_name"], [])])
        if key == ("SYNO.Core.EventScheduler", "delete"):
            self.events.pop(p["task_name"], None)
            return ok()
        if key == ("SYNO.Core.TaskScheduler", "get"):
            if p["id"] == -1:
                return ok(copy.deepcopy(TEMPLATE))
            t = self.scheduled.get(p["id"])
            return ok(t) if t else {"success": False, "error": {"code": 4800}}
        if api in ("SYNO.Core.TaskScheduler", "SYNO.Core.TaskScheduler.Root") and method in ("create", "set"):
            if api.endswith(".Root") and p.get("SynoConfirmPWToken") != "TOKEN-XYZ":
                return {"success": False, "error": {"code": 105}}
            tid = p.get("id") or self.next_id
            self.next_id += 1
            t = {k: v for k, v in p.items() if k not in ("SynoConfirmPWToken", "type")}
            t["id"] = tid
            self.scheduled[tid] = t
            return ok({"id": tid})
        if key == ("SYNO.Core.TaskScheduler", "delete"):
            for task in p["tasks"]:
                self.scheduled.pop(task["id"], None)
            return ok()
        if key == ("SYNO.Core.TaskScheduler", "run"):
            return ok()
        if key == ("SYNO.Core.TaskScheduler", "get_history_status_list"):
            return ok(self.history.get(p["id"], []))
        return {"success": False, "error": {"code": 103}}


def patched(fake):
    return patch("utils.synology_api.requests.get", side_effect=fake), patch("utils.synology_api.requests.post", side_effect=fake)


def tasks(password=PASSWORD):
    return SynologyTaskScheduler(BASE, "SID", syno_token="TOK", password_provider=lambda: password, username="mcp")


def run(fake, fn):
    g, p = patched(fake)
    with g, p:
        return fn(tasks())


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(synology_tasks.time, "sleep", lambda s: None)


class TestList:
    def test_lists_triggered_task_with_script_and_last_run(self):
        fake = FakeTaskDSM(events=[HDD_DB])
        out = run(fake, lambda t: t.list_tasks())
        assert out["success"] is True
        (task,) = out["tasks"]
        assert task["name"] == "Synology_HDD_db"
        assert task["kind"] == "triggered" and task["trigger"] == "bootup"
        assert task["owner"] == "root"
        assert task["script"] == HDD_DB["operation"]
        assert task["last_run"]["exit"] == {"exit_code": 0, "exit_type": "normal"}
        assert out["output_saving"] == {"save_output": False, "output_path": ""}

    def test_filters_by_name(self):
        fake = FakeTaskDSM(events=[HDD_DB, {**HDD_DB, "task_name": "other"}])
        out = run(fake, lambda t: t.list_tasks("other"))
        assert [x["name"] for x in out["tasks"]] == ["other"]


class TestSetTriggered:
    def test_root_bootup_task_confirms_password_then_writes_through_root_api(self):
        fake = FakeTaskDSM()
        out = run(fake, lambda t: t.set_script_task("mcp-task-test", "id > /tmp/mcp-task-test.txt", "bootup", enable=False))
        assert out["success"] is True, out
        assert out["action"] == "created" and out["before"] is None
        assert out["after"]["script"] == "id > /tmp/mcp-task-test.txt"
        assert out["after"]["enabled"] is False and out["after"]["owner"] == "root"

        (confirm,) = fake.called("SYNO.Core.User.PasswordConfirm", "auth")
        assert confirm[1]["version"] == "2" and confirm[3] == "post"
        (create,) = fake.called("SYNO.Core.EventScheduler.Root", "create")
        sent = create[2]
        assert sent["owner"] == {"0": "root"}
        assert sent["event"] == "bootup" and sent["operation_type"] == "script"
        assert sent["SynoConfirmPWToken"] == "TOKEN-XYZ"
        assert not fake.called("SYNO.Core.EventScheduler", "create")
        # The password and the confirm token never reach the caller.
        assert PASSWORD not in json.dumps(out) and "TOKEN-XYZ" not in json.dumps(out)

    def test_update_uses_set_and_keeps_dependencies(self):
        fake = FakeTaskDSM(events=[{**HDD_DB, "depend_on_task": "[prep]"}])
        out = run(fake, lambda t: t.set_script_task("Synology_HDD_db", "echo new", "bootup"))
        assert out["success"] is True and out["action"] == "updated"
        assert out["before"]["script"] == HDD_DB["operation"]
        (save,) = fake.called("SYNO.Core.EventScheduler.Root", "set")
        assert save[2]["depend_on_task"] == "[prep]"

    def test_non_root_owner_uses_plain_api_and_needs_no_password(self):
        fake = FakeTaskDSM()
        g, p = patched(fake)
        with g, p:
            out = tasks(password=None).set_script_task("as-richard", "whoami", "bootup", owner="richard")
        assert out["success"] is True, out
        (create,) = fake.called("SYNO.Core.EventScheduler", "create")
        assert create[2]["owner"] == {"1027": "richard"}
        assert not fake.called("SYNO.Core.User.PasswordConfirm", "auth")

    def test_root_task_without_a_password_writes_nothing(self):
        fake = FakeTaskDSM()
        g, p = patched(fake)
        with g, p:
            out = tasks(password=None).set_script_task("x", "id", "bootup")
        assert out["success"] is False and out["error"]["code"] == "password_unavailable"
        assert not [c for c in fake.calls if c[0][1] in ("create", "set")]

    def test_wrong_password_writes_nothing(self):
        fake = FakeTaskDSM()
        g, p = patched(fake)
        with g, p:
            out = tasks(password="nope").set_script_task("x", "id", "bootup")
        assert out["success"] is False and out["step"] == "confirm password"
        assert not [c for c in fake.calls if c[0][1] in ("create", "set")]

    def test_unknown_owner_is_refused(self):
        fake = FakeTaskDSM()
        out = run(fake, lambda t: t.set_script_task("x", "id", "bootup", owner="nobody"))
        assert out["error"]["code"] == "no_such_user"

    def test_trigger_change_is_refused(self):
        fake = FakeTaskDSM(events=[HDD_DB])
        out = run(fake, lambda t: t.set_script_task("Synology_HDD_db", "id", "shutdown"))
        assert out["error"]["code"] == "trigger_mismatch"
        assert not fake.called("SYNO.Core.EventScheduler.Root", "set")

    def test_kind_change_is_refused(self):
        fake = FakeTaskDSM(events=[HDD_DB])
        out = run(fake, lambda t: t.set_script_task("Synology_HDD_db", "id", "schedule", schedule={"hour": 3}))
        assert out["error"]["code"] == "kind_mismatch"

    @pytest.mark.parametrize("kwargs, code", [
        ({"trigger": "reboot"}, "invalid_argument"),
        ({"script": "  "}, "invalid_argument"),
        ({"script": "x" * 8193}, "invalid_argument"),
        ({"trigger": "schedule"}, "invalid_argument"),
        ({"schedule": {"hour": 1}}, "invalid_argument"),
    ])
    def test_bad_arguments_are_refused_before_any_call(self, kwargs, code):
        fake = FakeTaskDSM()
        args = {"name": "x", "script": "id", "trigger": "bootup", **kwargs}
        out = run(fake, lambda t: t.set_script_task(**args))
        assert out["error"]["code"] == code
        assert fake.calls == []


class TestSetScheduled:
    def test_root_weekly_task_is_built_from_dsm_template(self):
        fake = FakeTaskDSM()
        out = run(fake, lambda t: t.set_script_task(
            "nightly", "echo hi", "schedule", schedule={"repeat": "weekly", "week_days": [1, 3], "hour": 3, "minute": 15}))
        assert out["success"] is True, out
        (create,) = fake.called("SYNO.Core.TaskScheduler.Root", "create")
        assert create[1]["version"] == "4"
        sent = create[2]
        assert sent["type"] == "script" and sent["owner"] == "root" and sent["real_owner"] == "mcp"
        assert sent["extra"]["script"] == "echo hi"
        s = sent["schedule"]
        assert (s["date_type"], s["repeat_date"], s["week_day"], s["hour"], s["minute"], s["last_work_hour"]) == (0, 1002, "1,3", 3, 15, 3)
        assert s["repeat_hour_store_config"] == [] and s["repeat_min_store_config"] == []
        assert out["after"]["kind"] == "scheduled" and out["after"]["id"] == 1001

    def test_once_needs_a_date(self):
        fake = FakeTaskDSM()
        out = run(fake, lambda t: t.set_script_task("once", "id", "schedule", schedule={"repeat": "once", "hour": 1}))
        assert out["error"]["code"] == "invalid_argument"
        assert not fake.called("SYNO.Core.TaskScheduler.Root", "create")


class TestRunAndResult:
    def test_run_waits_and_returns_output(self):
        fake = FakeTaskDSM(events=[HDD_DB], output={"type": "esynoscheduler", "enable_output": True, "output_path": "docker/task-logs"})
        out = run(fake, lambda t: t.run_task("Synology_HDD_db", wait_seconds=10))
        assert out["finished"] is True
        assert out["result"]["output"] == "uid=0(root) gid=0(root)\n"
        assert out["result"]["exit"] == {"exit_code": 0, "exit_type": "normal"}
        (call,) = fake.called("SYNO.Core.EventScheduler", "run")
        assert call[2] == {"task_name": "Synology_HDD_db"}

    def test_result_without_output_saving_says_so(self):
        fake = FakeTaskDSM(events=[HDD_DB], results={"Synology_HDD_db": [
            {"result_id": 31, "start_time": "a", "stop_time": "b", "trigger_event": "bootup", "exit_info": {"exit_code": 0, "exit_type": "normal"}},
            {"result_id": 32, "start_time": "c", "stop_time": "d", "trigger_event": "bootup", "exit_info": {"exit_code": 1, "exit_type": "normal"}},
        ]})
        out = run(fake, lambda t: t.task_result("Synology_HDD_db"))
        assert out["success"] is True
        assert out["result"]["result_id"] == 32 and out["result"]["exit"]["exit_code"] == 1
        assert out["result"]["output"] is None and out["output_saving"] is False
        assert "synology_task_output_set" in out["message"]

    def test_scheduled_run_sends_id_and_real_owner(self):
        fake = FakeTaskDSM(scheduled=[{"id": 7, "name": "n", "owner": "root", "real_owner": "mcp", "enable": True, "extra": {"script": "id"}, "schedule": {}}])
        out = run(fake, lambda t: t.run_task("n"))
        assert out["started"] is True
        (call,) = fake.called("SYNO.Core.TaskScheduler", "run")
        assert call[1]["version"] == "2" and call[2] == {"tasks": [{"id": 7, "real_owner": "mcp"}]}

    def test_unknown_task(self):
        out = run(FakeTaskDSM(), lambda t: t.run_task("nope"))
        assert out["error"]["code"] == "not_found"


class TestDelete:
    def test_delete_reads_back_absence(self):
        fake = FakeTaskDSM(events=[HDD_DB])
        out = run(fake, lambda t: t.delete_task("Synology_HDD_db"))
        assert out["success"] is True and out["deleted"]["name"] == "Synology_HDD_db"
        assert "Synology_HDD_db" not in fake.events

    def test_delete_refuses_while_another_task_depends_on_it(self):
        fake = FakeTaskDSM(events=[HDD_DB], dependents={"Synology_HDD_db": ["after-hdd"]})
        out = run(fake, lambda t: t.delete_task("Synology_HDD_db"))
        assert out["error"]["code"] == "has_dependents"
        assert "Synology_HDD_db" in fake.events

    def test_delete_scheduled(self):
        fake = FakeTaskDSM(scheduled=[{"id": 7, "name": "n", "owner": "root", "real_owner": "mcp", "enable": True, "extra": {}, "schedule": {}}])
        out = run(fake, lambda t: t.delete_task("n"))
        assert out["success"] is True and fake.scheduled == {}


class TestOutputSetting:
    def test_enable_reads_back(self):
        fake = FakeTaskDSM()
        out = run(fake, lambda t: t.set_output_saving(True, "/docker/task-logs/"))
        assert out["success"] is True
        assert out["after"] == {"save_output": True, "output_path": "docker/task-logs"}

    def test_enable_without_a_path_is_refused(self):
        fake = FakeTaskDSM()
        out = run(fake, lambda t: t.set_output_saving(True))
        assert out["error"]["code"] == "invalid_argument"
        assert not fake.called("SYNO.Core.EventScheduler", "config_set")


class TestServerGuards:
    def server(self):
        srv = SynologyMCPServer()
        srv.sessions[BASE] = "sid"
        return srv

    @pytest.mark.parametrize("tool, args", [
        ("synology_task_set", {"name": "x", "script": "id", "trigger": "bootup"}),
        ("synology_task_run", {"name": "x"}),
        ("synology_task_delete", {"name": "x"}),
        ("synology_task_output_set", {"enable": True, "output_path": "docker/logs"}),
        ("synology_task_set", {"name": "x", "script": "id", "trigger": "bootup", "confirm": "true"}),
    ])
    def test_writes_refuse_without_confirm_true(self, tool, args):
        srv = self.server()
        with pytest.raises(ToolFailureError) as e:
            asyncio.run(srv._dispatch_tool(tool, args))
        assert e.value.payload["error"]["code"] == "confirmation_required"

    def test_undeclared_argument_is_refused(self):
        srv = self.server()
        with pytest.raises(ToolFailureError) as e:
            asyncio.run(srv._dispatch_tool("synology_task_set", {"name": "x", "script": "id", "trigger": "bootup", "user": "root", "confirm": True}))
        assert e.value.payload["error"]["code"] == "unknown_argument"

    def test_registered(self):
        names = set(self.server()._tool_registry)
        assert {"synology_task_list", "synology_task_set", "synology_task_run", "synology_task_result",
                "synology_task_delete", "synology_task_output_set"} <= names
