import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.logical import (
    graph,
    parse_sct,
    parse_allegro,
    validate_pins,
    export_round,
    compare_reports,
)
from runtime.native_logical import resolve_membership, read_selected
from runtime.sessions import PadsError
from test_schematic import sheet_context
from test_edits import context


SCT = """BEGIN_COMPPROPS
R1:'part','part','',''
R2:'part','part','',''
END_COMPPROPS
BEGIN_COMPPINS
R1((1:A),(2:B))
R2((1:A),(2:B))
END_COMPPINS
BEGIN_E-NETS
LOCAL_A:LOCAL_A
LOCAL_B:LOCAL_B
END_E-NETS
BEGIN_BUSES
END_BUSES
BEGIN_NETS
LOCAL_A:R1(1:A);
LOCAL_B:R2(1:A);
END_NETS
BEGIN_GROUND
END_GROUND
BEGIN_POWER
END_POWER
"""
ALLEGRO = """$PACKAGES
'cell'; R1 R2
$A_PROPERTIES
$NETS
'LOCAL_A' ; R1.1
'LOCAL_B' ; R2.1
$PINS
$A_PROPERTIES
$END
"""


def test_formats_agree_and_unused_pins_are_preserved():
    value = graph(SCT, ALLEGRO)
    assert len(value["nets"]) == 2
    assert value["components"][0]["pin_numbers"] == ["1", "2"]
    with pytest.raises(PadsError):
        graph(SCT, ALLEGRO.replace("R1.1", "R1.2"))


@pytest.mark.parametrize(
    "text",
    [
        SCT.replace("END_POWER", ""),
        SCT + "extra",
        SCT.replace("END_NETS", "END_POWER"),
        SCT.replace("R1(1:A);", "R9(1:A);"),
        SCT.replace("LOCAL_B:R2(1:A);", "LOCAL_B:R1(1:A);"),
        SCT.replace("LOCAL_A:R1(1:A);", "LOCAL_A:R1(1:A),R1(1:A);"),
        SCT.replace("LOCAL_A:R1(1:A);", "LOCAL_A:R1(1:A)"),
        SCT.replace("R1((1:A),(2:B))", "R1((1:A),(1:B))"),
        SCT.replace("BEGIN_NETS", "BEGIN_UNKNOWN"),
        SCT.replace("R1((1:A),(2:B))", ""),
        SCT.replace("LOCAL_B:R2(1:A);", "LOCAL_A:R2(1:A);"),
    ],
)
def test_incomplete_or_ambiguous_sct_is_refused(text):
    with pytest.raises(PadsError):
        parse_sct(text)


def test_repeated_identical_power_declarations_are_exposed_not_extra_pins():
    value = graph(SCT.replace("R1((1:A),(2:B))", "R1((1:A),(2:B),(2:B))"), ALLEGRO)
    assert value["components"][0]["pin_numbers"] == ["1", "2"]
    assert value["components"][0]["repeated_pin_declarations"] == ["2"]


@pytest.mark.parametrize(
    "text",
    [
        ALLEGRO.replace("$END", ""),
        ALLEGRO + "partial",
        ALLEGRO.replace("R2.1", "R1.1"),
        ALLEGRO.replace("'LOCAL_A' ; R1.1", "R1.1"),
        ALLEGRO.replace("'LOCAL_B'", "'LOCAL_A'"),
        ALLEGRO.replace("R1.1", "invalid"),
    ],
)
def test_incomplete_or_ambiguous_allegro_is_refused(text):
    with pytest.raises(PadsError):
        parse_allegro(text)


def live(ref, path, uid="same-local-net", number="1"):
    return {
        "refdes": ref,
        "pin_number": number,
        "block_path": path,
        "sheet": "child",
        "physical_net_uid": uid,
        "physical_net_name": "same-local-name",
    }


def test_same_local_uid_and_name_do_not_merge_hierarchical_instances():
    result = resolve_membership(
        graph(SCT, ALLEGRO), [live("R1", "left"), live("R2", "right")]
    )
    assert {r["logical_net_name"] for r in result} == {"LOCAL_A", "LOCAL_B"}
    # If the live physical identity is actually identical, conflicting exports must fail.
    with pytest.raises(PadsError):
        resolve_membership(
            graph(SCT, ALLEGRO), [live("R1", "left"), live("R2", "left")]
        )
    free = resolve_membership(graph(SCT, ALLEGRO), [live("R1", "left", None, "2")])
    assert free[0]["mapping_status"] == "unconnected"
    with pytest.raises(PadsError):
        resolve_membership(graph(SCT, ALLEGRO), [live("R1", "left", None)])


@pytest.mark.parametrize(
    "pins",
    [
        [],
        [{"refdes": "R1", "pin_number": "1", "command": "x"}],
        [{"refdes": "R1", "pin_number": "1"}] * 2,
        [{"refdes": "R1", "pin_number": True}],
        [{"refdes": "R1", "pin_number": "1;evil"}],
    ],
)
def test_pin_query_is_bounded_and_exact(pins):
    with pytest.raises(PadsError):
        validate_pins(pins)


def test_live_ambiguous_unit_pin_is_refused(monkeypatch):
    from runtime import native_logical

    comp = SimpleNamespace(Refdes="R1", UID="c", Parent=SimpleNamespace(SheetNum="s"))
    pin = SimpleNamespace(Number="1", Pin=SimpleNamespace(UID="p"))
    conn = SimpleNamespace(CompPin=pin, Net=None)

    def call(obj, method, *args):
        if method == "DesignComponents":
            return [comp]
        if method == "GetConnections":
            return [conn, conn]
        if method == "GetName":
            return "instance"

    monkeypatch.setattr(native_logical, "call", call)
    with pytest.raises(PadsError) as exc:
        read_selected(object(), "d", [{"refdes": "R1", "pin_number": "1"}])
    assert exc.value.kind == "logical_pin_not_unique"


@pytest.mark.parametrize("diagnostics", [False, True, "unpacked"])
def test_native_export_fresh_files_and_exit_zero_diagnostics(
    tmp_path, monkeypatch, diagnostics
):
    from runtime import logical

    sdd = tmp_path / "sdd"
    for name in ("SctNetlist", "AllegroNetlist"):
        file = sdd / "wv/win32/bin" / (name + ".exe")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(name.encode())
    monkeypatch.setenv("SDD_HOME", str(sdd))

    def run(command, **kwargs):
        assert "-icdbsnapshot=DxD" in command and "-flowtype=DX" in command
        output = Path(next(a[5:] for a in command if a.startswith("-out=")))
        assert not output.exists()
        output.write_text(SCT if Path(command[0]).stem == "SctNetlist" else ALLEGRO)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                (
                    b"Error: Design is not packaged!"
                    if diagnostics == "unpacked"
                    else b"Error: incomplete"
                )
                if diagnostics
                else b""
            ),
            stderr=b"",
        )

    monkeypatch.setattr(logical.subprocess, "run", run)
    if diagnostics:
        with pytest.raises(PadsError) as exc:
            export_round(tmp_path, "d", tmp_path / "out")
        assert exc.value.kind == (
            "logical_design_unpacked"
            if diagnostics == "unpacked"
            else "logical_export_incomplete"
        )
    else:
        value, evidence = export_round(tmp_path, "d", tmp_path / "out")
        assert len(value["nets"]) == 2 and len(evidence) == 2
        with pytest.raises(FileExistsError):
            export_round(tmp_path, "d", tmp_path / "out")


def test_electrical_equivalence_section_never_merges_physical_nets():
    combined = SCT.replace(
        "LOCAL_A:LOCAL_A\nLOCAL_B:LOCAL_B", "THROUGH_RESISTOR:LOCAL_A,LOCAL_B"
    )
    assert graph(combined, ALLEGRO) == graph(SCT, ALLEGRO)


@pytest.mark.parametrize(
    "change", [None, "graph", "live", "library", "exporter", "database"]
)
def test_native_inspection_refuses_changes_between_observations(
    tmp_path, monkeypatch, change
):
    from runtime import native_logical

    database = tmp_path / "database"
    database.mkdir()
    library = tmp_path / "library.lmc"
    library.write_bytes(b"original")
    data = SimpleNamespace(iCDBDir=str(database), CentralLibraryPath=str(library))

    def call(obj, method, *args):
        if method == "GetProjectData":
            return data
        if method == "GetiCDBDesigns":
            return object()
        if method == "GetCount":
            return 1
        if method == "GetItem":
            return "d"

    monkeypatch.setattr(native_logical, "call", call)
    reads = []

    def read(*args):
        result = [live("R1", "left")]
        if reads and change == "live":
            result[0]["physical_net_uid"] = "changed"
        reads.append(True)
        return result

    monkeypatch.setattr(native_logical, "read_selected", read)
    exports = []

    def export(db, design, output):
        output.mkdir(parents=True)
        value = graph(SCT, ALLEGRO)
        sha = "same"
        if exports:
            if change == "graph":
                value["nets"][0]["name"] = "changed"
            if change == "library":
                library.write_bytes(b"changed")
            if change == "exporter":
                sha = "changed"
            if change == "database":
                data.iCDBDir = str(tmp_path / "other")
        exports.append(True)
        return value, {"exporter": {"executable_sha256": sha}}

    monkeypatch.setattr(native_logical, "export_round", export)
    request = {
        "pins": [{"refdes": "R1", "pin_number": "1"}],
        "design": "d",
        "output_dir": str(tmp_path / "out"),
        "policy": {"fixture": str(tmp_path)},
    }
    if change:
        with pytest.raises(PadsError) as exc:
            native_logical.inspect(object(), request, lambda: {}, {})
        assert exc.value.kind == "logical_snapshot_changed"
        assert not (tmp_path / "out/result.json").exists()
    else:
        result = native_logical.inspect(object(), request, lambda: {}, {})
        assert (
            result["selected_pin_mapping_verified"]
            and not result["full_design_pin_mapping_verified"]
        )


def test_baseline_scope_and_failed_inspection_not_recorded(sheet_context):
    service, sid, _, calls, _, _ = sheet_context
    pins = [{"refdes": "R1", "pin_number": "1"}]
    value = {
        "ok": True,
        "provenance": {"format": "test"},
        "netlist": graph(SCT, ALLEGRO),
        "resolved_pins": [],
    }
    service.runner = lambda request: copy.deepcopy(value)
    baseline = service.inspect_logical(sid, "d", pins)
    same = service.inspect_logical(sid, "d", pins, baseline["check_id"])
    assert same["comparison"]["netlist_equal"]
    with pytest.raises(PadsError):
        service.inspect_logical(sid, "other", pins, baseline["check_id"])
    with pytest.raises(PadsError):
        service.inspect_logical(
            sid, "d", [{"refdes": "R2", "pin_number": "1"}], baseline["check_id"]
        )
    value["provenance"] = {"format": "changed"}
    with pytest.raises(PadsError):
        service.inspect_logical(sid, "d", pins, baseline["check_id"])
    service.runner = lambda request: {
        "ok": False,
        "error": {"kind": "logical_snapshot_changed", "message": "changed"},
    }
    with pytest.raises(PadsError):
        service.inspect_logical(sid, "d", pins)
    with service.db() as db:
        assert db.execute("SELECT count(*) FROM logical_checks").fetchone()[0] == 2
