import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_edits import context, expect
from runtime.attribute_state import property_state, parse_batch, FROZEN
from runtime.backannotation import parse_bam
from runtime.bam_execution import TARGETS, SCOPE, planned, read_approved, normalize
from runtime.bam_service import BamExecution
from runtime.sessions import PadsError


def sample():
    live, components, declarations, members = [], [], [], []
    bam = []
    for uid, ref in TARGETS.items():
        after = {"C4": "C44", "C2": "C45", "U12": "U32"}[ref]
        attrs = {
            name: property_state(parse_batch(f"0 {name}=part\r"), {}, name)
            for name in ("Part Name", "Part Label", "Tolerance")
        }
        live.append(
            {
                "scope": SCOPE,
                "uid": uid,
                "refdes": ref,
                "path": uid,
                "attributes": attrs,
                "frozen": property_state({}, {}, FROZEN),
            }
        )
        components.append(
            {
                "target": {"uid": uid, "refdes": ref, "path": uid},
                "position": [100, 100],
                "attributes": [
                    {
                        "name": n,
                        "base_value": "part",
                        "instance_value": "",
                        "effective_value": "part",
                    }
                    for n in ("Part Name", "Part Label")
                ]
                + [
                    {
                        "name": "Ref Designator",
                        "base_value": "?",
                        "instance_value": ref,
                        "effective_value": ref,
                    }
                ],
            }
        )
        declarations.append({"refdes": ref, "pin_numbers": ["1", "2"]})
        members.append({"refdes": ref, "pin_number": "1"})
        bam.append(
            f"CHANGE '{SCOPE}' INSTANCE\n SELECT COMP {uid}\n PARAM 'Part Name' = 'part'\n PARAM 'Part Label' = 'part'\n PARAM 23 = DELETE\n PARAM 15 = '{after}' /* Reference Desig was {ref} */\n"
        )
    live.append(
        {
            "scope": "CORPORATE(other)",
            "uid": "$20I1",
            "path": "$20I1",
            "refdes": "R1",
            "attributes": {},
            "frozen": property_state(parse_batch("0 Frozen Package=Fix\r"), {}, FROZEN),
        }
    )
    graph = {"components": declarations, "nets": [{"name": "GND", "pins": members}]}
    sheet = {
        "components": components,
        "nets": [{"name": "GND", "segments": [[0, 0, 100, 100]]}],
    }
    text = "\n".join(bam)
    return text, parse_bam(text), normalize(live), normalize(graph), normalize(sheet)


def test_exact_projection_preserves_geometry_and_distinguishes_new_overrides():
    _, records, live, graph, sheet = sample()
    original = copy.deepcopy([live, graph, sheet])
    result = planned(records, live, graph, sheet)
    assert [live, graph, sheet] == original
    assert {r["refdes"] for r in result["live"]} == {"C44", "C45", "U32", "R1"}
    assert result["sheet"]["nets"] == sheet["nets"]
    assert all(
        r["attributes"]["Part Name"]["instance_present"]
        for r in result["live"]
        if r["scope"] == SCOPE
    )
    assert {p["refdes"] for p in result["graph"]["nets"][0]["pins"]} == {
        "C44",
        "C45",
        "U32",
    }


@pytest.mark.parametrize(
    "fault",
    [
        "pin",
        "scope",
        "uid",
        "count",
        "old_ref",
        "collision",
        "duplicate",
        "prefix",
        "multi_unit",
        "frozen",
        "base_flag",
        "override",
        "attribute_value",
        "attribute_name",
        "visible",
        "sheet_path",
        "outside_unfrozen",
    ],
)
def test_unsupported_bam_plans_fail_before_native_writes(fault):
    _, records, live, graph, sheet = sample()
    r = records[0]
    row = next(x for x in live if x["uid"] == r["uid"])
    if fault == "pin":
        r["pins"] = [{"name": "A", "before": "1", "after": "2"}]
    if fault == "scope":
        r["scope"] = "other"
    if fault == "uid":
        r["uid"] = "$bad"
    if fault == "count":
        records.pop()
    if fault == "old_ref":
        r["refdes"]["before"] = "C9"
    if fault == "collision":
        r["refdes"]["after"] = "C2"
    if fault == "duplicate":
        records[1]["refdes"]["after"] = r["refdes"]["after"]
    if fault == "prefix":
        r["refdes"]["after"] = "X90"
    if fault == "multi_unit":
        live.append(copy.deepcopy(row))
    if fault == "frozen":
        row["frozen"] = property_state(
            {}, parse_batch("0 Frozen Package=Fix\r"), FROZEN
        )
    if fault == "base_flag":
        row["frozen"] = property_state(parse_batch("0 Frozen Package=\r"), {}, FROZEN)
    if fault == "override":
        row["attributes"]["Part Name"]["instance_present"] = True
    if fault == "attribute_value":
        r["attributes"]["Part Name"] = "other"
    if fault == "attribute_name":
        r["attributes"]["Tolerance"] = "1%"
    if fault == "visible":
        row["attributes"]["Part Name"]["base_visibility"] = 1
    if fault == "sheet_path":
        sheet["components"][0]["target"]["path"] = "other"
    if fault == "outside_unfrozen":
        next(x for x in live if x["refdes"] == "R1")["frozen"] = property_state(
            {}, {}, FROZEN
        )
    with pytest.raises(PadsError):
        planned(records, live, graph, sheet)


def test_hash_binds_exact_bytes_including_comments(tmp_path):
    text, records, *_ = sample()
    p = tmp_path / "approved.bam"
    p.write_bytes(text.encode())
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    assert read_approved(p, digest) == records
    p.write_bytes(p.read_bytes() + b"\n/* same semantics, changed bytes */\n")
    expect("approved_bam_changed", lambda: read_approved(p, digest))


def prepared_service(context):
    original, sid, *_ = context
    service = BamExecution(
        original.sdd, original.state, original.sessions, clock=lambda: 1000
    )
    text, records, live, graph, sheet = sample()
    impact_id = "a" * 32
    path = (
        service.state
        / "evidence"
        / ("backannotation-" + impact_id)
        / "first/changes.bam"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(text.encode())
    request = service.base(sid, "CORPORATE", "Powersupply", "bam_plan")
    report = {
        "design": "CORPORATE",
        "scope_sheet": "Powersupply",
        "mode": "repackage_unfixed",
        "project": request["expected_path"],
        "policy_sha256": request["policy_sha256"],
        "repeatability": {"identical": True},
        "evidence": {
            "first": {
                "bam": str(path),
                "bam_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        },
        "proposal_records": records,
        "live_baseline": live,
        "netlist_baseline": graph,
        "provenance": {},
    }
    with service.db() as db:
        db.execute(
            "CREATE TABLE backannotation_impacts (id TEXT PRIMARY KEY,status TEXT,record TEXT)"
        )
        db.execute(
            "INSERT INTO backannotation_impacts VALUES (?,?,?)",
            (impact_id, "inspected", json.dumps(report)),
        )
    service.checkpoint = lambda *args: "backup-digest"
    before = {"live": live, "graph": graph, "sheet": sheet}
    after = planned(records, live, graph, sheet)
    calls = []

    def runner(request):
        calls.append(request["action"])
        return {
            "ok": True,
            "before": before,
            "after": after,
            "observed": before if request["action"] == "bam_plan" else after,
            "protected": {},
            "provenance": {"adapter": "test"},
            "project_write_attempted": request["action"] == "bam_apply",
        }

    service.runner = runner
    plan = service.preview_execution(sid, impact_id, "b" * 32)
    return service, sid, plan, calls


def test_consumed_execution_invalidates_binding_and_refuses_replay(context):
    service, sid, plan, calls = prepared_service(context)
    result = service.run_execution(plan["preview_id"])
    assert result["status"] == "executed" and calls == ["bam_plan", "bam_apply"]
    expect("session_invalidated", lambda: service.sessions.load(sid))
    expect("preview_not_ready", lambda: service.run_execution(plan["preview_id"]))


@pytest.mark.parametrize(
    "fault", ["hash", "checkpoint", "expired", "exception", "partial", "mismatch"]
)
def test_execution_failure_preserves_evidence_and_does_not_replay(context, fault):
    service, sid, plan, calls = prepared_service(context)
    if fault == "hash":
        Path(plan["approved_file"]).write_text("tampered")
    if fault == "checkpoint":
        service.checkpoint = lambda *args: (_ for _ in ()).throw(
            PadsError("checkpoint_corrupt", "bad backup")
        )
    if fault == "expired":
        service.clock = lambda: 2000
    if fault == "exception":
        service.runner = lambda request: (_ for _ in ()).throw(
            RuntimeError("lost native worker")
        )
    if fault == "partial":
        service.runner = lambda request: {
            "ok": False,
            "project_write_attempted": True,
            "error": {"kind": "partial"},
        }
    if fault == "mismatch":
        service.runner = lambda request: {"ok": True, "observed": {}}
    if fault == "partial":
        assert not service.run_execution(plan["preview_id"])["ok"]
    else:
        with pytest.raises((PadsError, RuntimeError)):
            service.run_execution(plan["preview_id"])
    expect("preview_not_ready", lambda: service.run_execution(plan["preview_id"]))
    if fault in ("hash", "checkpoint", "expired"):
        assert calls == ["bam_plan"]
        service.sessions.load(sid)
    else:
        expect("session_invalidated", lambda: service.sessions.load(sid))


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "hash",
        "expired",
        "identity",
        "readonly",
        "source",
        "readback",
        "setter",
        "stale",
    ],
)
def test_native_executor_rechecks_before_setters_and_reports_partial_failure(
    tmp_path, monkeypatch, fault
):
    from runtime import native_bam_execution as native

    text, records, live, graph, sheet = sample()
    before = {"live": live, "graph": graph, "sheet": sheet}
    after = planned(records, live, graph, sheet)
    bam = tmp_path / "approved.bam"
    bam.write_bytes(text.encode())
    exe = tmp_path / "designer.exe"
    exe.write_bytes(b"native")
    sha = hashlib.sha256(bam.read_bytes()).hexdigest()
    initial = {"executable": str(exe), "instance_guid": "one"}
    app = SimpleNamespace(ActiveView=SimpleNamespace(Block=object()))
    events = []

    class Attribute:
        def __init__(self, uid, name):
            self.uid, self.name = uid, name

        @property
        def InstanceValue(self):
            return ""

        @InstanceValue.setter
        def InstanceValue(self, value):
            events.append((self.uid, self.name, value))
            if fault == "setter":
                raise RuntimeError("COM setter failed")

    class Component:
        def __init__(self, uid, ref):
            self.UID, self.ref = uid, ref
            self.Parent = SimpleNamespace(SheetNum="Powersupply")

        @property
        def Refdes(self):
            return self.ref

        @Refdes.setter
        def Refdes(self, value):
            events.append((self.UID, "Refdes", value))
            self.ref = value

    components = [Component(uid, ref) for uid, ref in TARGETS.items()]

    def call(obj, name, *args):
        if name in ("IsReadOnlyMode", "IsReadOnly"):
            return fault == "readonly"
        if name == "DesignComponents":
            return components
        if name == "GetName":
            return "CORPORATE"
        if name == "FindAttribute":
            return Attribute(obj.UID, args[0])
        raise AssertionError(name)

    count = [0]

    def snap(*args):
        count[0] += 1
        if (
            fault == "stale"
            or count[0] > 1
            and (fault == "readback" or len(events) != 9)
        ):
            return {}, {}
        return (before if count[0] == 1 else after), {}

    monkeypatch.setattr(native, "call", call)
    monkeypatch.setattr(native, "snapshot", snap)
    monkeypatch.setattr(native, "protected_files", lambda root: {})
    monkeypatch.setattr(native.time, "time", lambda: 1000)
    provenance = {
        "adapter": native.VERSION,
        "designer_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "exporters": {},
    }
    if fault == "source":
        provenance["designer_sha256"] = "changed"
    if fault == "hash":
        bam.write_bytes(bam.read_bytes() + b" ")
    request = {
        "action": "bam_apply",
        "output_dir": str(tmp_path / "out"),
        "policy": {"fixture": str(tmp_path)},
        "approved_file": str(bam),
        "approved_sha256": sha,
        "records": records,
        "before": before,
        "after": after,
        "protected": {},
        "provenance": provenance,
        "expires_at": 999 if fault == "expired" else 1100,
    }
    response = native.operate(
        app, request, lambda: {} if fault == "identity" else initial, initial
    )
    if fault is None:
        assert response["ok"] and response["observed"] == after and len(events) == 9
        assert {c.Refdes for c in components} == {"C44", "C45", "U32"}
    else:
        assert not response["ok"]
        assert response["project_write_attempted"] == (fault in ("readback", "setter"))
        if fault not in ("readback", "setter"):
            assert not events
