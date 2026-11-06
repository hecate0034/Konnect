"""Read-only PDB slot evidence; ambiguity never authorizes a schematic edit."""

from collections import Counter
import re

from .geometry import canonical
from .logical import REF, NUMBER
from .sessions import PadsError

FORMAT = "pads_project_pdb_slot_mapping_v1"


def require(value, message):
    if not value:
        raise PadsError("invalid_pin_mapping_source", message)


def validate_refs(refdes):
    if (
        not isinstance(refdes, list)
        or not 1 <= len(refdes) <= 4
        or any(not isinstance(r, str) or not re.fullmatch(REF, r) for r in refdes)
        or len(set(refdes)) != len(refdes)
    ):
        raise PadsError(
            "invalid_mapping_query", "Require 1..4 distinct exact reference designators"
        )


def quoted(value):
    match = re.fullmatch(r'"([^"\r\n]*)"', value)
    require(match is not None, "Unsupported quoted PDB field")
    return match[1]


def nodes(text):
    require(
        len(text) <= 4 * 1024 * 1024 and "\x00" not in text,
        "Invalid PDB export size/encoding",
    )
    root, stack = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("!"):
            continue
        match = re.fullmatch(r"(\.+)([A-Za-z_]+)(?:\s+(.*))?", line)
        require(match is not None, "Unknown PDB line syntax")
        depth, key, value = len(match[1]), match[2], match[3] or ""
        require(depth <= len(stack) + 1 and depth <= 4, "Broken PDB hierarchy")
        node = {"key": key, "value": value, "children": []}
        if depth == 1:
            root.append(node)
        else:
            stack[depth - 2]["children"].append(node)
        stack = stack[: depth - 1] + [node]
    return root


def children(node, allowed):
    require(
        all(n["key"] in allowed for n in node["children"]),
        "Unsupported PDB mapping field",
    )
    return node["children"]


def one(items, key):
    found = [n for n in items if n["key"] == key]
    require(len(found) == 1, "Missing/duplicate PDB " + key)
    return found[0]


def parse_part(text, number):
    tree = nodes(text)
    require(
        one(tree, "Filetype")["value"] == "ASCII_PDB"
        and quoted(one(tree, "Version")["value"]) == "02.01",
        "Unsupported PDB format/version",
    )
    require(
        all(
            n["key"]
            in {"Filetype", "Version", "LIBRARY_DB_TYPE", "Units", "Notation", "Number"}
            for n in tree
        ),
        "Unsupported PDB header",
    )
    part = one(tree, "Number")
    for header in tree:
        if header["key"] != "Number":
            children(header, set())
    require(
        quoted(part["value"]) == number,
        "Exported part differs from requested effective Part Number",
    )
    fields = children(
        part,
        {
            "Name",
            "Label",
            "Desc",
            "RefPrefix",
            "TopCell",
            "BottomCell",
            "AltCell",
            "Modified",
            "Prop",
            "SwapGroup",
            "Symbol",
            "Slots",
        },
    )
    groups, symbols, slots = {}, {}, []
    for node in fields:
        if node["key"] != "SwapGroup":
            continue
        name = quoted(node["value"])
        require(name not in groups, "Duplicate swap group")
        items = children(node, {"SwapID", "SwapIDProperties"})
        for item in items:
            if item["key"] == "SwapID":
                children(item, set())
        ids = [quoted(n["value"]) for n in items if n["key"] == "SwapID"]
        require(ids and len(set(ids)) == len(ids), "Empty/duplicate swap IDs")
        properties = {}
        for item in items:
            if item["key"] != "SwapIDProperties":
                continue
            swap = quoted(item["value"])
            require(
                swap in ids and swap not in properties,
                "Unknown/duplicate swap properties",
            )
            props = {}
            for prop in children(item, {"Prop"}):
                match = re.fullmatch(
                    r'"([^"\r\n]*)"\s*,\s*"([^"\r\n]*)"\s*,\s*"([^"\r\n]*)"',
                    prop["value"],
                )
                require(
                    match is not None and match[1] not in props,
                    "Malformed/duplicate pin property",
                )
                props[match[1]] = {"value": match[2], "type": match[3]}
            properties[swap] = props
        groups[name] = {"swap_ids": ids, "properties": properties}
    for node in fields:
        if node["key"] != "Symbol":
            continue
        name = quoted(node["value"])
        require(name.casefold() not in symbols, "Duplicate symbol mapping")
        items = children(node, {"Default", "Symbol_SwapGroup"})
        for item in items:
            if item["key"] == "Default":
                children(item, set())
        group = one(items, "Symbol_SwapGroup")
        group_name = quoted(group["value"])
        names = [quoted(p["value"]) for p in children(group, {"PinName"})]
        require(
            group_name in groups
            and len(names) == len(groups[group_name]["swap_ids"])
            and len(set(names)) == len(names),
            "Symbol/group pin count or uniqueness mismatch",
        )
        symbols[name.casefold()] = {
            "name": name,
            "group": group_name,
            "pin_names": names,
        }
    for node in children(one(fields, "Slots"), {"Slot_SwapGroup"}):
        group = quoted(node["value"])
        items = children(node, {"SlotID", "SwapCode", "PinNumber"})
        slot_id, swap_code = (
            one(items, "SlotID")["value"],
            one(items, "SwapCode")["value"],
        )
        numbers = [quoted(n["value"]) for n in items if n["key"] == "PinNumber"]
        require(
            re.fullmatch(r"[1-9][0-9]{0,5}", slot_id)
            and re.fullmatch(r"[0-9]{1,6}", swap_code),
            "Invalid slot identity/swap code",
        )
        require(
            group in groups
            and len(numbers) == len(groups[group]["swap_ids"])
            and all(re.fullmatch(NUMBER, n) for n in numbers)
            and len(set(numbers)) == len(numbers),
            "Slot pin count/number mismatch",
        )
        slots.append(
            {
                "slot_id": int(slot_id),
                "group": group,
                "swap_code": int(swap_code),
                "pin_numbers": numbers,
            }
        )
    require(
        symbols
        and slots
        and len(slots) <= 256
        and sum(len(s["pin_numbers"]) for s in slots) <= 5000
        and len({s["slot_id"] for s in slots}) == len(slots),
        "Incomplete or oversized part slots",
    )
    require(
        all(any(s["group"] == name for s in slots) for name in groups),
        "Swap group without physical slots",
    )
    return {"part_number": number, "groups": groups, "symbols": symbols, "slots": slots}


def resolve_reference(refdes, live, part, graph, labels):
    declared = next((c for c in graph["components"] if c["refdes"] == refdes), None)
    net_index = {
        p["pin_number"]: n["name"]
        for n in graph["nets"]
        for p in n["pins"]
        if p["refdes"] == refdes
    }
    issues, instances, occurrences, used = [], [], {}, set()
    physical = {n for slot in part["slots"] for n in slot["pin_numbers"]}
    if declared is None or physical != set(declared["pin_numbers"]):
        issues.append("pdb_export_pin_set_mismatch")
    if {c["part_number"] for c in live} != {part["part_number"]}:
        issues.append("conflicting_effective_part_numbers")
    for component in live:
        symbol_name = component["partition"] + ":" + component["symbol"]
        symbol = part["symbols"].get(symbol_name.casefold())
        pin_names = [p["name"] for p in component["pins"]]
        candidates = []
        if (
            symbol
            and len(set(pin_names)) == len(pin_names)
            and set(pin_names) == set(symbol["pin_names"])
        ):
            for slot in part["slots"]:
                if slot["group"] != symbol["group"]:
                    continue
                mapping = dict(zip(symbol["pin_names"], slot["pin_numbers"]))
                if all(
                    not p["number"] or p["number"] == mapping[p["name"]]
                    for p in component["pins"]
                ):
                    candidates.append((slot, mapping))
        result = {
            "instance_path": component["path"],
            "component_uid": component["uid"],
            "block_path": component["block_path"],
            "sheet": component["sheet"],
            "symbol": symbol_name,
            "candidate_slots": [s["slot_id"] for s, _ in candidates],
            "observed_pins": component["pins"],
            "pins": [],
        }
        if len(candidates) != 1:
            result["status"] = "ambiguous" if candidates else "unresolved"
            issues.append("ambiguous_slot" if candidates else "no_compatible_slot")
            instances.append(result)
            continue
        slot, mapping = candidates[0]
        if slot["slot_id"] in used:
            issues.append("slot_used_by_multiple_instances")
        used.add(slot["slot_id"])
        result.update(status="corroborated", slot_id=slot["slot_id"])
        for pin in component["pins"]:
            number = mapping[pin["name"]]
            net = net_index.get(number)
            supported = (
                number in physical
                and labels.get(refdes, {}).get(number) == pin["name"]
                and (net is None) == (pin["net_uid"] is None)
            )
            if not supported:
                issues.append("live_library_export_pin_mismatch")
                result["status"] = "conflict"
            row = {
                **pin,
                "physical_number": number,
                "number_source": (
                    "live_explicit" if pin["number"] else "unique_library_slot"
                ),
                "logical_net_name": net,
                "corroborated": supported,
            }
            result["pins"].append(row)
            occurrences.setdefault(number, []).append(
                {
                    "instance_path": component["path"],
                    "pin_uid": pin["uid"],
                    "name": pin["name"],
                    "slot_id": slot["slot_id"],
                }
            )
        instances.append(result)
    # A physical wire cannot be assigned to conflicting flattened nets. Different
    # repeated blocks must remain separate even when their local UIDs coincide.
    wire_map = {}
    for instance in instances:
        for pin in instance["pins"]:
            if pin["net_uid"] is not None:
                key = (instance["block_path"], instance["sheet"], pin["net_uid"])
                if key in wire_map and wire_map[key] != pin["logical_net_name"]:
                    issues.append("physical_net_mapping_conflict")
                wire_map[key] = pin["logical_net_name"]
    supply, unrepresented = [], []
    symbol_groups = {s["group"] for s in part["symbols"].values()}
    for slot in part["slots"]:
        if slot["slot_id"] in used:
            continue
        if slot["group"] in symbol_groups:
            unrepresented.append(slot)
            continue
        group = part["groups"][slot["group"]]
        for swap, number in zip(group["swap_ids"], slot["pin_numbers"]):
            declaration = group["properties"].get(swap, {}).get("Supply")
            value = (
                declaration["value"]
                if declaration and declaration["type"] == "Text"
                else None
            )
            net = net_index.get(number)
            corroborated = bool(
                value
                and value == net
                and number not in occurrences
                and declared
                and number in declared["pin_numbers"]
            )
            supply.append(
                {
                    "physical_number": number,
                    "slot_id": slot["slot_id"],
                    "library_supply_name": value,
                    "logical_net_name": net,
                    "corroborated": corroborated,
                    "live_endpoint_available": False,
                }
            )
            if not corroborated:
                issues.append("non_symbol_supply_unresolved")
    counts = Counter(n for s in part["slots"] for n in s["pin_numbers"])
    shared = [
        {
            "physical_number": n,
            "library_slot_count": counts[n],
            "live_occurrences": occurrences.get(n, []),
            "single_live_endpoint": len(occurrences.get(n, [])) == 1,
        }
        for n in sorted(counts)
        if counts[n] > 1
    ]
    return {
        "refdes": refdes,
        "part_number": part["part_number"],
        "status": "corroborated" if not issues else "needs_review",
        "issues": sorted(set(issues)),
        "instances": canonical(instances),
        "shared_physical_pins": shared,
        "non_symbol_supply_pins": supply,
        "unrepresented_symbol_slots": unrepresented,
        "exported_physical_pin_count": (
            len(declared["pin_numbers"]) if declared else None
        ),
        "all_selected_live_instances_corroborated": not issues,
        "write_authorized": False,
    }
