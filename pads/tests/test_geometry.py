import copy
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.geometry import intersects, wire_issues, move_geometry, canonical
from runtime.schematic_plan import plan, verify_change, normalize
from runtime.native_schematic import recovery_allowed
from runtime.sessions import PadsError


def comp(uid, x, refdes):
    return {
        "target": {
            "design": "d",
            "sheet": "s",
            "uid": uid,
            "path": uid,
            "refdes": refdes,
        },
        "symbol": "resistor",
        "symbol_partition": "Sample",
        "orientation": 0,
        "position": {"x": x, "y": 0, "unit": "0.01_inch"},
        "bbox": [[x, 0], [x + 10, 10]],
        "attributes": [
            {"name": n, "base_value": v, "instance_value": "", "effective_value": v}
            for n, v in [
                ("Part Name", "P1"),
                ("Part Number", "P1"),
                ("Cell Name", "C1"),
                ("Description", "R"),
            ]
        ],
        "pins": [
            {
                "uid": "p",
                "name": "N",
                "number": "1",
                "point": [x + 10, 5],
                "net_uid": None,
            }
        ],
        "connections": [
            {"pin_uid": "p", "pin_name": "N", "net_uid": None, "net_name": None}
        ],
    }


@pytest.fixture
def sheet():
    a = comp("a", 0, "R1")
    b = comp("b", 40, "R2")
    b["pins"][0]["point"] = [40, 5]
    return {"design": "d", "sheet": "s", "components": canonical([a, b]), "nets": []}


def target(sheet, uid):
    return next(c["target"] for c in sheet["components"] if c["target"]["uid"] == uid)


def test_segment_intersection_covers_crossing_touching_collinear_and_separation():
    assert intersects([0, 0], [10, 0], [5, -5], [5, 5])
    assert intersects([0, 0], [10, 0], [10, 0], [20, 0])
    assert intersects([0, 0], [10, 0], [3, 0], [7, 0])
    assert not intersects([0, 0], [10, 0], [11, 0], [20, 0])
    assert not intersects([0, 0], [10, 0], [0, 1], [10, 1])


def test_free_pin_connection_and_exact_delta(sheet):
    op = {
        "kind": "connect",
        "a": {"target": target(sheet, "a"), "pin_uid": "p"},
        "b": {"target": target(sheet, "b"), "pin_uid": "p"},
    }
    prepared = plan(sheet, op)
    assert prepared["route"] == {"start": [10, 5], "end": [40, 5], "unit": "0.01_inch"}
    after = copy.deepcopy(sheet)
    after["nets"] = [
        {
            "uid": "n",
            "name": "n",
            "segments": [
                {"a": [10, 5], "b": [40, 5], "joint_a": 2, "joint_b": 2, "bus": False}
            ],
        }
    ]
    for c in after["components"]:
        c["pins"][0]["net_uid"] = "n"
        c["connections"][0].update(net_uid="n", net_name="n")
    verify_change(prepared, after)
    after["nets"][0]["segments"][0]["b"] = [39, 5]
    with pytest.raises(PadsError):
        verify_change(prepared, after)
    assert wire_issues(after["components"], after["nets"])


@pytest.mark.parametrize(
    "case", ["diagonal", "wired_pin", "crossing", "third_pin", "body"]
)
def test_unsafe_routes_refused(sheet, case):
    a = next(c for c in sheet["components"] if c["target"]["uid"] == "a")
    b = next(c for c in sheet["components"] if c["target"]["uid"] == "b")
    if case == "diagonal":
        b["pins"][0]["point"][1] = 7
    if case == "wired_pin":
        a["pins"][0]["net_uid"] = "existing"
    if case == "crossing":
        sheet["nets"] = [{"uid": "n", "segments": [{"a": [25, 0], "b": [25, 10]}]}]
    if case in ("third_pin", "body"):
        c = comp("c", 20, "R3")
        if case == "body":
            c["pins"][0]["point"] = [20, 20]
        sheet["components"].append(c)
    op = {
        "kind": "connect",
        "a": {"target": a["target"], "pin_uid": "p"},
        "b": {"target": b["target"], "pin_uid": "p"},
    }
    with pytest.raises(PadsError):
        plan(sheet, op)


def test_placement_requires_existing_metadata_and_clear_region(sheet):
    op = {
        "kind": "place",
        "source": target(sheet, "a"),
        "refdes": "R3",
        "position": {"x": 80, "y": 0, "unit": "0.01_inch"},
    }
    p = plan(sheet, op)
    assert p["candidate"]["part_number"] == "P1"
    for change in [
        {"refdes": "R1"},
        {"position": {"x": 40, "y": 0, "unit": "0.01_inch"}},
        {"refdes": "R?"},
    ]:
        with pytest.raises(PadsError):
            plan(sheet, {**op, **change})


def test_connected_move_updates_endpoints_and_rejects_collapse(sheet):
    a = next(c for c in sheet["components"] if c["target"]["uid"] == "a")
    a["pins"][0]["net_uid"] = "n"
    sheet["nets"] = [
        {
            "uid": "n",
            "name": "N",
            "segments": [
                {"a": [10, 5], "b": [30, 5], "joint_a": 2, "joint_b": 1, "bus": False}
            ],
        }
    ]
    moved = copy.deepcopy(a)
    nets = copy.deepcopy(sheet["nets"])
    move_geometry(moved, {"x": 5, "y": 0, "unit": "0.01_inch"}, nets)
    assert nets[0]["segments"][0]["a"] == [15, 5] and moved["pins"][0]["point"] == [
        15,
        5,
    ]
    assert not wire_issues([moved], nets)
    move_geometry(moved, {"x": 20, "y": 0, "unit": "0.01_inch"}, nets)
    assert any(i["kind"] == "zero_length_wire" for i in wire_issues([moved], nets))


def test_perpendicular_move_is_refused_before_native_autorouting(sheet):
    c = sheet["components"][0]
    c["pins"][0]["net_uid"] = "n"
    net = {
        "uid": "n",
        "segments": [
            {"a": c["pins"][0]["point"][:], "b": [30, 5], "joint_a": 2, "joint_b": 1}
        ],
    }
    with pytest.raises(PadsError) as error:
        move_geometry(c, {"x": 0, "y": 10, "unit": "0.01_inch"}, [net])
    assert error.value.kind == "route_unsupported"


def test_moved_wire_cannot_touch_a_free_pin_on_a_body_boundary(sheet):
    a = next(c for c in sheet["components"] if c["target"]["uid"] == "a")
    a["pins"][0]["net_uid"] = "n"
    a["position"]["x"] = 10
    a["bbox"] = [[10, 0], [20, 10]]
    a["pins"][0]["point"] = [20, 5]
    foreign = comp("c", 12, "R3")
    foreign["bbox"] = [[12, 5], [14, 10]]
    foreign["pins"][0]["point"] = [14, 5]
    sheet["components"].append(foreign)
    sheet["nets"] = [
        {
            "uid": "n",
            "name": "N",
            "segments": [
                {"a": [20, 5], "b": [30, 5], "joint_a": 2, "joint_b": 1, "bus": False}
            ],
        }
    ]
    with pytest.raises(PadsError) as error:
        plan(
            sheet,
            {
                "kind": "edit",
                "target": a["target"],
                "changes": {"position": {"x": 0, "y": 0, "unit": "0.01_inch"}},
            },
        )
    assert error.value.kind == "geometry_collision"
    assert "unrelated pin" in str(error.value)


def test_recovery_accepts_only_verified_change_or_known_partial_prefix(sheet):
    changes = {
        "attribute": {"name": "Description", "value": "new"},
        "position": {"x": 2, "y": 0, "unit": "0.01_inch"},
    }
    p = plan(sheet, {"kind": "edit", "target": target(sheet, "a"), "changes": changes})
    recovery_allowed(p, p["expected"])
    prefix = plan(
        sheet,
        {
            "kind": "edit",
            "target": target(sheet, "a"),
            "changes": {"attribute": changes["attribute"]},
        },
    )["expected"]
    recovery_allowed(p, prefix)
    prefix["components"][0]["orientation"] = 1
    with pytest.raises(PadsError):
        recovery_allowed(p, prefix)


def test_only_unreferenced_native_datetime_is_excluded(sheet):
    graphic = comp("border", 100, "")
    graphic["attributes"] = [
        {"name": "@DATETIME", "effective_value": "old"},
        {"name": "Title", "effective_value": "A"},
    ]
    sheet["components"].append(graphic)
    old = normalize(sheet)
    graphic["attributes"][0]["effective_value"] = "new"
    assert normalize(sheet) == old
    graphic["attributes"][1]["effective_value"] = "B"
    assert normalize(sheet) != old


def test_fine_grid_restores_original_setting_even_when_native_edit_fails(monkeypatch):
    from runtime import native_geometry

    state = {"grid": 10}
    calls = []

    def call(app, name, *args):
        calls.append((name, args))
        if name == "ParamGetValue":
            return state["grid"]
        state["grid"] = args[1]

    monkeypatch.setattr(native_geometry, "call", call)
    with pytest.raises(RuntimeError):
        with native_geometry.exact_grid(object()):
            assert state["grid"] == 1
            raise RuntimeError("injected native failure")
    assert state["grid"] == 10
