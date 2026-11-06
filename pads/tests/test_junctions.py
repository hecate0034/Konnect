import copy
from types import SimpleNamespace

import pytest
from test_geometry import sheet, comp
from test_routing import connected
from runtime.schematic_plan import plan, normalize, verify_change
from runtime.native_schematic import recovery_allowed
from runtime.routing import segment
from runtime.sessions import PadsError
from runtime import electrical, native_schematic


def junction(before):
    c = comp("c", 70, "R3")
    c["bbox"] = [[70, 15], [90, 25]]
    c["position"]["y"] = 20
    c["pins"][0]["point"] = [80, 25]
    before["components"].append(c)
    op = {
        "kind": "route",
        "a": {"target": c["target"], "pin_uid": "p"},
        "b": {"net_uid": "n", "net_name": "n", "point": [25, 20]},
        "unit": "0.01_inch",
        "points": [[80, 25], [80, 40], [25, 40], [25, 20]],
    }
    return normalize(before), op


def test_interior_split_exact_net_and_partial_prefix(sheet):
    before, op = junction(connected(sheet))
    prepared = plan(before, op)
    split = prepared["route"]["split_segment"]
    after = copy.deepcopy(before)
    net = after["nets"][0]
    net["segments"].remove(split)
    net["segments"] += [
        segment([20, 20], [25, 20], 3, 5),
        segment([25, 20], [30, 20], 5, 3),
        segment([25, 20], [25, 40], 5, 1),
    ]
    recovery_allowed(prepared, normalize(after))
    net["segments"][-1] = segment([25, 20], [25, 40], 5, 3)
    net["segments"] += [
        segment([25, 40], [80, 40], 3, 3),
        segment([80, 25], [80, 40], 2, 3),
    ]
    c = next(c for c in after["components"] if c["target"]["uid"] == "c")
    c["pins"][0]["net_uid"] = "n"
    c["connections"][0].update(net_uid="n", net_name="n")
    verify_change(prepared, normalize(after))
    net["segments"][0]["joint_a"] = 5
    with pytest.raises(PadsError):
        recovery_allowed(prepared, normalize(after))


@pytest.mark.parametrize(
    "field,value",
    [("decoration", {"label": "VDD"}), ("decoration_checked", False), ("bus", True)],
)
def test_annotated_uninspected_bus_interiors_refused(sheet, field, value):
    before, op = junction(connected(sheet))
    for s in before["nets"][0]["segments"]:
        if s["a"] == [20, 20] and s["b"] == [30, 20]:
            s[field] = value
    with pytest.raises(PadsError):
        plan(before, op)


@pytest.mark.parametrize("misselect", [False, True])
def test_recovery_selects_only_added_edges_not_original_split_pieces(
    monkeypatch, misselect
):
    edges = [([20, 20], [25, 20]), ([25, 20], [30, 20]), ([25, 20], [25, 40])]
    pieces = [SimpleNamespace(edge=e) for e in edges]
    selected = []
    block = SimpleNamespace()
    net = SimpleNamespace(UID="n", Parent=block)
    app = SimpleNamespace(ActiveView=object())

    def call(obj, method, *args):
        if method == "Query":
            return [net]
        if method == "GetSegments":
            return pieces
        if method == "Location":
            x, y = obj.edge[args[0]]
            return SimpleNamespace(X=x, Y=y)
        if method == "SelectSegment":
            selected.append(args[0])
        if method == "IsSegmentSelected":
            return False if misselect else args[0] in selected
        if method == "DeleteSelected":
            assert not misselect
            assert selected == [pieces[2]]

    monkeypatch.setattr(native_schematic, "call", call)
    original = {
        "before": {},
        "operation": {},
        "kind": "route",
        "route": {"existing_net": {"uid": "n"}, "points": [[25, 40], [25, 20]]},
    }
    if misselect:
        with pytest.raises(PadsError) as exc:
            native_schematic.recover(app, original, {})
        assert exc.value.kind == "recovery_unsafe"
    else:
        native_schematic.recover(app, original, {})


def test_electrical_profile_is_explicit_and_preserves_definition_options():
    import xml.etree.ElementTree as ET

    data = (
        b"<VDRC>"
        + b"".join(
            (
                '<Check Name="%s" State="Disabled"><Option Value="original"/></Check>'
                % n
            ).encode()
            for n in electrical.PROFILES["electrical"]
        )
        + b"</VDRC>"
    )
    result = electrical.verify_profile(data, "electrical")
    assert {
        c.get("Name")
        for c in ET.fromstring(result).iter("Check")
        if c.get("State") == "Enabled"
    } == set(electrical.PROFILES["electrical"])
    assert result.count(b'Value="original"') == 8
    with pytest.raises(PadsError):
        electrical.verify_profile(data, "unknown")
    with pytest.raises(PadsError):
        electrical.check_options("electrical", "hierarchy")


def test_cross_page_inventory_keeps_native_identity_and_does_not_merge_names(
    monkeypatch,
):
    from runtime import native_designer

    nets = [
        SimpleNamespace(
            UID="same-local-uid",
            LogicalNetName="VDD",
            Parent=SimpleNamespace(SheetNum=page),
        )
        for page in ("a", "b")
    ]

    def call(obj, name, *args):
        if name == "DesignNets":
            assert args[-1] is False
            return nets
        if name == "GetSegments":
            return []

    monkeypatch.setattr(native_designer, "call", call)
    result = electrical.design_inventory(object(), "d")
    assert len(result["nets"]) == 2
    assert len(result["same_name_cross_page_candidates"]) == 1
    assert not result["same_name_cross_page_candidates"][0][
        "electrical_equivalence_verified"
    ]
    nets.append(nets[0])
    with pytest.raises(PadsError):
        electrical.design_inventory(object(), "d")
