"""Bounded BAMPlus impact model. Never an executable or inverse BAM generator."""

from collections import defaultdict
import re

from .geometry import canonical
from .logical import REF, NUMBER
from .sessions import PadsError

ATTRIBUTES = ("Part Name", "Part Label", "Tolerance")
FORMAT = "pads_bam_impact_v1"


def require(ok, message):
    if not ok:
        raise PadsError("backannotation_unsupported", message)


def parse_bam(text):
    require(
        len(text) <= 1024 * 1024 and "\x00" not in text, "Invalid BAM size/encoding"
    )
    records, current, scope = [], None, None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        # Native comments are single-line; their old values are corroborated with
        # live state, never trusted as the sole preimage. Never execute any text.
        comments = re.findall(r"/\*([^*]*)\*/", line)
        line = re.sub(r"/\*[^*]*\*/", "", line).strip()
        if not line:
            continue
        change = re.fullmatch(r"CHANGE '([^'\r\n]+)'\s+INSTANCE", line)
        select = re.fullmatch(r"SELECT COMP (\$[A-Za-z0-9]+)", line)
        param = re.fullmatch(r"PARAM (15|23|'[^']+') = ('[^']*'|DELETE)", line)
        pin = re.fullmatch(r"PIN_PARAM '([^']+)' 4 = '([^']+)'", line)
        if change:
            require(scope is None or current is not None, "Empty BAM scope")
            scope, current = change[1], None
        elif select and scope:
            current = {
                "scope": scope,
                "uid": select[1],
                "attributes": {},
                "pins": [],
                "refdes": None,
                "delete_fixed": False,
            }
            records.append(current)
        elif param and current:
            key, value = param[1], param[2]
            if key == "15":
                old = (
                    re.fullmatch(
                        r"\s*Reference Desig was (" + REF + r")\s*", comments[0]
                    )
                    if len(comments) == 1
                    else None
                )
                require(
                    old
                    and current["refdes"] is None
                    and re.fullmatch("'" + REF + "'", value),
                    "Invalid or duplicate reference change",
                )
                current["refdes"] = {"before": old[1], "after": value[1:-1]}
            elif key == "23":
                require(
                    value == "DELETE" and not current["delete_fixed"],
                    "Unsupported component flag assignment",
                )
                current["delete_fixed"] = True
            else:
                name = key[1:-1]
                require(
                    name in ATTRIBUTES
                    and name not in current["attributes"]
                    and value != "DELETE",
                    "Unsupported or duplicate property assignment",
                )
                current["attributes"][name] = value[1:-1]
        elif pin and current:
            old = (
                re.fullmatch(r"\s*was: (" + NUMBER + r")\s*", comments[0])
                if len(comments) == 1
                else None
            )
            require(
                old
                and re.fullmatch(NUMBER, pin[2])
                and pin[1] not in {p["name"] for p in current["pins"]},
                "Invalid or duplicate physical pin assignment",
            )
            current["pins"].append({"name": pin[1], "before": old[1], "after": pin[2]})
        else:
            raise PadsError(
                "backannotation_unsupported",
                f"Unsupported BAM syntax at line {lineno}; no executable preview recorded",
            )
    require(scope is None or current is not None, "Empty BAM scope")
    require(
        len(records) <= 512 and sum(len(r["pins"]) for r in records) <= 256,
        "BAM exceeds bounded inspection limits",
    )
    require(
        len({(r["scope"], r["uid"]) for r in records}) == len(records),
        "Duplicate BAM component selection",
    )
    require(
        all(
            r["attributes"] or r["pins"] or r["refdes"] or r["delete_fixed"]
            for r in records
        ),
        "Empty BAM component selection",
    )
    return canonical(records)


def impact(records, live, observed_pins, graph, labels=None):
    lookup = {(r["scope"], r["uid"]): r for r in live}
    require(len(lookup) == len(live), "Ambiguous hierarchical live identity")
    net_index = {
        (p["refdes"], p["pin_number"]): n["name"]
        for n in graph["nets"]
        for p in n["pins"]
    }
    declared = {c["refdes"]: c for c in graph["components"]}
    targets, pin_changes, attributes, flags = [], [], [], []
    proposed_refs = {(r["scope"], r["uid"]): r["refdes"] for r in live}
    numbered = defaultdict(set)
    for record in records:
        key = record["scope"], record["uid"]
        require(key in lookup, "BAM component does not uniquely match a live instance")
        row = lookup[key]
        rename = record["refdes"]
        require(
            not rename or rename["before"] == row["refdes"],
            "BAM reference preimage disagrees with live instance",
        )
        new_ref = rename["after"] if rename else row["refdes"]
        proposed_refs[key] = new_ref
        if rename:
            targets.append(
                {"scope": key[0], "uid": key[1], "instance_path": row["path"], **rename}
            )
        for name, value in record["attributes"].items():
            old = row["attributes"][name]
            attributes.append(
                {
                    "scope": key[0],
                    "uid": key[1],
                    "name": name,
                    "before": old,
                    "proposed_value": value,
                    "effective_value_changes": old is None or old["effective"] != value,
                    "instance_override_presence_verified": False,
                }
            )
        if record["delete_fixed"]:
            flags.append(
                {
                    "scope": key[0],
                    "uid": key[1],
                    "parameter": 23,
                    "operation": "delete",
                    "preimage_verified": False,
                }
            )
        for pin in record["pins"]:
            matches = [
                p
                for p in observed_pins
                if (p["scope"], p["uid"], p["name"]) == (*key, pin["name"])
            ]
            require(
                len(matches) == 1 and matches[0]["number"] == pin["before"],
                "BAM pin preimage disagrees with unique live pin",
            )
            observed = matches[0]
            if labels is not None:
                require(
                    labels.get(row["refdes"], {}).get(pin["before"]) == pin["name"],
                    "Live pin name disagrees with SCT physical pin label",
                )
            require(
                row["refdes"] in declared
                and pin["before"] in declared[row["refdes"]]["pin_numbers"],
                "BAM pin missing from clean exported graph",
            )
            net = net_index.get((row["refdes"], pin["before"]))
            require(
                (net is None) == (observed["net_uid"] is None),
                "Live/exported pin connection disagrees",
            )
            numbered[(row["refdes"], pin["before"])].add((new_ref, pin["after"]))
            pin_changes.append(
                {
                    "scope": key[0],
                    "uid": key[1],
                    "pin_uid": observed["pin_uid"],
                    "name": pin["name"],
                    "before": {"refdes": row["refdes"], "pin_number": pin["before"]},
                    "proposed": {"refdes": new_ref, "pin_number": pin["after"]},
                    "logical_net": net,
                }
            )
    packages, incoming = defaultdict(set), defaultdict(set)
    for key, row in lookup.items():
        packages[row["refdes"]].add(proposed_refs[key])
        incoming[proposed_refs[key]].add(row["refdes"])
    package_changes = [
        {"before": ref, "proposed": sorted(dest), "split": len(dest) > 1}
        for ref, dest in sorted(packages.items())
        if dest != {ref}
    ]
    merges = [
        {"proposed": ref, "before": sorted(old)}
        for ref, old in sorted(incoming.items())
        if len(old) > 1
    ]
    membership, unresolved, destination_nets = [], [], defaultdict(set)
    for net in graph["nets"]:
        for old in net["pins"]:
            ref, number = old["refdes"], old["pin_number"]
            options = numbered.get((ref, number), set())
            shared = number in declared[ref].get("repeated_pin_declarations", [])
            if options and (len(options) != 1 or shared):
                reason = "shared_or_conflicting_physical_pin"
                proposed = None
            elif options:
                new_ref, new_number = next(iter(options))
                proposed = {"refdes": new_ref, "pin_number": new_number}
            elif len(packages.get(ref, set())) == 1:
                proposed = {"refdes": next(iter(packages[ref])), "pin_number": number}
            else:
                reason = "split_package_requires_complete_slot_and_hidden_pin_mapping"
                proposed = None
            if proposed is None:
                unresolved.append({"net": net["name"], "before": old, "reason": reason})
            else:
                destination_nets[(proposed["refdes"], proposed["pin_number"])].add(
                    net["name"]
                )
                if old != proposed:
                    membership.append(
                        {"net": net["name"], "before": old, "proposed": proposed}
                    )
    collisions = [
        {"proposed": {"refdes": key[0], "pin_number": key[1]}, "nets": sorted(nets)}
        for key, nets in sorted(destination_nets.items())
        if len(nets) > 1
    ]
    blockers = ["execution_and_inverse_not_implemented"] if records else []
    if flags:
        blockers.append("fixed_flag_preimage_unverified")
    if attributes:
        blockers.append("attribute_override_ownership_unverified")
    if unresolved:
        blockers.append("incomplete_projected_network_membership")
    if collisions:
        blockers.append("projected_pin_net_collision")
    if merges or any(p["split"] for p in package_changes):
        blockers.append("package_split_or_merge_requires_library_slot_proof")
    return {
        "format": FORMAT,
        "component_changes": targets,
        "package_changes": package_changes,
        "package_merges": merges,
        "pin_changes": pin_changes,
        "attribute_assignments": attributes,
        "flag_changes": flags,
        "network_impact": {
            "kind": "proposal_projection_not_post_execution_netlist",
            "membership_changes": membership,
            "unresolved_members": unresolved,
            "pin_collisions": collisions,
            "complete": not unresolved and not collisions,
            "connectivity_after_execution_verified": False,
        },
        "summary": {
            "selected_instances": len(records),
            "reference_changes": len(targets),
            "pin_changes": len(pin_changes),
            "attribute_assignments": len(attributes),
            "effective_attribute_changes": sum(
                a["effective_value_changes"] for a in attributes
            ),
            "fixed_flag_deletions": len(flags),
            "projected_membership_changes": len(membership),
            "unresolved_members": len(unresolved),
        },
        "execution_allowed": False,
        "write_authorized": False,
        "recovery": {
            "status": "conditions_only_not_verified" if records else "not_needed",
            "automatic_inverse_available": False,
            "blockers": sorted(blockers),
            "conditions": [
                "Before any future execution: close the editor and capture a complete, independently reopenable project/library checkpoint; this preview does not create one.",
                "Bind all affected instances by full path and UID; verify the exact reference/pin/attribute preimages and native tool/PDB hashes again.",
                "Prove package/slot/shared/hidden-pin mappings, collision-free temporary renaming and attribute override/fixed-flag ownership before defining an inverse.",
                "After execution and after recovery: independently read live identities/pins/attributes, compare clean SCT/Allegro graphs, and close/reopen. Require the recovery graph to equal the original baseline.",
                "Any intervening edit, uncertain native outcome or incomplete inverse requires retaining the copy for inspection; do not blindly replay BAM or restore files into an open database.",
            ],
        },
    }
