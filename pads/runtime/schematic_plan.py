"""Pure, reviewable plans and exact delta checks for bounded schematic operations."""

import copy
import re
from .edits import validate_target, validate_changes, proposed_state
from .geometry import (
    canonical,
    move_geometry,
    wire_issues,
    clear_rectangle,
    intersects,
    on_segment,
    crosses_body,
)
from .sessions import PadsError


def find_component(sheet, target):
    validate_target(target)
    found = [c for c in sheet["components"] if c["target"] == target]
    if len(found) != 1:
        raise PadsError(
            "component_not_unique", "Exact component not found on the requested sheet"
        )
    return found[0]


def effective(component, name):
    matches = [
        a["effective_value"] for a in component["attributes"] if a["name"] == name
    ]
    if len(matches) != 1 or not matches[0]:
        raise PadsError(
            "reuse_metadata_missing", "Required existing property is missing: " + name
        )
    return matches[0]


def find_pin(sheet, endpoint):
    if not isinstance(endpoint, dict) or set(endpoint) != {"target", "pin_uid"}:
        raise PadsError(
            "invalid_operation", "Require an exact component target and pin UID"
        )
    c = find_component(sheet, endpoint["target"])
    pins = [p for p in c["pins"] if p["uid"] == endpoint["pin_uid"]]
    if len(pins) != 1 or not pins[0]["number"]:
        raise PadsError("pin_not_unique", "Existing numbered pin required")
    return c, pins[0]


def normalize(sheet):
    result = copy.deepcopy(sheet)
    # Designer updates the border's reserved timestamp on every edit. It is
    # explicitly excluded from the electrical/geometry recovery contract.
    for c in result["components"]:
        if not c["target"]["refdes"]:
            c["attributes"] = [a for a in c["attributes"] if a["name"] != "@DATETIME"]
    result["components"] = canonical(result["components"])
    for net in result["nets"]:
        net["segments"] = canonical(net["segments"])
    result["nets"] = canonical(result["nets"])
    return result


def plan(sheet, operation):
    if not isinstance(operation, dict):
        raise PadsError("invalid_operation", "Operation must be an object")
    kind = operation.get("kind")
    keys = {
        "edit": {"kind", "target", "changes"},
        "place": {"kind", "source", "position", "refdes"},
        "connect": {"kind", "a", "b"},
        "route": {"kind", "a", "b", "points", "unit"},
    }
    if kind not in keys or set(operation) != keys[kind]:
        raise PadsError("invalid_operation", "Unknown operation or extra fields")
    result = {
        "kind": kind,
        "operation": copy.deepcopy(operation),
        "before": normalize(sheet),
    }
    if kind == "route":
        from .routing import prepare_route

        result["route"] = prepare_route(sheet, operation)
    elif kind == "edit":
        changes = validate_changes(operation["changes"])
        original = find_component(sheet, operation["target"])
        expected = copy.deepcopy(sheet)
        comp = find_component(expected, operation["target"])
        attrs = {k: v for k, v in changes.items() if k != "position"}
        if attrs:
            comp.update(proposed_state(comp, attrs))
        if "position" in changes:
            # A moved pin cannot share its location with another connected pin.
            pin_points = {tuple(p["point"]) for p in original["pins"] if p["net_uid"]}
            if any(
                tuple(p["point"]) in pin_points
                for c in sheet["components"]
                if c["target"] != original["target"]
                for p in c["pins"]
            ):
                raise PadsError(
                    "shared_joint_unsupported",
                    "Moving a pin shared with another component is unsupported",
                )
            move_geometry(comp, changes["position"], expected["nets"])
            attached = {p["net_uid"] for p in comp["pins"] if p["net_uid"]}
            clear_rectangle(
                comp["bbox"],
                expected["components"],
                [n for n in expected["nets"] if n["uid"] not in attached],
                exclude=(comp["target"]["uid"],),
            )
            # Check newly changed wire segments against other nets, including crossovers.
            old = {n["uid"]: n for n in sheet["nets"]}
            for n in expected["nets"]:
                for segment in n["segments"]:
                    if segment in old[n["uid"]]["segments"]:
                        continue
                    if any(
                        p["net_uid"] != n["uid"]
                        and on_segment(p["point"], segment["a"], segment["b"])
                        for c in expected["components"]
                        for p in c["pins"]
                    ):
                        raise PadsError(
                            "geometry_collision",
                            "Moved wire would touch an unrelated pin",
                        )
                    if any(
                        crosses_body(segment["a"], segment["b"], c)
                        for c in expected["components"]
                    ):
                        raise PadsError(
                            "geometry_collision", "Moved wire crosses a component body"
                        )
                    for other in expected["nets"]:
                        if other["uid"] != n["uid"] and any(
                            intersects(segment["a"], segment["b"], s["a"], s["b"])
                            for s in other["segments"]
                        ):
                            raise PadsError(
                                "geometry_collision",
                                "Moved wire would intersect another net",
                            )
            if wire_issues(expected["components"], expected["nets"]) != wire_issues(
                sheet["components"], sheet["nets"]
            ):
                raise PadsError(
                    "geometry_invalid",
                    "Move changes pin attachment geometry or collapses a wire",
                )
        result["expected"] = normalize(expected)
        if result["expected"] == result["before"]:
            raise PadsError("no_change", "Proposed values already match")
    elif kind == "place":
        source = find_component(sheet, operation["source"])
        validate_changes({"position": operation["position"]})
        if not isinstance(operation["refdes"], str) or not re.fullmatch(
            "[A-Za-z][A-Za-z0-9_]{0,31}", operation["refdes"]
        ):
            raise PadsError(
                "invalid_operation",
                "Explicit alphanumeric reference designator required",
            )
        if any(
            c["target"]["refdes"].casefold() == operation["refdes"].casefold()
            for c in sheet["components"]
        ):
            raise PadsError(
                "refdes_exists", "Reference designator already exists on this sheet"
            )
        if (
            source["orientation"] != 0
            or not source["pins"]
            or any(not p["number"] for p in source["pins"])
        ):
            raise PadsError(
                "reuse_unsupported",
                "First placement increment requires an unrotated source with numbered pins",
            )
        names = [a["name"] for a in source["attributes"]]
        if len(names) != len(set(names)) or any(
            not n or "=" in n or "\n" in n for n in names
        ):
            raise PadsError("reuse_unsupported", "Ambiguous source attributes")
        candidate = {
            "partition": source["symbol_partition"],
            "symbol": source["symbol"],
            "part_name": effective(source, "Part Name"),
            "part_number": effective(source, "Part Number"),
            "cell": effective(source, "Cell Name"),
        }
        if not candidate["partition"]:
            raise PadsError(
                "reuse_metadata_missing", "Existing symbol partition is required"
            )
        comp = copy.deepcopy(source)
        move_geometry(comp, operation["position"], [])
        clear_rectangle(comp["bbox"], sheet["components"], sheet["nets"])
        result.update(
            candidate=candidate,
            projected_bbox=comp["bbox"],
            projected_pins=comp["pins"],
        )
    else:
        ca, a = find_pin(sheet, operation["a"])
        cb, b = find_pin(sheet, operation["b"])
        if ca["target"] == cb["target"] or a["net_uid"] or b["net_uid"]:
            raise PadsError(
                "connection_unsupported",
                "Require free pins on two different existing components; net merges are not supported",
            )
        start, end = a["point"], b["point"]
        if start == end or (start[0] != end[0] and start[1] != end[1]):
            raise PadsError(
                "route_unsupported",
                "First connection increment requires one nonzero horizontal or vertical segment",
            )
        from .routing import pin_exit

        pin_exit(ca, a, end)
        pin_exit(cb, b, start)
        if any(
            intersects(start, end, s["a"], s["b"])
            for n in sheet["nets"]
            for s in n["segments"]
        ):
            raise PadsError(
                "geometry_collision", "Connection intersects an existing wire"
            )
        for c in sheet["components"]:
            for p in c["pins"]:
                if (c["target"], p["uid"]) not in [
                    (ca["target"], a["uid"]),
                    (cb["target"], b["uid"]),
                ] and on_segment(p["point"], start, end):
                    raise PadsError(
                        "geometry_collision", "Connection would touch another pin"
                    )
            if crosses_body(start, end, c):
                raise PadsError(
                    "geometry_collision", "Connection crosses a component body"
                )
        result["route"] = {"start": start, "end": end, "unit": "0.01_inch"}
    return result


def verify_change(plan, actual):
    """Require exactly the allowed design delta, not merely a returned COM object."""
    before = normalize(plan["before"])
    actual = normalize(actual)
    kind = plan["kind"]
    op = plan["operation"]
    if kind == "route":
        from .routing import verify_route

        verify_route(plan, actual)
        return
    if actual["design"] != before["design"] or actual["sheet"] != before["sheet"]:
        raise PadsError("readback_mismatch", "Sheet identity changed")
    if kind == "edit":
        if normalize(actual) != plan["expected"]:
            raise PadsError(
                "readback_mismatch",
                "Edit geometry or unrequested sheet state differs from preview",
            )
        return
    old_ids = {c["target"]["uid"] for c in before["components"]}
    new = [c for c in actual["components"] if c["target"]["uid"] not in old_ids]
    old_nets = {n["uid"] for n in before["nets"]}
    new_nets = [n for n in actual["nets"] if n["uid"] not in old_nets]
    if kind == "place":
        if (
            len(new) != 1
            or actual["nets"] != before["nets"]
            or canonical([c for c in actual["components"] if c not in new])
            != before["components"]
        ):
            raise PadsError(
                "readback_mismatch",
                "Placement changed more than one component or changed wires",
            )
        placed = new[0]
        source = find_component(before, op["source"])
        attrs = {a["name"]: a["effective_value"] for a in placed["attributes"]}
        for a in source["attributes"]:
            expected = (
                op["refdes"] if a["name"] == "Ref Designator" else a["effective_value"]
            )
            if attrs.get(a["name"]) != expected:
                raise PadsError(
                    "readback_mismatch",
                    "Placed instance property differs: " + a["name"],
                )
        expected_pins = copy.deepcopy(plan["projected_pins"])
        for p in expected_pins:
            p["net_uid"] = None
        if (
            placed["target"]["refdes"] != op["refdes"]
            or placed["position"] != op["position"]
            or placed["orientation"] != 0
            or placed["symbol"] != source["symbol"]
            or placed["symbol_partition"] != source["symbol_partition"]
            or placed["bbox"] != plan["projected_bbox"]
            or placed["pins"] != canonical(expected_pins)
        ):
            raise PadsError(
                "readback_mismatch",
                "Placed instance identity, pins or geometry differs",
            )
    else:
        if (
            new
            or len(new_nets) != 1
            or canonical([n for n in actual["nets"] if n not in new_nets])
            != before["nets"]
        ):
            raise PadsError(
                "readback_mismatch",
                "Connection changed existing nets or created unexpected objects",
            )
        net = new_nets[0]
        if net["name"] != net["uid"]:
            raise PadsError(
                "readback_mismatch",
                "Expected a new unnamed net; named/global net joining is unsupported",
            )
        points = sorted([plan["route"]["start"], plan["route"]["end"]])
        if len(net["segments"]) != 1 or net["segments"][0] != dict(
            a=points[0], b=points[1], joint_a=2, joint_b=2, bus=False
        ):
            raise PadsError(
                "readback_mismatch",
                "Connection geometry differs from the single segment preview",
            )
        expected = copy.deepcopy(before)
        for endpoint in (op["a"], op["b"]):
            comp, pin = find_pin(expected, endpoint)
            pin["net_uid"] = net["uid"]
            for conn in comp["connections"]:
                if conn["pin_uid"] == pin["uid"]:
                    conn["net_uid"] = net["uid"]
                    conn["net_name"] = net["name"]
            comp["connections"] = canonical(comp["connections"])
        if canonical(actual["components"]) != canonical(expected["components"]):
            raise PadsError(
                "readback_mismatch", "Connection changed other pins or component state"
            )
    if wire_issues(actual["components"], actual["nets"]) != wire_issues(
        before["components"], before["nets"]
    ):
        raise PadsError(
            "readback_mismatch", "New pin/endpoint gap or collapsed wire detected"
        )
