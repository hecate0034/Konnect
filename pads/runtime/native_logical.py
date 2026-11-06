"""Live selected pins corroborating native whole-design netlist exports."""

from itertools import combinations
from pathlib import Path
import json

from .native_designer import call
from .logical import FORMAT, validate_pins, export_round, digest
from .diagnostics import fingerprint
from .geometry import canonical
from .sessions import PadsError


def read_selected(app, design, pins):
    validate_pins(pins)
    wanted = {(p["refdes"], p["pin_number"]) for p in pins}
    refs = {r for r, _ in wanted}
    found = {key: [] for key in wanted}
    count = 0
    for component in call(app, "DesignComponents", "", design, "-1", "", True):
        count += 1
        if count > 10000:
            raise PadsError(
                "design_too_large",
                "Logical inspection limit is 10000 component instances",
            )
        ref = str(component.Refdes)
        if ref not in refs:
            continue
        for conn in call(component, "GetConnections"):
            cp = conn.CompPin
            key = (ref, str(cp.Number))
            if key not in wanted:
                continue
            net = conn.Net
            if net and call(conn.Segment, "IsBus"):
                raise PadsError(
                    "logical_pin_unsupported",
                    "Direct bus-segment pin mapping is not supported",
                )
            found[key].append(
                {
                    "refdes": ref,
                    "pin_number": key[1],
                    "component_uid": str(component.UID),
                    # Flag 3 loses the parent instance in repeated child blocks.
                    "instance_path": str(call(component, "GetName", 0)),
                    "block_path": str(call(component.Parent, "GetName", 0)),
                    "sheet": str(component.Parent.SheetNum),
                    "pin_uid": str(cp.Pin.UID),
                    "physical_net_uid": str(net.UID) if net else None,
                    "physical_net_name": str(net.LogicalNetName) if net else None,
                }
            )
    if any(len(v) != 1 for v in found.values()):
        raise PadsError(
            "logical_pin_not_unique",
            "Pins must resolve once; no implicit numbering/unit inference: "
            + ", ".join(
                f"{r}.{p} ({len(found[(r, p)])} matches)"
                for r, p in sorted(found)
                if len(found[(r, p)]) != 1
            ),
        )
    rows = [v[0] for v in found.values()]
    if any(
        not p[k]
        for p in rows
        for k in ("component_uid", "instance_path", "block_path", "sheet", "pin_uid")
    ) or len({(p["instance_path"], p["sheet"], p["pin_uid"]) for p in rows}) != len(
        rows
    ):
        raise PadsError(
            "logical_pin_not_unique",
            "Native instance-path/pin identity is incomplete or ambiguous",
        )
    return canonical(rows)


def resolve_membership(netlist, observed):
    known = {(c["refdes"], p) for c in netlist["components"] for p in c["pin_numbers"]}
    index = {
        (p["refdes"], p["pin_number"]): n["name"]
        for n in netlist["nets"]
        for p in n["pins"]
    }
    result, physical = [], {}
    for pin in observed:
        key = (pin["refdes"], pin["pin_number"])
        if key not in known:
            raise PadsError(
                "logical_live_mismatch", "Live pin is missing from fresh native export"
            )
        name = index.get(key)
        if (name is None) != (pin["physical_net_uid"] is None):
            raise PadsError(
                "logical_live_mismatch",
                "Export/live connection presence differs; no hidden-pin inference is allowed",
            )
        physical_key = (pin["block_path"], pin["sheet"], pin["physical_net_uid"])
        if name is not None:
            if physical_key in physical and physical[physical_key] != name:
                raise PadsError(
                    "logical_live_mismatch",
                    "One physical net maps to conflicting exported nets",
                )
            physical[physical_key] = name
        result.append(
            {
                **pin,
                "logical_net_name": name,
                "mapping_status": "connected" if name is not None else "unconnected",
            }
        )
    return canonical(result)


def inspect(app, request, identity, initial):
    pins, design = request["pins"], request["design"]
    validate_pins(pins)
    data = call(app, "GetProjectData")
    designs = call(data, "GetiCDBDesigns")
    if design not in [
        str(call(designs, "GetItem", i))
        for i in range(1, call(designs, "GetCount") + 1)
    ]:
        raise PadsError("design_not_found", "Select an existing project design")
    database = Path(data.iCDBDir).resolve(strict=True)
    library = Path(data.CentralLibraryPath).resolve(strict=True)
    root = Path(request["policy"]["fixture"]).resolve(strict=True)
    if not database.is_relative_to(root) or not library.is_relative_to(root):
        raise PadsError(
            "write_scope_denied", "Logical source database/library left the test copy"
        )
    library_sha = fingerprint(library)
    before = read_selected(app, design, pins)
    output = Path(request["output_dir"])
    first, first_evidence = export_round(database, design, output / "first")
    if identity() != initial:
        raise PadsError("target_changed", "Project changed during native export")
    resolved = resolve_membership(first, before)
    after = read_selected(app, design, pins)
    second, second_evidence = export_round(database, design, output / "second")
    sources = {k: v["executable_sha256"] for k, v in first_evidence.items()}
    if (
        before != after
        or first != second
        or identity() != initial
        or fingerprint(library) != library_sha
        or Path(call(app, "GetProjectData").iCDBDir).resolve() != database
        or Path(call(app, "GetProjectData").CentralLibraryPath).resolve() != library
        or sources != {k: v["executable_sha256"] for k, v in second_evidence.items()}
    ):
        raise PadsError(
            "logical_snapshot_changed",
            "Live pins, native graph, exporters, library or target changed during inspection",
        )
    # Each selected pair is resolved through native exported pin membership,
    # never through equality of local net names or UIDs.
    relations = [
        {
            "a": {k: a[k] for k in ("refdes", "pin_number")},
            "b": {k: b[k] for k in ("refdes", "pin_number")},
            "connected": a["logical_net_name"] is not None
            and a["logical_net_name"] == b["logical_net_name"],
        }
        for a, b in combinations(resolved, 2)
    ]
    report = {
        "ok": True,
        "identity": initial,
        "project_write_attempted": False,
        "provenance": {
            "format": FORMAT,
            "exporter_sha256": sources,
            "snapshot": "DxD",
            "flow": "DX",
        },
        "netlist": first,
        "netlist_sha256": digest(first),
        "resolved_pins": resolved,
        "relations": relations,
        "evidence": {"first": first_evidence, "second": second_evidence},
        "scope": "native_flattened_package_pin_membership_with_selected_live_pin_corroboration",
        "selected_pin_mapping_verified": True,
        "repeated_export_stable": True,
        "full_design_pin_mapping_verified": False,
        "full_erc_verified": False,
        "note": "Both formats share the native database engine. Selected pins are corroborated against live instance paths. Exported net names are scoped to this design/report, not permanent UIDs. No unselected hidden/multi-unit pin or PCB/library consistency claim.",
    }
    (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
