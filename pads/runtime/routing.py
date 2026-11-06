"""Explicit orthogonal routes with exact local-net identity preservation."""

import copy
from .geometry import canonical, intersects, on_segment, crosses_body
from .sessions import PadsError


def segment(a, b, ja, jb):
    if a > b:
        a, b, ja, jb = b, a, jb, ja
    return dict(a=a[:], b=b[:], joint_a=ja, joint_b=jb, bus=False)


def pin_exit(component, pin, neighbor):
    """Refuse tangent departures that COM 66 may replace with an automatic stub."""
    x, y = pin["point"]
    low, high = component["bbox"]
    if sum((x == low[0], x == high[0], y == low[1], y == high[1])) != 1:
        raise PadsError(
            "pin_exit_unsupported", "Pin is not on one unambiguous bounding-box edge"
        )
    outward = (
        (x == low[0] and neighbor[0] < x and neighbor[1] == y)
        or (x == high[0] and neighbor[0] > x and neighbor[1] == y)
        or (y == low[1] and neighbor[1] < y and neighbor[0] == x)
        or (y == high[1] and neighbor[1] > y and neighbor[0] == x)
    )
    if not outward:
        raise PadsError(
            "pin_exit_unsupported",
            "Pin departure must be outward and perpendicular to its bounding-box edge; tangent/ambiguous exits may trigger native rerouting",
        )


def prepare_route(sheet, op):
    from .schematic_plan import find_pin

    if set(op) != {"kind", "a", "b", "points", "unit"} or op["unit"] != "0.01_inch":
        raise PadsError(
            "invalid_route", "Require explicit endpoints, points and 0.01_inch units"
        )
    points = op["points"]
    if (
        not isinstance(points, list)
        or not 2 <= len(points) <= 9
        or any(
            not isinstance(p, list)
            or len(p) != 2
            or any(type(v) is not int or not 0 <= v <= 10000 for v in p)
            for p in points
        )
    ):
        raise PadsError(
            "invalid_route", "Require 2..9 bounded integer coordinate pairs"
        )
    ca, a = find_pin(sheet, op["a"])
    if a["net_uid"]:
        raise PadsError(
            "connection_unsupported",
            "Starting pin must be free; net merging is unsupported",
        )
    endpoints = [(ca["target"], a["uid"])]
    target_net = None
    if isinstance(op["b"], dict) and set(op["b"]) == {"net_uid", "net_name", "point"}:
        b = op["b"]
        matches = [
            n
            for n in sheet["nets"]
            if n["uid"] == b["net_uid"] and n["name"] == b["net_name"]
        ]
        if len(matches) != 1:
            raise PadsError(
                "net_not_unique", "Exact existing net identity/name required"
            )
        target_net = matches[0]
        at_anchor = [
            s for s in target_net["segments"] if b["point"] in (s["a"], s["b"])
        ]
        if (
            not at_anchor
            or any(s["bus"] for s in target_net["segments"])
            or any(
                s["joint_" + k] not in (3, 5)
                for s in at_anchor
                for k in ("a", "b")
                if s[k] == b["point"]
            )
        ):
            raise PadsError(
                "anchor_unsupported",
                "Use an existing wire corner or solder joint; interior splitting is not supported",
            )
        end = b["point"]
    else:
        cb, b = find_pin(sheet, op["b"])
        if b["net_uid"] or ca["target"] == cb["target"]:
            raise PadsError(
                "connection_unsupported", "Require free pins on different components"
            )
        endpoints.append((cb["target"], b["uid"]))
        end = b["point"]
    if points[0] != a["point"] or points[-1] != end:
        raise PadsError(
            "route_endpoint_mismatch",
            "Path endpoints must equal the chosen pin/joint coordinates",
        )
    edges = list(zip(points, points[1:]))
    for i, (p, q) in enumerate(edges):
        if p == q or (p[0] != q[0] and p[1] != q[1]):
            raise PadsError(
                "invalid_route", "Every segment must be nonzero and orthogonal"
            )
        if i and (points[i - 1][0] == p[0] == q[0] or points[i - 1][1] == p[1] == q[1]):
            raise PadsError(
                "invalid_route", "Remove redundant or backtracking collinear vertices"
            )
        if any(intersects(p, q, r, s) for r, s in edges[: max(0, i - 1)]):
            raise PadsError("geometry_collision", "Route touches or crosses itself")
        for n in sheet["nets"]:
            for s in n["segments"]:
                if not intersects(p, q, s["a"], s["b"]):
                    continue
                # Only the final endpoint may touch the explicitly chosen joint.
                allowed = (
                    target_net is not None
                    and n["uid"] == target_net["uid"]
                    and i == len(edges) - 1
                    and end in (s["a"], s["b"])
                )
                if allowed:
                    other = s["b"] if s["a"] == end else s["a"]
                    allowed = not on_segment(p, s["a"], s["b"]) and not on_segment(
                        other, p, q
                    )
                if not allowed:
                    raise PadsError(
                        "geometry_collision",
                        "Route intersects an existing wire away from its chosen joint",
                    )
        for c in sheet["components"]:
            if crosses_body(p, q, c):
                raise PadsError("geometry_collision", "Route crosses a component body")
            for pin in c["pins"]:
                if on_segment(pin["point"], p, q):
                    allowed = (
                        (c["target"], pin["uid"]) in endpoints
                        and pin["point"] in (points[0], points[-1])
                        and (
                            i == 0
                            and pin["point"] == p
                            or i == len(edges) - 1
                            and pin["point"] == q
                        )
                    )
                    if not allowed:
                        raise PadsError(
                            "geometry_collision", "Route touches an unrelated pin"
                        )
    pin_exit(ca, a, points[1])
    if target_net is None:
        pin_exit(cb, b, points[-2])
    added = [
        segment(
            p,
            q,
            2 if i == 0 else 3,
            (5 if target_net else 2) if i == len(edges) - 1 else 3,
        )
        for i, (p, q) in enumerate(edges)
    ]
    return {
        "points": copy.deepcopy(points),
        "added_segments": canonical(added),
        "existing_net": copy.deepcopy(target_net),
    }


def verify_route(prepared, actual, completed=None):
    from .schematic_plan import normalize, find_pin

    before = prepared["before"]
    op, route = prepared["operation"], prepared["route"]
    expected = copy.deepcopy(before)
    existing = route["existing_net"]
    count = len(route["points"]) - 1
    completed = count if completed is None else completed
    if not 1 <= completed <= count:
        raise PadsError("readback_mismatch", "Invalid route prefix")
    full = completed == count
    points = (
        route["points"][-completed - 1 :]
        if existing
        else route["points"][: completed + 1]
    )
    segments = canonical(
        [
            segment(
                p,
                q,
                (2 if full or not existing else 1) if i == 0 else 3,
                (5 if existing else (2 if full else 1)) if i == completed - 1 else 3,
            )
            for i, (p, q) in enumerate(zip(points, points[1:]))
        ]
    )
    if existing:
        net = next(n for n in expected["nets"] if n["uid"] == existing["uid"])
        anchor = route["points"][-1]
        for s in net["segments"]:
            for k in ("a", "b"):
                if s[k] == anchor:
                    s["joint_" + k] = 5
        net["segments"] = canonical(net["segments"] + segments)
    else:
        old = {n["uid"] for n in before["nets"]}
        added = [n for n in actual["nets"] if n["uid"] not in old]
        if len(added) != 1 or added[0]["name"] != added[0]["uid"]:
            raise PadsError("readback_mismatch", "Expected exactly one new unnamed net")
        net = {"uid": added[0]["uid"], "name": added[0]["name"], "segments": segments}
        expected["nets"].append(net)
    endpoints = (
        ([op["a"]] if full else [])
        if existing
        else ([op["a"], op["b"]] if full else [op["a"]])
    )
    for endpoint in endpoints:
        c, p = find_pin(expected, endpoint)
        p["net_uid"] = net["uid"]
        for conn in c["connections"]:
            if conn["pin_uid"] == p["uid"]:
                conn.update(net_uid=net["uid"], net_name=net["name"])
        c["connections"] = canonical(c["connections"])
    if normalize(expected) != normalize(actual):
        raise PadsError(
            "readback_mismatch",
            "Route changed unexpected geometry, net identity or pin connectivity",
        )


def netlist(sheet):
    """Local physical-net connectivity, deliberately not a flattened hierarchy."""
    nets = {
        n["uid"]: {"uid": n["uid"], "name": n["name"], "pins": []}
        for n in sheet["nets"]
    }
    free = []
    for c in sheet["components"]:
        for p in c["pins"]:
            pin = {"target": c["target"], "pin_uid": p["uid"], "number": p["number"]}
            if p["net_uid"]:
                if p["net_uid"] not in nets:
                    raise PadsError(
                        "connectivity_mismatch", "Pin references a missing sheet net"
                    )
                nets[p["net_uid"]]["pins"].append(pin)
            else:
                free.append(pin)
    for n in nets.values():
        n["pins"] = canonical(n["pins"])
    return {
        "nets": canonical(list(nets.values())),
        "unconnected_pins": canonical(free),
        "scope": "one_sheet_physical_nets",
    }
