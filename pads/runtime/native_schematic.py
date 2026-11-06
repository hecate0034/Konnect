"""COM 66 schematic operations on a configured disposable fixture only."""

import copy
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pads"))
from runtime.native_designer import connect, call
from runtime.native_edit import component_state, locate
from runtime.native_geometry import component_geometry, net_state, exact_grid
from runtime.geometry import canonical, wire_issues
from runtime.schematic_plan import plan, verify_change, find_component, normalize
from runtime.edits import check_fixture
from runtime.diagnostics import fingerprint
from runtime.sessions import PadsError


def read_sheet(app, design, sheet):
    document = existing_sheet(app, design, sheet)
    call(document, "Activate")
    view = app.ActiveView
    if (
        str(view.Block.SheetNum) != sheet
        or str(call(view, "GetTopLevelDesignName")) != design
    ):
        raise PadsError(
            "target_mismatch",
            "Active view does not match the requested design and sheet",
        )
    components, nets = [], []
    for item in call(view, "Query", 128, 0):
        if str(item.Parent.SheetNum) != sheet:
            continue
        target = {
            "design": design,
            "sheet": sheet,
            "uid": str(item.UID),
            "path": str(call(item, "GetName", 3)),
            "refdes": str(item.Refdes),
        }
        value = component_state(item, target, geometry=False)
        value.update(
            component_geometry(item, wires=False),
            symbol_partition=str(item.SymbolBlock.LibraryName),
        )
        components.append(value)
        if len(components) > 500:
            raise PadsError(
                "sheet_too_large", "P2 sheet limit is 500 component objects"
            )
    for net in call(view, "Query", 32, 0):
        if str(net.Parent.SheetNum) == sheet:
            nets.append(net_state(net))
        if len(nets) > 2000:
            raise PadsError("sheet_too_large", "P2 sheet limit is 2000 net objects")
    if not components:
        raise PadsError("sheet_not_found", "Existing nonempty schematic sheet required")
    return normalize(
        {
            "design": design,
            "sheet": sheet,
            "components": canonical(components),
            "nets": canonical(nets),
        }
    )


def existing_sheet(app, design, sheet):
    docs = call(app, "SchematicSheetDocuments")
    sheets = call(docs, "GetAvailableSheets", design)
    if sheet not in [
        str(call(sheets, "GetItem", i)) for i in range(1, call(sheets, "GetCount") + 1)
    ]:
        raise PadsError("sheet_not_found", "Only an existing sheet may be opened")
    return call(docs, "Open", design, sheet)


def apply_edit(app, op):
    item = locate(app, op["target"])
    changes = op["changes"]
    if "attribute" in changes:
        attr = call(item, "FindAttribute", changes["attribute"]["name"])
        attr.InstanceValue = changes["attribute"]["value"]
    if "position" in changes:
        call(item, "SetLocation", changes["position"]["x"], changes["position"]["y"])


def apply_place(app, prepared):
    op = prepared["operation"]
    source = locate(app, op["source"])
    candidate = prepared["candidate"]
    # Native AddPartInstance sets the part name but does not copy instance values/pin numbers.
    # Materialize values only on the newly created design instance, never in the library.
    item = call(
        source.Parent,
        "AddPartInstance",
        candidate["partition"],
        candidate["part_name"],
        candidate["symbol"],
        op["position"]["x"],
        op["position"]["y"],
    )
    item.Refdes = op["refdes"]
    source_state = find_component(prepared["before"], op["source"])
    for attr in source_state["attributes"]:
        if attr["name"] == "Ref Designator":
            continue
        existing = call(item, "FindAttribute", attr["name"])
        if not existing:
            existing = call(
                item,
                "AddAttribute",
                attr["name"] + "=" + attr["effective_value"],
                op["position"]["x"],
                op["position"]["y"],
                0,
            )
        if attr["effective_value"]:
            existing.InstanceValue = attr["effective_value"]
    numbers = {p["uid"]: p["number"] for p in source_state["pins"]}
    for conn in call(item, "GetConnections"):
        conn.CompPin.Number = numbers[str(conn.CompPin.Pin.UID)]
    return str(item.UID)


def apply_connection(app, op):
    # Repeated DesignComponents calls can invalidate earlier COM component handles
    # on VX.2.4. Resolve both endpoints from one retained active-sheet collection.
    view = app.ActiveView
    block = view.Block
    collection = call(view, "Query", 128, 0)
    items = list(collection)
    pins = []
    for endpoint in (op["a"], op["b"]):
        target = endpoint["target"]
        matches = [
            i
            for i in items
            if str(i.UID) == target["uid"]
            and str(i.Refdes) == target["refdes"]
            and str(call(i, "GetName", 3)) == target["path"]
            and str(i.Parent.SheetNum) == target["sheet"]
        ]
        if len(matches) != 1:
            raise PadsError("component_not_unique", "Endpoint component changed")
        connections = call(matches[0], "GetConnections")
        found = [
            c.CompPin
            for c in connections
            if str(c.CompPin.Pin.UID) == endpoint["pin_uid"]
        ]
        if len(found) != 1:
            raise PadsError("pin_not_unique", "Endpoint pin changed")
        pins.append(found[0])
    pa, pb = call(pins[0], "GetLocation"), call(pins[1], "GetLocation")
    return str(
        call(
            block,
            "AddNet",
            int(pa.X),
            int(pa.Y),
            int(pb.X),
            int(pb.Y),
            pins[0],
            pins[1],
            0,
        ).UID
    )


def apply_route(app, prepared, identity, initial):
    import pythoncom
    from win32com.client import VARIANT

    null = VARIANT(pythoncom.VT_DISPATCH, None)
    points = prepared["route"]["points"]
    existing = prepared["route"]["existing_net"]
    if existing:
        points = list(reversed(points))
    uid = existing["uid"] if existing else None
    for a, b in zip(points, points[1:]):
        if identity() != initial:
            raise PadsError(
                "target_changed", "Target changed during route construction"
            )
        net = call(app.ActiveView.Block, "AddNet", *a, *b, null, null, 0)
        actual_uid = str(net.UID)
        if uid is not None and actual_uid != uid:
            raise PadsError(
                "readback_mismatch", "Native route changed the existing net identity"
            )
        uid = actual_uid


def recover(app, original, observed):
    before = original["before"]
    op = original["operation"]
    kind = original["kind"]
    if kind == "route" and original["route"]["existing_net"]:
        uid = original["route"]["existing_net"]["uid"]
        prior = original["route"]["existing_net"]["segments"]
        old_edges = {(tuple(s["a"]), tuple(s["b"])) for s in prior}
        net = next(n for n in call(app.ActiveView, "Query", 32, 0) if str(n.UID) == uid)
        block = net.Parent
        call(block, "DeSelectAll")
        segments = list(call(net, "GetSegments"))
        for s in segments:
            from runtime.native_geometry import point

            edge = (
                tuple(point(call(s, "Location", 0))),
                tuple(point(call(s, "Location", 1))),
            )
            if edge not in old_edges:
                call(net, "SelectSegment", s)
        # Confirm selection is exactly the added segments before deleting anything.
        for s in segments:
            edge = (
                tuple(point(call(s, "Location", 0))),
                tuple(point(call(s, "Location", 1))),
            )
            if bool(call(net, "IsSegmentSelected", s)) != (edge not in old_edges):
                raise PadsError(
                    "recovery_unsafe",
                    "Native segment selection differs from recovery plan",
                )
        call(block, "DeleteSelected", False)
    elif kind == "edit":
        item = locate(app, op["target"])
        baseline = find_component(before, op["target"])
        if "attribute" in op["changes"]:
            name = op["changes"]["attribute"]["name"]
            value = next(a for a in baseline["attributes"] if a["name"] == name)[
                "instance_value"
            ]
            attr = call(item, "FindAttribute", name)
            if value:
                attr.InstanceValue = value
            else:
                call(attr, "DeleteInstanceValue")
        if "position" in op["changes"]:
            call(
                item,
                "SetLocation",
                baseline["position"]["x"],
                baseline["position"]["y"],
            )
    else:
        if kind == "place":
            old = {c["target"]["uid"] for c in before["components"]}
            added = [c for c in observed["components"] if c["target"]["uid"] not in old]
            if len(added) != 1 or any(p["net_uid"] for p in added[0]["pins"]):
                raise PadsError(
                    "recovery_unsafe",
                    "Only the recorded unconnected placed component can be removed",
                )
            item = locate(app, added[0]["target"])
            block = item.Parent
        else:
            old = {n["uid"] for n in before["nets"]}
            added = [n for n in observed["nets"] if n["uid"] not in old]
            if len(added) != 1:
                raise PadsError(
                    "recovery_unsafe", "Expected exactly one recorded new net"
                )
            matches = [
                n
                for n in call(app, "DesignNets", "", before["design"], "-1", "", True)
                if str(n.Parent.SheetNum) == before["sheet"]
                and str(n.UID) == added[0]["uid"]
            ]
            if len(matches) != 1:
                raise PadsError(
                    "recovery_unsafe", "New net no longer resolves uniquely"
                )
            item = matches[0]
            block = item.Parent
        call(block, "DeSelectAll")
        call(item, "_SetSelected", True)
        call(block, "DeleteSelected", False)


def recovery_allowed(original, observed):
    """Exact successful state, or a known attribute-only prefix of a failed edit."""
    try:
        verify_change(original, observed)
        return
    except PadsError:
        pass
    if original["kind"] == "route":
        from runtime.routing import verify_route

        for completed in range(1, len(original["route"]["points"]) - 1):
            try:
                verify_route(original, observed, completed)
                return
            except PadsError:
                pass
    if original["kind"] == "edit" and set(original["operation"]["changes"]) == {
        "attribute",
        "position",
    }:
        op = copy.deepcopy(original["operation"])
        del op["changes"]["position"]
        prefix = plan(original["before"], op)
        if prefix["expected"] == observed:
            return
    if original["kind"] == "edit" and "position" in original["operation"]["changes"]:
        # A failed native move may snap coordinates and reroute only its attached nets.
        # Ownership is established by the supervisor's recorded immediate post-state;
        # preview recovery additionally requires that the entire sheet still matches it.
        from runtime.geometry import move_geometry
        from runtime.schematic_plan import normalize

        candidate = copy.deepcopy(original["before"])
        op = original["operation"]
        actual = find_component(observed, op["target"])
        comp = find_component(candidate, op["target"])
        attached = {p["net_uid"] for p in comp["pins"] if p["net_uid"]}
        if "attribute" in op["changes"]:
            change = op["changes"]["attribute"]
            for a in comp["attributes"]:
                if a["name"] == change["name"]:
                    actual_attr = next(
                        x for x in actual["attributes"] if x["name"] == a["name"]
                    )
                    if actual_attr["instance_value"] == change["value"]:
                        a.update(
                            instance_value=change["value"],
                            effective_value=change["value"],
                        )
        move_geometry(comp, actual["position"], [])
        observed_nets = {n["uid"]: n for n in observed["nets"]}
        for i, n in enumerate(candidate["nets"]):
            if (
                n["uid"] in attached
                and n["uid"] in observed_nets
                and n["name"] == observed_nets[n["uid"]]["name"]
            ):
                candidate["nets"][i] = copy.deepcopy(observed_nets[n["uid"]])
        if normalize(candidate) == normalize(observed) and wire_issues(
            observed["components"], observed["nets"]
        ) == wire_issues(original["before"]["components"], original["before"]["nets"]):
            return
    raise PadsError(
        "recovery_unsafe",
        "Recorded changes cannot be safely inverted automatically; retain copy for inspection",
    )


def operate(request):
    attempted = lifecycle = False
    app = identity = None
    try:
        if request.get("com_version") != "66":
            raise PadsError("unsupported_version", "COM 66 is required")
        root = check_fixture(request["policy"], request["expected_path"])
        app, identity = connect(request)
        initial = identity()
        data = call(app, "GetProjectData")
        for p in (data.iCDBDir, data.CentralLibraryPath):
            if not Path(p).resolve(strict=True).is_relative_to(root):
                raise PadsError(
                    "write_scope_denied", "Database/library is outside the test copy"
                )
        library_sha = fingerprint(Path(data.CentralLibraryPath))
        design, sheet = request["design"], request["sheet"]
        current = read_sheet(app, design, sheet)
        if identity() != initial:
            raise PadsError("target_changed", "Project changed while reading")
        action = request["action"]
        if action == "check":
            from runtime.electrical import native_connectivity, run_verify

            connectivity = native_connectivity(app, current)
            check = run_verify(
                Path(request["expected_path"]), design, sheet, request["output_dir"]
            )
            if read_sheet(app, design, sheet) != current or identity() != initial:
                raise PadsError(
                    "check_changed_design",
                    "Sheet or application changed during native verification",
                )
            return {
                "ok": True,
                "connectivity": connectivity,
                "native_verify": check,
                "geometry_issues": wire_issues(current["components"], current["nets"]),
                "identity": initial,
                "project_write_attempted": False,
            }
        if action == "inspect":
            return {
                "ok": True,
                "sheet": current,
                "geometry_issues": wire_issues(current["components"], current["nets"]),
                "identity": initial,
                "library_sha256": library_sha,
                "project_write_attempted": False,
            }
        if action == "preview":
            prepared = plan(current, request["operation"])
            # Refdes uniqueness must be design-wide, not only local to the sheet.
            if prepared["kind"] == "place" and any(
                str(i.Refdes).casefold() == request["operation"]["refdes"].casefold()
                for i in call(app, "DesignComponents", "", design, "-1", "", True)
            ):
                raise PadsError(
                    "refdes_exists",
                    "Reference designator already exists in this design",
                )
            return {
                "ok": True,
                "plan": prepared,
                "identity": initial,
                "library_sha256": library_sha,
                "project_write_attempted": False,
            }
        if (
            current != request["expected_sheet"]
            or library_sha != request["library_sha256"]
        ):
            raise PadsError(
                "stale_preview",
                "Sheet geometry, properties, connectivity or library changed",
            )
        if action == "reopen":
            existing_sheet(app, design, sheet)
            if identity() != initial:
                raise PadsError("target_changed", "Target changed before close")
            lifecycle = True
            if not call(app, "CloseProject") or call(app, "IsProjectOpened"):
                raise PadsError("close_failed", "Project did not close")
            call(app, "OpenProject", request["expected_path"])
            if (
                Path(call(call(app, "GetProjectData"), "GetProjectFilePath")).resolve()
                != Path(request["expected_path"]).resolve()
            ):
                raise PadsError("target_changed", "Wrong project after reopening")
            existing_sheet(app, design, sheet)
        else:
            existing_sheet(app, design, sheet)
            if call(app, "IsReadOnlyMode") or call(app.ActiveView.Block, "IsReadOnly"):
                raise PadsError("document_read_only", "Sheet is read-only")
            if action == "recover":
                recovery_allowed(request["plan"], current)
            elif action == "execute":
                if plan(current, request["plan"]["operation"]) != request["plan"]:
                    raise PadsError(
                        "invalid_preview", "Native plan differs from approved preview"
                    )
                if request["plan"]["kind"] == "place" and any(
                    str(i.Refdes).casefold()
                    == request["plan"]["operation"]["refdes"].casefold()
                    for i in call(app, "DesignComponents", "", design, "-1", "", True)
                ):
                    raise PadsError(
                        "refdes_exists", "Reference designator was assigned elsewhere"
                    )
            else:
                raise PadsError("invalid_action", "Unknown schematic action")
            if identity() != initial:
                raise PadsError("target_changed", "Project changed before writing")
            if time.time() > request["expires_at"]:
                raise PadsError(
                    "preview_expired", "Preview expired before native write"
                )
            attempted = True
            with exact_grid(app):
                if action == "recover":
                    recover(app, request["plan"], current)
                elif request["plan"]["kind"] == "edit":
                    apply_edit(app, request["plan"]["operation"])
                elif request["plan"]["kind"] == "place":
                    apply_place(app, request["plan"])
                elif request["plan"]["kind"] == "route":
                    apply_route(app, request["plan"], identity, initial)
                else:
                    apply_connection(app, request["plan"]["operation"])
        actual = read_sheet(app, design, sheet)
        if identity() != initial:
            raise PadsError("target_changed", "Project changed after operation")
        if action == "execute":
            verify_change(request["plan"], actual)
        elif actual != (request["plan"]["before"] if action == "recover" else current):
            raise PadsError(
                "readback_mismatch",
                "Recovery/reopen sheet differs from expected geometry and connectivity",
            )
        return {
            "ok": True,
            "sheet": actual,
            "identity": initial,
            "project_write_attempted": attempted,
            "lifecycle_attempted": lifecycle,
        }
    except Exception as exc:
        result = {
            "ok": False,
            "error": {
                "kind": (
                    exc.kind
                    if isinstance(exc, PadsError)
                    else "native_schematic_failed"
                ),
                "message": (
                    str(exc)
                    if isinstance(exc, PadsError)
                    else "Native operation failed; inspect the recorded copy"
                ),
            },
            "project_write_attempted": attempted,
            "lifecycle_attempted": lifecycle,
        }
        if not isinstance(exc, PadsError):
            result["error"]["exception_type"] = type(exc).__name__
            if isinstance(getattr(exc, "hresult", None), int):
                result["error"]["hresult"] = exc.hresult
        if attempted and app is not None:
            try:
                if identity() == initial:
                    result["sheet"] = read_sheet(
                        app, request["design"], request["sheet"]
                    )
            except Exception:
                pass
        return result


if __name__ == "__main__":
    print(
        json.dumps(
            operate(json.loads(sys.stdin.read(4 * 1024 * 1024))), ensure_ascii=False
        )
    )
