import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_edits import context, expect
from test_geometry import comp
from runtime.packaging import Packaging, expected_graph, validate_output
from runtime.sessions import PadsError


SUCCESS = "Packaging has been successfully tested with no errors or warnings.\nPackager finished successfully.\n"


@pytest.mark.parametrize("preview", [True, False])
def test_packager_requires_success_markers_and_empty_assignment_file(preview):
    message = SUCCESS + (
        "!THE iCDB HAS NOT BEEN UPDATED!" if preview else "!THE iCDB IS UP-TO-DATE!"
    )
    validate_output(0, message, "", message, b"", preview)
    for rc, out, err, log, bam in [
        (1, message, "", message, b""),
        (0, message, "diagnostic", message, b""),
        (0, message + "\nERROR: failed", "", message, b""),
        (0, message + "\nWARNING: unsupported", "", message, b""),
        (0, "", "", "", b""),
        (0, message, "", message, b"rename R1 R2"),
    ]:
        with pytest.raises(PadsError):
            validate_output(rc, out, err, log, bam, preview)
    if preview:
        expect(
            "packaging_unexpected_write",
            lambda: validate_output(
                0,
                message + "The Common DataBase has been successfully updated.",
                "",
                "",
                b"",
                True,
            ),
        )


def sample():
    before = {
        "design": "d",
        "sheet": "s",
        "components": [comp("a", 0, "R1")],
        "nets": [],
    }
    placed = copy.deepcopy(before)
    placed["components"].append(comp("b", 60, "R2"))
    baseline = {
        "netlist": {
            "components": [
                {"refdes": "R1", "pin_numbers": ["1"], "repeated_pin_declarations": []}
            ],
            "nets": [],
        }
    }
    return baseline, before, placed


@pytest.mark.parametrize(
    "change",
    [
        None,
        "missing_number",
        "duplicate_number",
        "connected",
        "hierarchy",
        "existing_ref",
        "two_added",
    ],
)
def test_only_explicit_unconnected_new_instance_is_eligible(change):
    baseline, before, placed = sample()
    new = placed["components"][-1]
    if change == "missing_number":
        new["pins"][0]["number"] = ""
    if change == "duplicate_number":
        new["pins"].append(copy.deepcopy(new["pins"][0]))
    if change == "connected":
        new["pins"][0]["net_uid"] = "net"
    if change == "hierarchy":
        new["target"]["path"] = "block\\b"
    if change == "existing_ref":
        new["target"]["refdes"] = "R1"
    if change == "two_added":
        placed["components"].append(comp("c", 100, "R3"))
    if change:
        with pytest.raises(PadsError):
            expected_graph(baseline, before, placed)
    else:
        graph, pins = expected_graph(baseline, before, placed)
        assert (
            graph["nets"] == baseline["netlist"]["nets"]
            and len(graph["components"]) == 2
        )
        assert len(baseline["netlist"]["components"]) == 1 and pins == [
            {"refdes": "R2", "pin_number": "1"}
        ]


@pytest.fixture
def package_context(context):
    original, sid, _, calls, _ = context
    service = Packaging(
        original.sdd, original.state, original.sessions, clock=lambda: 1000
    )
    baseline, before, placed = sample()
    request = service.base(sid, "d", "s", "inspect")
    record = {
        "schema": 2,
        "preview_id": "a" * 32,
        "session_id": sid,
        "action": "execute",
        "design": "d",
        "sheet": "s",
        "plan": {"kind": "place", "before": before},
        "expected_sheet": before,
        "project": request["expected_path"],
        "policy_sha256": request["policy_sha256"],
        "result": {"ok": True, "sheet": placed},
    }
    service.insert(record)
    service.persist(record, "executed")
    baseline.update(
        design="d",
        project=request["expected_path"],
        policy_sha256=request["policy_sha256"],
        provenance={"format": "test"},
    )
    with service.db() as db:
        db.execute(
            "CREATE TABLE logical_checks (id TEXT PRIMARY KEY,record TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO logical_checks VALUES (?,?)", ("b" * 32, json.dumps(baseline))
        )

    def runner(request):
        calls.append(request)
        return {
            "ok": True,
            "sources": {"source": "same"},
            "schematic_update_attempted": request["action"] == "packaging_execute",
        }

    service.runner = runner
    return service, sid, record, calls


def test_packaging_single_use_scope_expiration_and_recovery_record(package_context):
    service, sid, change, calls = package_context
    p = service.preview_packaging(sid, change["preview_id"], "b" * 32)
    assert not p["schematic_update_attempted"] and p["auxiliary_files_written"]
    assert service.execute_packaging(p["preview_id"])["status"] == "executed"
    expect("preview_not_ready", lambda: service.execute_packaging(p["preview_id"]))
    assert service.previous(change["preview_id"])[0] == "executed"
    q = service.preview_packaging(sid, change["preview_id"], "b" * 32)
    service.clock = lambda: 2000
    expect("preview_expired", lambda: service.execute_packaging(q["preview_id"]))
    assert calls[-1]["action"] == "packaging_preview"
    service.persist(change, "recovered")
    expect(
        "packaging_scope_denied",
        lambda: service.preview_packaging(sid, change["preview_id"], "b" * 32),
    )


@pytest.mark.parametrize("outcome", ["before_write", "after_write", "unknown"])
def test_failed_packaging_is_consumed_and_unknown_writes_invalidate_session(
    package_context, outcome
):
    service, sid, change, _ = package_context
    p = service.preview_packaging(sid, change["preview_id"], "b" * 32)

    def runner(request):
        if outcome == "unknown":
            raise RuntimeError("timeout")
        return {
            "ok": False,
            "error": {"kind": "failed"},
            "schematic_update_attempted": outcome == "after_write",
        }

    service.runner = runner
    if outcome == "unknown":
        with pytest.raises(RuntimeError):
            service.execute_packaging(p["preview_id"])
    else:
        assert not service.execute_packaging(p["preview_id"])["ok"]
    expect("preview_not_ready", lambda: service.execute_packaging(p["preview_id"]))
    if outcome == "before_write":
        assert service.sessions.load(sid)
    else:
        expect("session_invalidated", lambda: service.sessions.load(sid))


@pytest.mark.parametrize(
    "change",
    [
        None,
        "preflight_assignment",
        "preflight_protected",
        "nonempty_bam",
        "stale_sources",
        "changed_graph",
        "expired",
        "post_graph",
        "post_assignment",
        "candidate_after",
        "preflight_database",
        "preflight_exporter",
        "hierarchy_pin",
    ],
)
def test_native_packaging_boundaries_and_post_verification(
    tmp_path, monkeypatch, change
):
    from runtime import native_packaging as native, native_schematic
    from runtime.logical import FORMAT

    root = tmp_path / "fixture"
    root.mkdir()
    project = root / "test.prj"
    project.write_text("LIST PDBs\nENDLIST\n")
    database = root / "database"
    database.mkdir()
    library = root / "library.lmc"
    library.write_bytes(b"library")
    executable = tmp_path / "wg/win32/bin/package.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixed program")
    for name in ("SctNetlist", "AllegroNetlist"):
        exe = tmp_path / "wv/win32/bin" / (name + ".exe")
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"exporter")
    exporter_hashes = {
        n: native.fingerprint(tmp_path / "wv/win32/bin" / (n + ".exe"))
        for n in ("SctNetlist", "AllegroNetlist")
    }
    monkeypatch.setenv("SDD_HOME", str(tmp_path))
    baseline, before, placed = sample()
    graph, pins = expected_graph(baseline, before, placed)
    state = {"stage": "initial", "calls": [], "test": None}
    data = SimpleNamespace(iCDBDir=str(database), CentralLibraryPath=str(library))
    app = SimpleNamespace(ActiveView=SimpleNamespace(Block=object()))
    monkeypatch.setattr(
        native,
        "call",
        lambda obj, name, *args: data if name == "GetProjectData" else False,
    )
    monkeypatch.setattr(native_schematic, "read_sheet", lambda *args: placed)

    def protected(*args):
        return {
            "lib": (
                "changed"
                if state["test"] == "preflight_protected"
                and state["stage"] == "proposal"
                else "same"
            )
        }

    def assignments(*args):
        altered = (
            state["test"] == "stale_sources"
            or (
                state["test"] == "preflight_assignment" and state["stage"] == "proposal"
            )
            or (state["test"] == "post_assignment" and state["stage"] == "applied")
        )
        return ["changed" if altered else "same"]

    monkeypatch.setattr(native, "protected_files", protected)
    monkeypatch.setattr(native, "assignments", assignments)
    monkeypatch.setattr(
        native,
        "read_selected",
        lambda *args: (
            [{"block_path": "child", "instance_path": "nested\\part"}]
            if state["test"] == "hierarchy_pin"
            else []
        ),
    )

    def export(*args, **kwargs):
        value = copy.deepcopy(graph)
        if (
            state["test"] == "changed_graph"
            or (state["test"] == "candidate_after" and state["stage"] == "proposal")
            or state["test"] == "post_graph"
            and state["stage"] == "applied"
        ):
            value["nets"] = [{"changed": True}]
        return value, {
            n: {
                "executable_sha256": (
                    "changed"
                    if state["test"] == "preflight_exporter"
                    and state["stage"] == "proposal"
                    else h
                )
            }
            for n, h in exporter_hashes.items()
        }

    monkeypatch.setattr(native, "export_round", export)

    def package(project, design, output, preview):
        state["calls"].append(preview)
        state["stage"] = "proposal" if preview else "applied"
        if state["test"] == "preflight_database":
            data.iCDBDir = str(tmp_path / "external")
        if state["test"] == "nonempty_bam":
            raise PadsError("packaging_assignment_required", "assignments")
        return {"executable_sha256": native.fingerprint(executable)}

    monkeypatch.setattr(native, "run_package", package)
    request = {
        "policy": {"fixture": str(root)},
        "expected_path": str(project),
        "design": "d",
        "sheet": "s",
        "expected_sheet": placed,
        "expected_graph": graph,
        "pins": pins,
        "output_dir": str(tmp_path / "out"),
        "baseline_provenance": {
            "format": FORMAT,
            "snapshot": "DxD",
            "flow": "DX",
            "exporter_sha256": exporter_hashes,
        },
        "action": "packaging_preview",
    }
    preview = native.operate(app, request, lambda: {}, {})
    assert preview["ok"]
    state.update(stage="initial", calls=[], test=change)
    request.update(
        action="packaging_execute",
        expected_preview=preview,
        expires_at=0 if change == "expired" else 9999999999,
    )
    result = native.operate(app, request, lambda: {}, {})
    if change:
        assert not result["ok"], result
        if change.startswith("post_"):
            assert result["schematic_update_attempted"] and state["calls"] == [
                True,
                False,
            ]
        else:
            assert (
                not result["schematic_update_attempted"] and False not in state["calls"]
            )
    else:
        assert result["ok"] and state["calls"] == [True, False]


def test_candidate_export_never_weakens_public_logical_inspection(
    tmp_path, monkeypatch
):
    from runtime import logical
    from test_logical import SCT, ALLEGRO

    for name in ("SctNetlist", "AllegroNetlist"):
        exe = tmp_path / "wv/win32/bin" / (name + ".exe")
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(name.encode())
    monkeypatch.setenv("SDD_HOME", str(tmp_path))
    diagnostic = {"text": b"Error: Design is not packaged!\r\n"}

    def run(command, **kwargs):
        path = Path(next(v[5:] for v in command if v.startswith("-out=")))
        path.write_text(SCT if "SctNetlist" in command[0] else ALLEGRO)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=diagnostic["text"])

    monkeypatch.setattr(logical.subprocess, "run", run)
    expect(
        "logical_design_unpacked",
        lambda: logical.export_round(tmp_path, "d", tmp_path / "public"),
    )
    graph, evidence = logical.export_round(
        tmp_path, "d", tmp_path / "candidate", _candidate_only=True
    )
    assert graph and all(v["unpackaged_candidate"] for v in evidence.values())
    diagnostic["text"] += b"Error: library mapping failed\r\n"
    expect(
        "logical_export_incomplete",
        lambda: logical.export_round(
            tmp_path, "d", tmp_path / "bad", _candidate_only=True
        ),
    )


@pytest.mark.parametrize("preview", [True, False])
def test_native_packager_uses_fixed_modes_and_private_pdb(
    tmp_path, monkeypatch, preview
):
    from runtime import native_packaging as native

    project = tmp_path / "fixture/test.prj"
    project.parent.mkdir()
    project.write_bytes(b"project")
    pdb = project.parent / "Integration/LocalPartsDB.pdb"
    pdb.parent.mkdir()
    pdb.write_bytes(b"original")
    exe = tmp_path / "wg/win32/bin/package.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"program")
    monkeypatch.setenv("SDD_HOME", str(tmp_path))
    output = tmp_path / "out"

    def run(command, **kwargs):
        assert command[0] == str(exe) and "-Add" in command and "-NoFill" in command
        assert ("-y" in command) == preview and kwargs["cwd"] == project.parent
        target = Path(next(v[2:] for v in command if v.startswith("-p")))
        assert target.is_relative_to(output) and target.read_bytes() == b"original"
        target.write_bytes(b"private updated PDB")
        Path(next(v[2:] for v in command if v.startswith("-b"))).write_bytes(b"")
        message = SUCCESS + (
            "!THE iCDB HAS NOT BEEN UPDATED!" if preview else "!THE iCDB IS UP-TO-DATE!"
        )
        Path(next(v[2:] for v in command if v.startswith("-l"))).write_text(message)
        return SimpleNamespace(returncode=0, stdout=message.encode(), stderr=b"")

    monkeypatch.setattr(native.subprocess, "run", run)
    result = native.run_package(project, "d", output, preview)
    assert result["bam_bytes"] == 0 and pdb.read_bytes() == b"original"
    with pytest.raises(FileExistsError):
        native.run_package(project, "d", output, preview)


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_preflight_is_not_executable_and_retains_unknown_outcome(
    package_context,
    timeout,
):
    service, sid, change, _ = package_context

    def runner(request):
        if timeout:
            raise RuntimeError("native timeout")
        return {
            "ok": False,
            "error": {"kind": "packaging_preflight_changed"},
            "schematic_update_outcome_unknown": True,
        }

    service.runner = runner
    if timeout:
        with pytest.raises(RuntimeError):
            service.preview_packaging(sid, change["preview_id"], "b" * 32)
    else:
        expect(
            "packaging_preflight_changed",
            lambda: service.preview_packaging(sid, change["preview_id"], "b" * 32),
        )
    with service.db() as db:
        row = db.execute("SELECT status,record FROM packaging").fetchone()
    assert row[0] == ("outcome_unknown" if timeout else "failed")
    expect("session_invalidated", lambda: service.sessions.load(sid))
