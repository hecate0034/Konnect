import copy
import pytest
from test_edits import context, expect
from test_geometry import comp
from runtime.schematic import Schematic
from runtime.schematic_plan import plan
from runtime.geometry import canonical
from runtime.sessions import PadsError


@pytest.fixture
def sheet_context(context):
    old, sid, _, _, fixture = context
    current = {
        "design": "d",
        "sheet": "s",
        "components": canonical([comp("a", 0, "R1"), comp("b", 40, "R2")]),
        "nets": [],
    }
    calls = []
    behavior = {"fail": False}

    def runner(request):
        calls.append(request)
        if request["action"] == "inspect":
            return {
                "ok": True,
                "sheet": copy.deepcopy(current),
                "library_sha256": "lib",
            }
        if request["action"] == "preview":
            return {
                "ok": True,
                "plan": plan(current, request["operation"]),
                "library_sha256": "lib",
            }
        if request["expected_sheet"] != current:
            return {
                "ok": False,
                "error": {"kind": "stale_preview"},
                "project_write_attempted": False,
            }
        if request["action"] == "execute":
            if behavior["fail"]:
                op = copy.deepcopy(request["plan"]["operation"])
                del op["changes"]["position"]
                current.update(plan(current, op)["expected"])
                return {
                    "ok": False,
                    "sheet": copy.deepcopy(current),
                    "error": {"kind": "native_failed"},
                    "project_write_attempted": True,
                }
            current.update(copy.deepcopy(request["plan"]["expected"]))
        if request["action"] == "recover":
            current.update(copy.deepcopy(request["plan"]["before"]))
        return {
            "ok": True,
            "sheet": copy.deepcopy(current),
            "project_write_attempted": request["action"] != "reopen",
        }

    service = Schematic(old.sdd, old.state, old.sessions, runner, clock=lambda: 1000)
    target = next(
        c["target"] for c in current["components"] if c["target"]["uid"] == "a"
    )
    op = {
        "kind": "edit",
        "target": target,
        "changes": {
            "attribute": {"name": "Description", "value": "changed"},
            "position": {"x": 2, "y": 0, "unit": "0.01_inch"},
        },
    }
    return service, sid, current, calls, behavior, op


def test_sheet_recovery_restores_exact_state_and_blocks_replay(sheet_context):
    service, sid, current, calls, _, op = sheet_context
    before = copy.deepcopy(current)
    change = service.preview_change(sid, "d", "s", op)
    service.execute_change(change["preview_id"])
    recovery = service.preview_recovery(sid, change["preview_id"])
    assert (
        recovery["expected_sheet"] == current and recovery["recovery_target"] == before
    )
    result = service.execute_change(recovery["preview_id"])
    assert result["ok"] and current == before
    expect("preview_not_ready", lambda: service.execute_change(recovery["preview_id"]))
    expect(
        "recovery_unavailable",
        lambda: service.preview_recovery(sid, change["preview_id"]),
    )
    assert (
        service.reopen_change(sid, recovery["preview_id"])["status"]
        == "persistence_verified"
    )
    expect("session_invalidated", lambda: service.sessions.load(sid))


def test_later_sheet_changes_are_never_overwritten_by_recovery(sheet_context):
    service, sid, current, calls, _, op = sheet_context
    change = service.preview_change(sid, "d", "s", op)
    service.execute_change(change["preview_id"])
    current["components"][1]["orientation"] = 1
    count = len(calls)
    expect(
        "stale_recovery", lambda: service.preview_recovery(sid, change["preview_id"])
    )
    assert len(calls) == count + 1 and calls[-1]["action"] == "inspect"


def test_observed_partial_edit_requires_rebind_then_can_be_recovered(sheet_context):
    service, sid, current, calls, behavior, op = sheet_context
    before = copy.deepcopy(current)
    change = service.preview_change(sid, "d", "s", op)
    behavior["fail"] = True
    result = service.execute_change(change["preview_id"])
    assert result["project_write_attempted"] is True and not result["ok"]
    expect("session_invalidated", lambda: service.sessions.load(sid))
    sid = service.sessions.bind("designer", change["project"])["session_id"]
    recovery = service.preview_recovery(sid, change["preview_id"])
    service.execute_change(recovery["preview_id"])
    assert current == before


def test_unknown_outcome_is_consumed_and_not_assumed_recoverable(sheet_context):
    service, sid, _, _, _, op = sheet_context
    change = service.preview_change(sid, "d", "s", op)

    def timeout(request):
        raise PadsError("edit_outcome_unknown", "timeout")

    service.runner = timeout
    expect("edit_outcome_unknown", lambda: service.execute_change(change["preview_id"]))
    expect("preview_not_ready", lambda: service.execute_change(change["preview_id"]))
    expect(
        "recovery_unavailable",
        lambda: service.preview_recovery(sid, change["preview_id"]),
    )


def test_sheet_expiry_and_stale_preview_refuse_writes(sheet_context):
    service, sid, current, calls, _, op = sheet_context
    change = service.preview_change(sid, "d", "s", op)
    service.clock = lambda: 1700
    expect("preview_expired", lambda: service.execute_change(change["preview_id"]))
    assert len(calls) == 1
    service.clock = lambda: 1000
    change = service.preview_change(sid, "d", "s", op)
    current["components"][1]["orientation"] = 1
    result = service.execute_change(change["preview_id"])
    assert (
        result["error"]["kind"] == "stale_preview"
        and result["project_write_attempted"] is False
    )


def test_native_check_baseline_is_scoped_and_failed_check_is_not_recorded(
    sheet_context,
):
    from runtime.routing import netlist

    service, sid, current, calls, _, _ = sheet_context

    def runner(request):
        calls.append(request)
        return {
            "ok": True,
            "connectivity": netlist(current),
            "native_verify": {
                "input_sha256": {},
                "profile_sha256": "rules",
                "counts": {"errors": 0, "warnings": 1, "notes": 0},
                "findings_text": "existing warning",
            },
        }

    service.runner = runner
    baseline = service.check_sheet(sid, "d", "s")
    same = service.check_sheet(sid, "d", "s", baseline["check_id"])
    assert (
        same["comparison"]["netlist_equal"]
        and same["comparison"]["native_findings_equal"]
    )
    count = len(calls)
    expect(
        "check_scope_mismatch",
        lambda: service.check_sheet(sid, "d", "other", baseline["check_id"]),
    )
    assert len(calls) == count
    for profile, scope in (("electrical", "sheet"), ("connectivity", "design")):
        expect(
            "check_scope_mismatch",
            lambda: service.check_sheet(
                sid, "d", "s", baseline["check_id"], profile, scope
            ),
        )
    assert len(calls) == count
    service.runner = lambda request: {
        "ok": False,
        "error": {"kind": "native_check_timeout", "message": "timeout"},
    }
    expect("native_check_timeout", lambda: service.check_sheet(sid, "d", "s"))
    with service.db() as db:
        assert db.execute("SELECT count(*) FROM checks").fetchone()[0] == 2
