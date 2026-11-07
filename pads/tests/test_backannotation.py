import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from test_edits import context, expect
from runtime.backannotation import parse_bam, impact, ATTRIBUTES
from runtime.backannotation_service import Backannotation
from runtime.packaging import Packaging
from runtime.sessions import PadsError

BAM = """/* Sym: A123  Path 1: d Page 1: d(s) */
CHANGE 'd(s)' INSTANCE
 SELECT COMP $1I1
  PARAM 'Part Name' = 'res'
  PARAM 'Part Label' = 'res'
  PARAM 23 = DELETE /* Component Fix flag */
  PARAM 15 = 'R2' /* Reference Desig was R1 */
  PIN_PARAM 'A' 4 = '2' /* was: 1 */
"""
SUCCESS = "Packaging has been successfully tested with no errors or warnings.\nPackager finished successfully.\n!THE iCDB HAS NOT BEEN UPDATED!\nThere have been 1 reference designator changes and 1 pin\n number changes."


def sample():
    live = [
        {
            "scope": "d(s)",
            "uid": "$1I1",
            "path": "$1I1",
            "refdes": "R1",
            "attributes": {
                k: {"base": "res", "instance": "", "effective": "res"}
                for k in ATTRIBUTES
            },
        }
    ]
    pins = [
        {
            "scope": "d(s)",
            "uid": "$1I1",
            "pin_uid": "$1P1",
            "name": "A",
            "number": "1",
            "net_uid": "wire",
        }
    ]
    graph = {
        "components": [
            {"refdes": "R1", "pin_numbers": ["1", "3"], "repeated_pin_declarations": []}
        ],
        "nets": [
            {"name": "N", "pins": [{"refdes": "R1", "pin_number": "1"}]},
            {"name": "PWR", "pins": [{"refdes": "R1", "pin_number": "3"}]},
        ],
    }
    return live, pins, graph


def test_exact_native_subset_is_data_not_an_executable_preview():
    records = parse_bam(BAM)
    assert parse_bam(BAM.replace("A123", "DEAD")) == records
    value = impact(records, *sample())
    assert value["summary"]["reference_changes"] == value["summary"]["pin_changes"] == 1
    assert value["network_impact"]["membership_changes"] == [
        {
            "net": "N",
            "before": {"refdes": "R1", "pin_number": "1"},
            "proposed": {"refdes": "R2", "pin_number": "2"},
        },
        {
            "net": "PWR",
            "before": {"refdes": "R1", "pin_number": "3"},
            "proposed": {"refdes": "R2", "pin_number": "3"},
        },
    ]
    assert (
        not value["execution_allowed"]
        and not value["recovery"]["automatic_inverse_available"]
    )
    assert "fixed_flag_preimage_unverified" in value["recovery"]["blockers"]
    assert value["summary"]["effective_attribute_changes"] == 0


@pytest.mark.parametrize(
    "text",
    [
        BAM + "EXEC 'script'",
        BAM + "SELECT NET xyz",
        BAM.replace("INSTANCE", "ALL"),
        BAM.replace("PARAM 23 = DELETE", "PARAM 23 = '1'"),
        BAM.replace("PIN_PARAM 'A' 4", "PIN_PARAM 'A' 7"),
        BAM.replace("'Part Name'", "'Arbitrary'"),
        BAM.replace("/* was: 1 */", ""),
        BAM.replace("/* Reference Desig was R1 */", ""),
        BAM + "PIN_PARAM 'A' 4 = '2' /* was: 1 */",
        BAM + "PARAM 15 = 'R3' /* Reference Desig was R1 */",
        BAM + "PARAM 'Part Name' = 'other'",
        BAM + "PARAM 23 = DELETE",
        BAM + "CHANGE 'd(s)' INSTANCE",
        BAM + BAM,
        "SELECT COMP $1I1",
        "CHANGE 'd(s)' INSTANCE\nSELECT COMP $1I1",
        BAM + "\x00",
        BAM.replace("'R2'", "'R 2'"),
        BAM.replace("'2' /* was", "'2;evil' /* was"),
    ],
)
def test_unknown_incomplete_or_duplicate_bam_fails_closed(text):
    with pytest.raises(PadsError):
        parse_bam(text)


@pytest.mark.parametrize(
    "fault",
    [
        "target",
        "duplicate_identity",
        "ref",
        "pin_number",
        "pin_name",
        "duplicate_pin",
        "declared_pin",
        "connected",
    ],
)
def test_preimage_is_corroborated_not_trusted_from_comments(fault):
    live, pins, graph = sample()
    if fault == "target":
        live[0]["scope"] = "another(s)"
    if fault == "duplicate_identity":
        live.append(copy.deepcopy(live[0]))
    if fault == "ref":
        live[0]["refdes"] = "R99"
    if fault == "pin_number":
        pins[0]["number"] = "99"
    if fault == "pin_name":
        pins[0]["name"] = "other"
    if fault == "duplicate_pin":
        pins.append(copy.deepcopy(pins[0]))
    if fault == "declared_pin":
        graph["components"][0]["pin_numbers"] = []
    if fault == "connected":
        pins[0]["net_uid"] = None
    with pytest.raises(PadsError):
        impact(parse_bam(BAM), live, pins, graph)


def test_package_split_and_shared_pins_remain_explicitly_unresolved():
    live, pins, graph = sample()
    other = copy.deepcopy(live[0])
    other.update(uid="$1I2", path="$1I2")
    live.append(other)
    value = impact(parse_bam(BAM), live, pins, graph)
    assert value["package_changes"][0]["split"]
    assert (
        value["network_impact"]["unresolved_members"][0]["before"]["pin_number"] == "3"
    )
    graph["components"][0]["repeated_pin_declarations"] = ["1"]
    value = impact(parse_bam(BAM), live, pins, graph)
    assert len(value["network_impact"]["unresolved_members"]) == 2
    assert not value["network_impact"]["complete"]


def test_package_merge_and_network_pin_collision_block_recovery_claim():
    live, pins, graph = sample()
    other = copy.deepcopy(live[0])
    other.update(uid="$1I2", path="$1I2", refdes="R2")
    live.append(other)
    graph["components"].append({"refdes": "R2", "pin_numbers": ["2"]})
    graph["nets"].append(
        {"name": "other", "pins": [{"refdes": "R2", "pin_number": "2"}]}
    )
    value = impact(parse_bam(BAM), live, pins, graph)
    assert value["package_merges"] == [{"proposed": "R2", "before": ["R1", "R2"]}]
    assert value["network_impact"]["pin_collisions"][0]["nets"] == ["N", "other"]


def test_empty_native_proposal_needs_no_recovery_and_does_not_enable_writes():
    value = impact(parse_bam(""), *sample())
    assert value["recovery"]["status"] == "not_needed"
    assert (
        not value["execution_allowed"]
        and not value["network_impact"]["membership_changes"]
    )


def test_verified_absence_and_empty_overrides_remove_only_preimage_blockers():
    from runtime.attribute_state import parse_batch, property_state, FROZEN

    live, pins, graph = sample()
    live[0]["frozen"] = property_state({}, {}, FROZEN)
    live[0]["attributes"] = {
        name: property_state(
            parse_batch(f"0 {name}=res\r"), parse_batch(f"0 {name}=\r"), name
        )
        for name in ATTRIBUTES
    }
    value = impact(parse_bam(BAM), live, pins, graph)
    assert value["recovery"]["blockers"] == ["execution_and_inverse_not_implemented"]
    assert (
        not value["execution_allowed"]
        and not value["recovery"]["automatic_inverse_available"]
    )
    assert all(
        a["before"]["instance_present"] and a["before"]["instance"] == ""
        for a in value["attribute_assignments"]
    )


def test_exported_pin_name_must_agree_when_native_labels_are_provided():
    with pytest.raises(PadsError):
        impact(parse_bam(BAM), *sample(), {"R1": {"1": "other"}})
    assert (
        impact(parse_bam(BAM), *sample(), {"R1": {"1": "A"}})["summary"]["pin_changes"]
        == 1
    )


def test_service_baselines_never_enter_executable_packaging_table(context):
    original, sid, *_ = context
    service = Backannotation(original.sdd, original.state, original.sessions)
    value = {
        "ok": True,
        "impact": impact(parse_bam(BAM), *sample()),
        "provenance": {"source": "one"},
        "live_baseline": [],
        "netlist_baseline": {},
        "executable_preview": False,
    }
    service.runner = lambda request: copy.deepcopy(value)
    record = service.preview_impact(sid, "d", "repackage_unfixed")
    compared = service.preview_impact(
        sid, "d", "repackage_unfixed", record["impact_id"]
    )
    assert all(compared["comparison"].values())
    expect(
        "check_scope_mismatch",
        lambda: service.preview_impact(
            sid, "d", "repackage_unfixed", record["impact_id"], "s"
        ),
    )
    expect(
        "invalid_backannotation_scope",
        lambda: service.preview_impact(sid, "d", "add", scope_sheet="s"),
    )
    expect(
        "check_scope_mismatch",
        lambda: service.preview_impact(sid, "d", "add", record["impact_id"]),
    )
    expect(
        "preview_not_ready",
        lambda: Packaging(
            original.sdd, original.state, original.sessions
        ).execute_packaging(record["impact_id"]),
    )
    value["provenance"]["source"] = "changed"
    expect(
        "backannotation_baseline_changed",
        lambda: service.preview_impact(
            sid, "d", "repackage_unfixed", record["impact_id"]
        ),
    )


@pytest.mark.parametrize("fault", ["error", "exception"])
def test_uncertain_preflight_preserves_failed_record_and_invalidates_session(
    context, fault
):
    original, sid, *_ = context
    service = Backannotation(original.sdd, original.state, original.sessions)

    def run(request):
        if fault == "exception":
            raise RuntimeError("lost worker")
        return {
            "ok": False,
            "error": {"kind": "backannotation_snapshot_changed"},
            "schematic_update_outcome_unknown": True,
        }

    service.runner = run
    if fault == "exception":
        with pytest.raises(RuntimeError):
            service.preview_impact(sid, "d")
    else:
        result = service.preview_impact(sid, "d")
        assert not result["ok"] and result["schematic_update_outcome_unknown"]
    expect("session_invalidated", lambda: original.sessions.load(sid))
    with service.db() as db:
        assert (
            db.execute(
                "select count(*) from backannotation_impacts where status != 'inspected'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "diagnostic",
        "unexpected_write",
        "missing",
        "count_mismatch",
        "source_changed",
        "tool_changed",
        "timeout",
        "invalid_mode",
        "ref_only",
    ],
)
def test_native_command_is_always_bam_only_and_requires_complete_evidence(
    tmp_path, monkeypatch, fault
):
    from runtime import native_backannotation as native

    project = tmp_path / "sample.prj"
    project.write_text("project")
    pdb = tmp_path / "Integration/LocalPartsDB.pdb"
    pdb.parent.mkdir()
    pdb.write_bytes(b"source")
    exe = tmp_path / "sdd/wg/win32/bin/package.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"tool")
    monkeypatch.setenv("SDD_HOME", str(tmp_path / "sdd"))

    def run(command, **kwargs):
        assert (
            "-y" in command
            and "-r" in command
            and "-Add" in command
            and "-NoFill" in command
        )
        assert "-i" not in command and kwargs["cwd"] == project.parent
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, 40)
        bam = Path(next(a[2:] for a in command if a.startswith("-b")))
        log = Path(next(a[2:] for a in command if a.startswith("-l")))
        if fault != "missing":
            bam.write_text(BAM.split("  PIN_PARAM")[0] if fault == "ref_only" else BAM)
        message = SUCCESS
        if fault == "ref_only":
            message = message.replace(" and 1 pin\n number changes", "")
        if fault == "diagnostic":
            message += "\nWARNING: failed"
        if fault == "unexpected_write":
            message += "\nThe Common DataBase has been successfully updated."
        if fault == "count_mismatch":
            message = message.replace("been 1", "been 2")
        if fault == "source_changed":
            pdb.write_bytes(b"changed")
        if fault == "tool_changed":
            exe.write_bytes(b"changed")
        log.write_text(message)
        return SimpleNamespace(returncode=0, stdout=message.encode(), stderr=b"")

    monkeypatch.setattr(native.subprocess, "run", run)
    if fault and fault != "ref_only":
        with pytest.raises(PadsError):
            native.run_proposal(
                project,
                "d",
                tmp_path / "out",
                "bad" if fault == "invalid_mode" else "repackage_unfixed",
            )
    else:
        records, result = native.run_proposal(
            project, "d", tmp_path / "out", "repackage_unfixed"
        )
        expected_bam = BAM.split("  PIN_PARAM")[0] if fault == "ref_only" else BAM
        assert records == parse_bam(expected_bam) and result["bam_only"]
        assert pdb.read_bytes() == b"source"


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "variant",
        "live",
        "pins",
        "graph",
        "labels",
        "protected",
        "identity",
        "database",
        "library",
        "tool",
        "source",
        "exporter",
        "scope_valid",
        "scope_escape_first",
        "scope_escape_second",
    ],
)
def test_native_snapshot_drift_rejects_but_proposal_variants_are_reported(
    tmp_path, monkeypatch, fault
):
    from runtime import native_backannotation as native

    root = tmp_path / "fixture"
    root.mkdir()
    project = root / "sample.prj"
    project.write_text("LIST PDBs\nENDLIST\n")
    pdb = root / "Integration/LocalPartsDB.pdb"
    pdb.parent.mkdir()
    pdb.write_bytes(b"pdb")
    database = root / "db"
    database.mkdir()
    library = root / "Library.lmc"
    library.write_bytes(b"lib")
    exe = tmp_path / "sdd/wg/win32/bin/package.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"tool")
    monkeypatch.setenv("SDD_HOME", str(tmp_path / "sdd"))
    digest = native.fingerprint(exe)
    data = SimpleNamespace(iCDBDir=str(database), CentralLibraryPath=str(library))
    monkeypatch.setattr(
        native,
        "call",
        lambda obj, method, *args: {
            "GetProjectData": data,
            "GetiCDBDesigns": "designs",
            "GetCount": 1,
            "GetItem": "d",
        }[method],
    )
    calls = {"read": 0, "proposal": 0, "graph": 0, "protected": 0}
    live, pins, graph = sample()
    if fault and fault.startswith("scope_"):
        from runtime.attribute_state import property_state, FROZEN

        live[0]["frozen"] = property_state({}, {}, FROZEN)

    def read(*args):
        calls["read"] += 1
        rows, selected = copy.deepcopy(live), copy.deepcopy(pins)
        if calls["read"] == 3:
            if fault == "live":
                rows[0]["refdes"] = "other"
            if fault == "pins":
                selected[0]["number"] = "other"
        return rows, selected

    def proposal(*args):
        calls["proposal"] += 1
        records = parse_bam(BAM)
        if fault == "variant" and calls["proposal"] == 2:
            records[0]["refdes"]["after"] = "R3"
        if (fault == "scope_escape_first" and calls["proposal"] == 1) or (
            fault == "scope_escape_second" and calls["proposal"] == 2
        ):
            records[0]["scope"] = "d(other)"
        return records, {
            "executable_sha256": digest,
            "source_pdb_sha256": (
                "changed" if fault == "source" and calls["proposal"] == 2 else "pdb"
            ),
        }

    def export(db, design, output):
        calls["graph"] += 1
        output.mkdir(parents=True)
        sct = output / "sct.txt"
        sct.write_text(str(calls["graph"]))
        current = copy.deepcopy(graph)
        if calls["graph"] == 2:
            if fault == "graph":
                current["nets"] = []
            if fault == "database":
                data.iCDBDir = str(root)
            if fault == "library":
                data.CentralLibraryPath = str(project)
            if fault == "tool":
                exe.write_bytes(b"changed")
        return current, {
            "SctNetlist": {
                "output": str(sct),
                "executable_sha256": (
                    "changed" if fault == "exporter" and calls["graph"] == 2 else "tool"
                ),
            }
        }

    def protected(*args):
        calls["protected"] += 1
        return {
            "file": (
                "changed"
                if fault == "protected" and calls["protected"] == 2
                else "original"
            )
        }

    monkeypatch.setattr(native, "read_state", read)
    monkeypatch.setattr(native, "run_proposal", proposal)
    monkeypatch.setattr(native, "export_round", export)
    monkeypatch.setattr(native, "protected_files", protected)
    monkeypatch.setattr(
        native,
        "parse_sct",
        lambda text: {
            "pin_labels": {
                "R1": {"1": "other" if fault == "labels" and text == "2" else "A"}
            }
        },
    )
    request = {
        "policy": {"fixture": str(root)},
        "expected_path": str(project),
        "design": "d",
        "mode": "repackage_unfixed",
        "output_dir": str(tmp_path / "out"),
    }
    if fault and fault.startswith("scope_"):
        request["scope_sheet"] = "s"
    result = native.preview(
        None,
        request,
        lambda: "changed" if fault == "identity" and calls["proposal"] else "original",
        "original",
    )
    if fault not in (None, "variant", "scope_valid"):
        assert not result["ok"] and result["schematic_update_outcome_unknown"]
        if fault.startswith("scope_escape"):
            assert result["error"]["kind"] == "backannotation_scope_escape"
        assert (
            result["project_write_attempted"]
            and not result["schematic_update_attempted"]
        )
    else:
        assert result["ok"] and result["repeatability"]["identical"] == (
            fault != "variant"
        )
        if fault == "variant":
            assert (
                "native_proposal_not_repeatable"
                in result["impact"]["recovery"]["blockers"]
            )
            assert len(result["repeatability"]["changed_instances"]) == 1
