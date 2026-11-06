import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_schematic import sheet_context
from test_edits import context, expect
from runtime.pin_mapping import parse_part, resolve_reference, validate_refs
from runtime.sessions import PadsError


HEADER = '.Filetype ASCII_PDB\n.Version "02.01"\n.Units mm\n'
PART = """.Number "P1"
..Name "P1"
...Default
..SwapGroup "gate"
...SwapID "a"
...SwapID "power"
..SwapGroup "ground"
...SwapID "g"
...SwapIDProperties "g"
....Prop "Supply", "GND", "Text"
..Symbol "Sample:AMP"
...Default
...Symbol_SwapGroup "gate"
....PinName "IN"
....PinName "VDD"
..Slots
...Slot_SwapGroup "gate"
....SlotID 1
....SwapCode 1
....PinNumber "1"
....PinNumber "8"
...Slot_SwapGroup "gate"
....SlotID 2
....SwapCode 1
....PinNumber "2"
....PinNumber "8"
...Slot_SwapGroup "ground"
....SlotID 3
....SwapCode 0
....PinNumber "4"
"""


def sample(single=False):
    text = HEADER + PART
    if single:
        text = text.replace(
            '...Slot_SwapGroup "gate"\n....SlotID 2\n....SwapCode 1\n....PinNumber "2"\n....PinNumber "8"\n',
            "",
        )
    part = parse_part(text, "P1")
    numbers = ["1", "4", "8"] if single else ["1", "2", "4", "8"]
    graph = {
        "components": [{"refdes": "U1", "pin_numbers": numbers}],
        "nets": [
            {
                "name": {"1": "N1", "2": "N2", "4": "GND", "8": "VDD"}[n],
                "pins": [{"refdes": "U1", "pin_number": n}],
            }
            for n in numbers
        ],
    }
    labels = {"U1": {"1": "IN", "2": "IN", "4": "4", "8": "VDD"}}
    live = []
    for i in range(1, 2 if single else 3):
        live.append(
            {
                "refdes": "U1",
                "part_number": "P1",
                "symbol": "AMP",
                "partition": "Sample",
                "uid": "i" + str(i),
                "path": "block\\i" + str(i),
                "block_path": "block",
                "sheet": "s",
                "pins": [
                    {
                        "uid": "p1",
                        "name": "IN",
                        "number": str(i),
                        "net_uid": "n" + str(i),
                        "net_name": "N" + str(i),
                    },
                    {
                        "uid": "p2",
                        "name": "VDD",
                        "number": "8",
                        "net_uid": "power",
                        "net_name": "PWR",
                    },
                ],
            }
        )
    return part, graph, labels, live


def test_shared_units_and_non_symbol_supply_have_no_unique_live_write_endpoint():
    part, graph, labels, live = sample()
    result = resolve_reference("U1", live, part, graph, labels)
    assert result["status"] == "corroborated" and not result["write_authorized"]
    assert {i["slot_id"] for i in result["instances"]} == {1, 2}
    shared = result["shared_physical_pins"][0]
    assert (
        shared["physical_number"] == "8"
        and len(shared["live_occurrences"]) == 2
        and not shared["single_live_endpoint"]
    )
    hidden = result["non_symbol_supply_pins"][0]
    assert (
        hidden["physical_number"] == "4"
        and hidden["corroborated"]
        and not hidden["live_endpoint_available"]
    )


def test_implicit_number_requires_unique_whole_slot_and_export_label_agreement():
    part, graph, labels, live = sample(single=True)
    for pin in live[0]["pins"]:
        pin["number"] = ""
    result = resolve_reference("U1", live, part, graph, labels)
    assert result["status"] == "corroborated"
    assert {p["number_source"] for p in result["instances"][0]["pins"]} == {
        "unique_library_slot"
    }
    labels["U1"]["1"] = "different"
    result = resolve_reference("U1", live, part, graph, labels)
    assert (
        not result["all_selected_live_instances_corroborated"]
        and "live_library_export_pin_mismatch" in result["issues"]
    )


@pytest.mark.parametrize(
    "change,issue",
    [
        ("blank_signal", "ambiguous_slot"),
        ("wrong_number", "no_compatible_slot"),
        ("wrong_symbol", "no_compatible_slot"),
        ("missing_live_pin", "no_compatible_slot"),
        ("duplicate_slot", "slot_used_by_multiple_instances"),
        ("supply_net", "non_symbol_supply_unresolved"),
        ("declared_pin_missing", "pdb_export_pin_set_mismatch"),
        ("connection_presence", "live_library_export_pin_mismatch"),
        ("wire_conflict", "physical_net_mapping_conflict"),
        ("part_number", "conflicting_effective_part_numbers"),
    ],
)
def test_ambiguity_and_conflicts_are_reports_not_guessed_assignments(change, issue):
    part, graph, labels, live = sample()
    if change == "blank_signal":
        live[0]["pins"][0]["number"] = ""
    if change == "wrong_number":
        live[0]["pins"][0]["number"] = "99"
    if change == "wrong_symbol":
        live[0]["symbol"] = "OTHER"
    if change == "missing_live_pin":
        live[0]["pins"].pop()
    if change == "duplicate_slot":
        live[1]["pins"][0]["number"] = "1"
    if change == "supply_net":
        next(n for n in graph["nets"] if n["name"] == "GND")["name"] = "OTHER"
    if change == "declared_pin_missing":
        graph["components"][0]["pin_numbers"].remove("4")
    if change == "connection_presence":
        live[0]["pins"][0]["net_uid"] = None
    if change == "wire_conflict":
        live[1]["pins"][0]["net_uid"] = "n1"
    if change == "part_number":
        live[0]["part_number"] = "OTHER"
    result = resolve_reference("U1", live, part, graph, labels)
    assert (
        issue in result["issues"]
        and result["status"] == "needs_review"
        and not result["write_authorized"]
    )
    if change == "blank_signal":
        first = next(i for i in result["instances"] if i["component_uid"] == "i1")
        assert (
            first["candidate_slots"] == [1, 2]
            and first["pins"] == []
            and first["observed_pins"]
        )


def test_same_local_wire_uid_in_different_hierarchy_blocks_does_not_merge():
    part, graph, labels, live = sample()
    live[1]["block_path"] = "other"
    live[1]["path"] = "other\\i2"
    live[1]["pins"][0]["net_uid"] = "n1"
    assert (
        resolve_reference("U1", live, part, graph, labels)["status"] == "corroborated"
    )


@pytest.mark.parametrize(
    "text",
    [
        (HEADER + PART).replace('"02.01"', '"99.00"'),
        HEADER + PART + PART,
        (HEADER + PART).replace("....SlotID 1", "....SlotID 1\n....SlotID 1"),
        (HEADER + PART).replace("....SlotID 2", "....SlotID 1"),
        (HEADER + PART).replace('....PinNumber "2"\n', ""),
        (HEADER + PART).replace('....PinName "IN"', '....PinName "VDD"'),
        (HEADER + PART).replace('....PinNumber "2"', '....PinNumber "8"'),
        (HEADER + PART).replace('...SwapID "a"', '...SwapID "power"'),
        (HEADER + PART).replace("..Slots", "..Unknown"),
        (HEADER + PART).replace("..Slots", "....Slots"),
        (HEADER + PART).replace('...SwapID "a"', '...SwapID "a"\n....Unsupported "x"'),
        (HEADER + PART).replace(
            ".Filetype ASCII_PDB", '.Filetype ASCII_PDB\n..Unsupported "x"'
        ),
        (HEADER + PART).replace('"P1"', '"P2"', 1),
        HEADER + PART + "\x00",
    ],
)
def test_unsupported_truncated_duplicate_or_conflicting_pdb_is_rejected(text):
    with pytest.raises(PadsError):
        parse_part(text, "P1")


@pytest.mark.parametrize(
    "refs", [[], ["U1"] * 2, ["U1"] * 5, [True], ["bad ref"], "U1"]
)
def test_queries_are_exact_bounded_reference_lists(refs):
    expect("invalid_mapping_query", lambda: validate_refs(refs))


def test_mapping_baseline_scope_source_change_and_failures(sheet_context):
    service, sid, _, _, _, _ = sheet_context
    value = {
        "ok": True,
        "mapping": [{"status": "needs_review"}],
        "parts": {},
        "provenance": {"format": "test", "pdb": "one"},
        "all_selected_live_instances_corroborated": False,
    }
    service.runner = lambda request: copy.deepcopy(value)
    baseline = service.inspect_pin_mapping(sid, "d", ["U1"])
    same = service.inspect_pin_mapping(sid, "d", ["U1"], baseline["check_id"])
    assert (
        same["comparison"]["mapping_equal"]
        and not same["all_selected_live_instances_corroborated"]
    )
    expect(
        "check_scope_mismatch",
        lambda: service.inspect_pin_mapping(sid, "d", ["U2"], baseline["check_id"]),
    )
    value["provenance"]["pdb"] = "changed"
    expect(
        "mapping_baseline_changed",
        lambda: service.inspect_pin_mapping(sid, "d", ["U1"], baseline["check_id"]),
    )
    service.runner = lambda request: {
        "ok": False,
        "error": {"kind": "mapping_snapshot_changed"},
    }
    expect(
        "mapping_snapshot_changed",
        lambda: service.inspect_pin_mapping(sid, "d", ["U1"]),
    )
    with service.db() as db:
        assert db.execute("SELECT count(*) FROM mapping_checks").fetchone()[0] == 2


@pytest.mark.parametrize(
    "fault", [None, "diagnostic", "missing", "truncated", "wrong_part", "exit_code"]
)
def test_native_part_export_requires_readable_valid_output_even_on_exit_one(
    tmp_path, monkeypatch, fault
):
    from runtime import native_pin_mapping as native

    for tool in ("PartsDB2HKP", "DataConvert"):
        exe = tmp_path / "common/win32/_bin" / (tool + ".exe")
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(tool.encode())
    monkeypatch.setenv("SDD_HOME", str(tmp_path))
    pdb = tmp_path / "private.pdb"
    pdb.write_bytes(b"original")

    def run(command, **kwargs):
        output = Path(command[command.index("-o") + 1])
        converter = Path(command[0]).stem == "DataConvert"
        if not converter:
            assert command[command.index("-n") + 1] == "P1" and command[
                command.index("-i") + 1
            ] == str(pdb)
        text = HEADER + PART if converter else "native"
        if converter and fault == "truncated":
            text = text[:30]
        if converter and fault == "wrong_part":
            text = text.replace('"P1"', '"other"', 1)
        if not (converter and fault == "missing"):
            output.write_text(text)
        return SimpleNamespace(
            returncode=7 if fault == "exit_code" else (1 if converter else 0),
            stdout=b"",
            stderr=b"Error" if fault == "diagnostic" else b"",
        )

    monkeypatch.setattr(native.subprocess, "run", run)
    if fault:
        with pytest.raises(PadsError):
            native.export_part(pdb, "P1", tmp_path / "out")
    else:
        part, evidence = native.export_part(pdb, "P1", tmp_path / "out")
        assert (
            part["part_number"] == "P1"
            and evidence["tools"]["DataConvert"]["exit_code"] == 1
        )
        assert pdb.read_bytes() == b"original"


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "live",
        "graph",
        "labels",
        "source",
        "copy",
        "protected",
        "identity",
        "database",
        "library",
        "netlist_tool",
        "part_tool",
    ],
)
def test_native_inspection_rejects_drift_before_recording_mapping(
    tmp_path, monkeypatch, fault
):
    from runtime import native_pin_mapping as native

    part, graph, labels, live = sample()
    root = tmp_path / "fixture"
    root.mkdir()
    project = root / "sample.prj"
    project.write_text("LIST PDBs\nENDLIST\n")
    (root / "Integration").mkdir()
    pdb = root / "Integration/LocalPartsDB.pdb"
    pdb.write_bytes(b"source")
    database, library = root / "database", root / "Library.lmc"
    database.mkdir()
    library.write_bytes(b"library")
    base = tmp_path / "sdd/common/win32/_bin"
    base.mkdir(parents=True)
    exe = base / "PartsDB2HKP.exe"
    exe.write_bytes(b"exporter")
    monkeypatch.setenv("SDD_HOME", str(tmp_path / "sdd"))
    data = SimpleNamespace(iCDBDir=str(database), CentralLibraryPath=str(library))
    monkeypatch.setattr(
        native,
        "call",
        lambda obj, method, *args: {
            "GetProjectData": data,
            "GetiCDBDesigns": "designs",
            "GetCount": 1,
            "GetItem": "CORPORATE",
        }[method],
    )
    calls = {"live": 0, "export": 0, "protected": 0}

    def read(*args):
        calls["live"] += 1
        result = copy.deepcopy(live)
        if fault == "live" and calls["live"] == 2:
            result[0]["pins"][0]["net_uid"] = "changed"
        return result

    def protection(*args):
        calls["protected"] += 1
        return {
            "file": (
                "changed"
                if fault == "protected" and calls["protected"] == 2
                else "original"
            )
        }

    def export_part(*args):
        return part, {"tools": {"PartsDB2HKP": {"sha256": native.fingerprint(exe)}}}

    def export_graph(db, design, output):
        calls["export"] += 1
        output.mkdir()
        sct = output / "sample.sct"
        sct.write_text(str(calls["export"]))
        current = copy.deepcopy(graph)
        if calls["export"] == 2:
            if fault == "graph":
                current["nets"] = []
            if fault == "source":
                pdb.write_bytes(b"changed")
            if fault == "copy":
                (tmp_path / "output/project-parts.pdb").write_bytes(b"changed")
            if fault == "database":
                data.iCDBDir = str(root)
            if fault == "library":
                data.CentralLibraryPath = str(project)
            if fault == "part_tool":
                exe.write_bytes(b"changed")
        return current, {
            "SctNetlist": {
                "output": str(sct),
                "executable_sha256": (
                    "changed"
                    if fault == "netlist_tool" and calls["export"] == 2
                    else "tool"
                ),
            }
        }

    monkeypatch.setattr(native, "read_instances", read)
    monkeypatch.setattr(native, "protected_files", protection)
    monkeypatch.setattr(native, "export_part", export_part)
    monkeypatch.setattr(native, "export_round", export_graph)
    monkeypatch.setattr(
        native,
        "parse_sct",
        lambda text: {
            "pin_labels": {} if fault == "labels" and text == "2" else labels
        },
    )
    request = {
        "refdes": ["U1"],
        "design": "CORPORATE",
        "policy": {"fixture": str(root)},
        "expected_path": str(project),
        "output_dir": str(tmp_path / "output"),
    }
    identity = lambda: {"instance": "changed" if fault == "identity" else "original"}
    if fault:
        with pytest.raises(PadsError) as exc:
            native.inspect(None, request, identity, {"instance": "original"})
        assert exc.value.kind in ("mapping_snapshot_changed", "mapping_source_changed")
    else:
        result = native.inspect(None, request, identity, {"instance": "original"})
        assert result["all_selected_live_instances_corroborated"]
        assert (
            not result["write_authorized"]
            and not result["full_design_pin_mapping_verified"]
        )
