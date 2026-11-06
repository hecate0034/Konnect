"""Integer schematic geometry. Units are 0.01 inch; no tolerance guessing."""

import copy
import json
from .sessions import PadsError


def canonical(rows):
    return sorted(rows, key=lambda value: json.dumps(value, sort_keys=True))


def on_segment(point, a, b):
    return (
        (point[0] - a[0]) * (b[1] - a[1]) == (point[1] - a[1]) * (b[0] - a[0])
        and min(a[0], b[0]) <= point[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= point[1] <= max(a[1], b[1])
    )


def intersects(a, b, c, d):
    def cross(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    x, y, z, w = cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)
    return (x * y < 0 and z * w < 0) or any(
        (v == 0 and on_segment(p, s, t))
        for v, p, s, t in ((x, c, a, b), (y, d, a, b), (z, a, c, d), (w, b, c, d))
    )


def wire_issues(components, nets):
    by_uid = {n["uid"]: n for n in nets}
    issues = []
    for c in components:
        for pin in c["pins"]:
            if pin["net_uid"]:
                net = by_uid.get(pin["net_uid"])
                if not net or not any(
                    pin["point"] in (s["a"], s["b"]) for s in net["segments"]
                ):
                    issues.append(
                        {
                            "kind": "pin_endpoint_gap",
                            "component": c["target"]["uid"],
                            "pin": pin["uid"],
                        }
                    )
    for net in nets:
        for s in net["segments"]:
            if s["a"] == s["b"]:
                issues.append(
                    {"kind": "zero_length_wire", "net": net["uid"], "point": s["a"]}
                )
    return canonical(issues)


def move_geometry(component, position, nets):
    """Predict native pin-following endpoint moves; refuse shared/ambiguous joints."""
    dx, dy = (
        position["x"] - component["position"]["x"],
        position["y"] - component["position"]["y"],
    )
    points = {}
    for pin in component["pins"]:
        if pin["net_uid"]:
            points[(pin["net_uid"], tuple(pin["point"]))] = [
                pin["point"][0] + dx,
                pin["point"][1] + dy,
            ]
    for net in nets:
        for s in net["segments"]:
            original_a, original_b = s["a"][:], s["b"][:]
            for end in ("a", "b"):
                key = (net["uid"], tuple(s[end]))
                if key in points:
                    s[end] = points[key]
            if (s["a"], s["b"]) != (original_a, original_b) and not (
                original_a[0] == original_b[0] == s["a"][0] == s["b"][0]
                or original_a[1] == original_b[1] == s["a"][1] == s["b"][1]
            ):
                raise PadsError(
                    "route_unsupported",
                    "Move would require native rerouting; only straight parallel endpoint moves are supported",
                )
            if tuple(s["a"]) > tuple(s["b"]):
                s["a"], s["b"] = s["b"], s["a"]
                s["joint_a"], s["joint_b"] = s["joint_b"], s["joint_a"]
        net["segments"] = canonical(net["segments"])
    for pin in component["pins"]:
        pin["point"] = [pin["point"][0] + dx, pin["point"][1] + dy]
    component["bbox"] = [[p[0] + dx, p[1] + dy] for p in component["bbox"]]
    component["position"] = copy.deepcopy(position)


def clear_rectangle(box, components, nets, exclude=()):
    low, high = box
    for c in components:
        if c["target"]["uid"] in exclude or (
            not c["target"]["refdes"] and not c.get("pins")
        ):
            continue  # Sheet borders/annotations are not component obstacles.
        a, b = c["bbox"]
        if max(low[0], a[0]) <= min(high[0], b[0]) and max(low[1], a[1]) <= min(
            high[1], b[1]
        ):
            raise PadsError(
                "geometry_collision",
                "Proposed component overlaps an existing component",
            )
    edges = [
        (low, [high[0], low[1]]),
        ([high[0], low[1]], high),
        (high, [low[0], high[1]]),
        ([low[0], high[1]], low),
    ]
    for n in nets:
        for s in n["segments"]:
            if any(intersects(s["a"], s["b"], a, b) for a, b in edges) or (
                low[0] <= s["a"][0] <= high[0] and low[1] <= s["a"][1] <= high[1]
            ):
                raise PadsError(
                    "geometry_collision", "Proposed component overlaps an existing wire"
                )


def crosses_body(start, end, component):
    if not component["target"]["refdes"] and not component.get("pins"):
        return False
    low, high = component["bbox"]
    return (
        start[1] == end[1]
        and low[1] < start[1] < high[1]
        and max(min(start[0], end[0]), low[0]) < min(max(start[0], end[0]), high[0])
    ) or (
        start[0] == end[0]
        and low[0] < start[0] < high[0]
        and max(min(start[1], end[1]), low[1]) < min(max(start[1], end[1]), high[1])
    )
