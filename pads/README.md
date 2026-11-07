# PADS native integration (development)

This is a PADS-specific derivative of Konnect, based on upstream commit
`676efce43c9779f9f9e59a9181cb0746c6452650`. The upstream AGPL-3.0-only license
and copyright notices remain applicable. Local development began 2026-11-06.
The original KiCad executable and its installed configuration are not replaced.

阶段结论和后续验收条件见 [开发记录](docs/implementation-status.md)。

## Current scope

Version 0.9.0 provides read-only project sessions and bounded Designer test-copy
editing: property/position changes, reused component placement, straight and
explicit multi-segment connections, guarded interior/corner branches, recovery, and
connectivity/electrical native profiles, and selected global/hierarchical pin checks
using fresh native netlist exports with baseline comparison, plus selected effective
project-library pin mapping evidence and nonempty backannotation impact previews.
There are 25 domain tools. Full ERC, PCB writes
and library writes remain outside the verified scope.

## Installation diagnostics (P0)

P0 supplies installation evidence, 32/64-bit registration inspection, actual
type-library member discovery, and bounded 32-bit activation probes. Activation
does not open a document or library and is **not** evidence that drawing, routing,
licensing a document, saving, or editing a library works.

Run from the repository root with Python 3.13 on Windows:

```powershell
python -m pip install --target .pads-runtime/python pywin32==312
python -m pip install pypdf==6.16.2 pytest
python pads/cli.py inspect --sdd-home D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --output .pads-runtime/evidence/installation.json
python pads/cli.py typelibs --sdd-home D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --output .pads-runtime/evidence/typelibs.json
python pads/cli.py activate --domain designer --sdd-home D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --output .pads-runtime/evidence/activation-designer.json
python -m pytest pads/tests -q
```

Activation is a developer diagnostic, not an arbitrary script or COM interface.
Only known domains can be selected. A timeout leaves server state uncertain;
inspect before retrying. Never terminate another user's process by name.
The probe closes only an application it created; it does not attach to a user's
current editor. License environment values are inherited in memory, not logged.
Application probes refuse to run if an editor with the same executable name is
already running, because some COM servers may reuse a singleton. Do not start
an editor concurrently with a probe. Probe execution is deliberately not an MCP tool.

## Read-only MCP (P1)

The standalone Rust server reuses Konnect's JSON-RPC and MCP result types.
Its group loading follows Konnect's progressive-discovery pattern; PADS schemas,
dispatch and worker isolation are implemented here. It does not link KiCad IPC,
run arbitrary scripts supplied by an agent, or require KiCad to be installed.

Ten PADS tools are available:

| Tool | Purpose |
| --- | --- |
| `get_pads_environment` | Installed product, COM registration and binary architecture |
| `get_pads_capabilities` | Historical activation/read evidence and explicitly scoped test-edit evidence |
| `list_pads_interface_members` | Member names and parameter names from the installed type library |
| `search_pads_reference` | Search all three local PDFs with page and source fingerprint |
| `get_pads_reference_page` | Retrieve a complete PDF page |
| `bind_pads_session` | Bind an existing editor to an explicit absolute `.prj` / `.pcb` path |
| `get_pads_session` | Recheck process, application instance and project identity |
| `read_pads_project` | Read pages, component identities and net identities |
| `validate_pads_snapshot` | Compare listed object state with a prior snapshot |
| `release_pads_session` | Release only the local binding, leaving the editor open |

`environment` is loaded initially. Use `list_toolboxes` and
`load_toolset {"name":"reference"}` to discover the reference tools.
Load `session` for live project tools. Both editors must already have the intended
project open; Designer also needs an existing sheet open. Multiple instances
are refused on initial binding. Bound calls select the recorded PID and verify
its creation time, application GUID, executable path and project path again.
Every reference query verifies source hashes and index page counts; changed or
missing manuals require rebuilding to a new index path. PDF text is untrusted
reference data, never execution instructions. Vendor PDFs and extracted contents
stay local under `.pads-runtime`, outside Git.

```powershell
python pads/build_reference.py --manual-dir D:/PADSPro --output .pads-runtime/reference.sqlite
cargo build --manifest-path pads/server/Cargo.toml --locked
cargo test --manifest-path pads/server/Cargo.toml --locked
cargo clippy --manifest-path pads/server/Cargo.toml --locked --all-targets -- -D warnings
python pads/smoke_mcp.py --server pads/server/target/debug/pads-automation.exe --python C:/Users/m/AppData/Local/Programs/Python/Python313/python.exe --sdd-home D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --state .pads-runtime
```

An existing index is not overwritten. The build uses the repository's pinned
Rust toolchain. Launch the executable with four existing absolute paths:

```powershell
& 'D:/Codes/Project/pads-automation/pads/server/target/debug/pads-automation.exe' `
  'C:/Users/m/AppData/Local/Programs/Python/Python313/python.exe' `
  'D:/Codes/Project/pads-automation/pads/worker.py' `
  'D:/MentorGraphics/PADSProVX.2.4/SDD_HOME' `
  'D:/Codes/Project/pads-automation/.pads-runtime'
```

Transport is newline-delimited JSON-RPC over standard input/output, protocol
version `2024-11-05`. Diagnostics belong on stderr. Each domain call uses a fresh
Python worker with a 90-second deadline for P1. P2 component tools allow 150 seconds
for the supervisor, because session preflight and native editing each have a 60-second limit.
A failure is returned as an
MCP tool error, never as successful empty data. Startup arguments are trusted
local configuration; callers cannot replace the worker or installation path.
The smoke test is specifically for the inspected VX.2.4/COM 66 installation and
the supplied 776-page Designer manual. No MCP client configuration is changed
automatically by these scripts.

## Session acceptance and scope

Example calls after loading `session`:

```json
{"name":"bind_pads_session","arguments":{"domain":"designer","expected_project":"D:/test/board/board.prj"}}
{"name":"read_pads_project","arguments":{"session_id":"<returned session_id>"}}
{"name":"validate_pads_snapshot","arguments":{"session_id":"<returned session_id>","snapshot_token":"<returned snapshot_token>"}}
{"name":"release_pads_session","arguments":{"session_id":"<returned session_id>"}}
```

Session IDs and snapshots are local to the configured state directory. Binding
and release update only the local SQLite ledger. Native read failures invalidate
the binding, including timeouts, closed applications, changed targets and modal
windows. Recovery requires an explicit new binding; no editor is started,
closed, saved, or synchronized by an MCP session tool.

The snapshot covers **only returned object identities and attributes**. It does
not cover geometry, complete pin-to-net connectivity, traces or unlisted database
state. `validate_pads_snapshot` is a read-time comparison, not a write transaction
or authorization. Read callers must still treat text from designs as reference
data, not instructions. Designer component objects include graphical/hierarchy
objects without a reference designator; they are not a physical BOM count.

Native adapters use pywin32 against existing out-of-process servers. Layout uses
the documented x86 automation licensing helper through a private pipe; challenge
and token are never logged or persisted. No arbitrary COM member is exposed.

For a repeatable sample, use the installed evaluation project and library:

```powershell
python pads/prepare_fixture.py --source D:/MentorGraphics/PADSProVX.2.4/SDD_HOME/Documentation/PADS_Professional_Eval --target D:/Codes/Project/pads-automation/.pads-runtime/my-fixture
python pads/launch_fixture.py --domain designer --sdd-home D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --fixture .pads-runtime/my-fixture --state .pads-runtime/my-host
python pads/smoke_sessions.py --server pads/server/target/debug/pads-automation.exe --python C:/Users/m/AppData/Local/Programs/Python/Python313/python.exe --sdd D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --state .pads-runtime --fixture .pads-runtime/my-fixture --domain designer
```

Repeat with `--domain layout`. Wait for `opened` in the host log before testing.
The developer host opens only a prepared copy and refuses an existing same-name
editor. Startup dialogs may require attention; it does not dismiss them or enable
forward annotation. Its 20-minute idle lifetime starts **after** opening succeeds;
startup can remain pending on a modal. Write `quit` to the returned control path
to end an opened test host. Cleanup refuses to close a changed project. These
fixture commands are not public MCP tools. Preparing a copy does not prove it is
electrically synchronized. See [P1 acceptance](docs/p1-acceptance.md) for results
and observed limitations.

## Delivery gates

1. P0: freeze source/license baseline, establish environment and interface paths.
2. P1: isolated MCP transport, read-only tools, target/state contracts, reference index.
3. P2: schematic edits on a disposable test project, save/reopen/netlist checks.
4. P3: schematic/PCB identity and native sync, placement and specified-path routing.
5. P4: selected existing objects in a test library, dependency and readback checks.
6. P5: packaged skills and three repeatable end-to-end acceptance scenarios.

General design writes remain disabled. P2 adds only the bounded test-copy tools
below. Library creation or derivation requires a concrete candidate selection
under the user's existing reuse policy. Production GLK writes are outside test execution.

## Bounded schematic edits (P2 first increment)

Four additional tools are available through `load_toolset {"name":"edit"}`:

| Tool | Purpose |
| --- | --- |
| `inspect_pads_component` | Read one exact existing component's position, attributes and pin/net identities |
| `preview_pads_component_edit` | Persist a before/after preview locally, without changing the design |
| `execute_pads_component_edit` | Consume one preview, recheck state, write and read back |
| `reopen_pads_edit` | Close/reopen the test project and verify the recorded after-state; invalidate the old binding |

These tools support Designer COM 66 only. Editing is off until a developer configures
one prepared, disposable fixture inside the runtime state directory:

```powershell
python pads/prepare_fixture.py --source D:/MentorGraphics/PADSProVX.2.4/SDD_HOME/Documentation/PADS_Professional_Eval --target D:/Codes/Project/pads-automation/.pads-runtime/p2-fixture
python pads/configure_fixture_edits.py --state .pads-runtime --fixture .pads-runtime/p2-fixture
python pads/launch_fixture.py --domain designer --sdd-home D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --fixture .pads-runtime/p2-fixture --state .pads-runtime/p2-host
```

Wait for the host's `opened` status. Existing fixture/policy paths are not overwritten.
The local policy is not an MCP tool or a security sandbox: someone who edits local
configuration/code can change it. Each operation checks the exact project, manifest,
linked paths and native database/library locations. Never configure production copies.
Use one state directory for a fixture; locks do not coordinate separate deployments.

Bind the open test project, then choose an existing physical component from
`read_pads_project`. Target fields are all required: `design`, `sheet`, `uid`, `path`,
`refdes`. A reference designator alone is insufficient. One preview can contain an
existing attribute and/or absolute x/y coordinates:

```json
{
  "session_id": "<bound session id>",
  "target": {"design":"CORPORATE","sheet":"control_buffers","uid":"$5I134","path":"$5I134","refdes":"R24"},
  "changes": {
    "attribute": {"name":"Description","value":"P2 test copy"},
    "position": {"x":580,"y":500,"unit":"0.01_inch"}
  }
}
```

Allowed existing attribute names are Description, Value, Tolerance, Power and Cost.
The adapter sets **InstanceValue** and preserves the base/library value; it does not
create attributes. Native acceptance currently covers Description; other allowed
names need their own real-project validation. Refdes, part, footprint, orientation,
library objects and connections cannot be changed by this tool. Coordinates are
integers in 1/100 inch (0.254 mm), bounded to 0..10000. Writes temporarily use a
fine native grid setting and restore the previous setting. The raw grid parameter
is not equated to the coordinate unit. No sheet-boundary check is promised.

Review the returned `before`/`after`, then pass its `preview_id` to execution. The
preview expires after ten minutes and can be executed once. Attributes, base and
instance values, coordinates, orientation, symbol name and component pin/net
identities, pin coordinates and attached wire geometry must still match. Other sheet
objects and ERC are outside this older component-only comparison; prefer the new
sheet-level tools below. Native editing is not an atomic transaction; avoid manual editing
or switching projects while the call runs. A failed setter can leave partial changes.

Designer stores schematic edits in its database immediately. `SchematicSheetDocument.Save`
is for embedded symbol editing, so this implementation deliberately does not call it.
Persistence is verified by native `CloseProject` / `OpenProject` and independent
readback. Reopen only uses an already existing sheet. It leaves the editor running
and requires a new binding afterward. This is a project-close/reopen check, not a
power-loss/durability test or whole-process restart guarantee.

On timeout/partial failure, the preview remains consumed and the binding is invalidated;
inspect the disposable project and bind again. There is no automatic retry or rollback.
Native failure results retain whether a write was attempted (`true`, `false`, or
`null` when unknown). Successful scope is reported by `bounded_test_edit_verified`;
`write_verified` remains false for unrestricted editing.

Repeatable acceptance changes R24's Description and moves it +10 native units on x:

```powershell
python pads/smoke_edits.py --server pads/server/target/debug/pads-automation.exe --python C:/Users/m/AppData/Local/Programs/Python/Python313/python.exe --sdd D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --state .pads-runtime --fixture .pads-runtime/p2-fixture
```

That first-increment script intentionally leaves its modified test copy for review.
Its historical results are recorded in [P2 first acceptance](docs/p2-acceptance.md).

## P2 second increment: recorded sheet changes and recovery

The `edit` toolset now also contains five tools (19 domain tools in total):

| Tool | Purpose |
| --- | --- |
| `inspect_pads_sheet` | Read recorded component, pin and wire state on one existing sheet |
| `preview_pads_schematic_change` | Preview an `edit`, `place` or `connect` operation |
| `execute_pads_schematic_change` | Consume a preview once, execute and verify its allowed delta |
| `preview_pads_schematic_recovery` | Preview the inverse of an observed, still-current change |
| `reopen_pads_schematic_change` | Close/open the project and compare persisted state |

These tools require the same configured disposable fixture and a bound Designer
session. Supply `session_id`, `design`, `sheet` and `operation` for a preview.
The `edit` operation wraps the existing exact `target` and `changes` described above.
For placement, select an existing component on the same sheet as the reuse source:

```json
{
  "kind": "place",
  "source": {"design":"CORPORATE","sheet":"Powersupply","uid":"$11I16","path":"$11I16","refdes":"C2"},
  "refdes": "C900",
  "position": {"x":300,"y":200,"unit":"0.01_inch"}
}
```

The preview identifies the existing symbol partition, symbol, part and cell metadata.
It checks an explicit new reference designator across the design. Placement creates
a design instance of that symbol, preserving effective source properties and pin
numbers. It does not create library objects or independently validate library part
packaging. The source must be unrotated, with numbered pins and complete reuse metadata.

Connection operations have the shape below. Obtain each complete `target` and
`pin_uid` from an inspection or successful placement result; do not guess UIDs:

```json
{
  "kind": "connect",
  "a": {"target":{"design":"CORPORATE","sheet":"Powersupply","uid":"<C900 UID>","path":"<C900 path>","refdes":"C900"},"pin_uid":"<first pin UID>"},
  "b": {"target":{"design":"CORPORATE","sheet":"Powersupply","uid":"<C901 UID>","path":"<C901 path>","refdes":"C901"},"pin_uid":"<second pin UID>"}
}
```

Replace all angle-bracket placeholders with values returned by inspection. Both pins
must be free, numbered and on different components of the same sheet. Only one
nonzero horizontal/vertical segment is supported. Existing wires, unrelated pins
and component-body intersections are refused. The returned net must be unnamed,
have the exact two pin attachments and expected segment, with other recorded state
unchanged. Moves of connected components must preserve straight parallel wire
segments; moves requiring native rerouting are refused before writing.

The sheet comparison records component identities, properties, orientation, bounding
boxes, pin identities/coordinates/connections, net identities/names, segment endpoints,
joint types and bus flags. It explicitly excludes the automatically updated
`@DATETIME` property of objects without reference designators. It does not cover all
text/artwork, labels, bus semantics, hierarchy or ERC. The inspection's geometry
issues report only pin-to-endpoint gaps and zero-length segments, not a full DRC.
Limits are 500 component objects and 2,000 net objects per sheet.

Execution rechecks the entire recorded sheet, project identity, fixture policy and
central-library descriptor fingerprint. Keep the copied library and editor unchanged
while calls run; the descriptor fingerprint is not a recursive library lock. State
directory locks serialize these tools but cannot lock manual GUI changes or workers
using another state directory. A native sheet call has a 180-second limit; its MCP
worker has 270 seconds. Older component edits retain their 60/150-second limits.

Recovery is explicit: pass a successful or observed failed change's `preview_id` as
`change_id` to `preview_pads_schematic_recovery`, review its before/target state,
then execute the returned recovery preview with `execute_pads_schematic_change`.
Recover later changes first (wire, last placed component, then earlier component).
Any subsequent recorded-state difference refuses recovery. Known partial attribute/
move results can be recovered; unknown timeout results and unrecognized partial
placements/connections require inspection and are not blindly retried. Recovery
itself can fail and is not an atomic rollback. Reopen invalidates its binding;
bind again before the next operation. Recovery restores recorded logical/geometry
state, not byte-identical database files.

Repeatable acceptance on a fresh configured fixture runs edit/reopen/recovery,
two placements, specified-pin connection, independent readback, reopen, reverse-order
recovery, and a final reopen/baseline comparison:

```powershell
python pads/prepare_fixture.py --source D:/MentorGraphics/PADSProVX.2.4/SDD_HOME/Documentation/PADS_Professional_Eval --target .pads-runtime/p2b/fixture
python pads/configure_fixture_edits.py --state .pads-runtime/p2b --fixture .pads-runtime/p2b/fixture
python pads/launch_fixture.py --domain designer --sdd-home D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --fixture .pads-runtime/p2b/fixture --state .pads-runtime/p2b/host --lifetime-minutes 120
python pads/smoke_schematic.py --server pads/server/target/debug/pads-automation.exe --python C:/Users/m/AppData/Local/Programs/Python/Python313/python.exe --sdd D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --state .pads-runtime/p2b --fixture .pads-runtime/p2b/fixture
```

Wait for the fixture host to report `opened` before starting acceptance. Existing
fixture/policy paths are not overwritten. The script restores its recorded baseline
and verifies source files plus the copied library/PCB hashes. See
[P2 second acceptance](docs/p2b-acceptance.md) for measured results and known limits.
The next increment below adds bounded multi-segment routes and native checks.
Full P2 electrical validation, P3 PCB edits and P4 library writes remain pending.

## P2 third increment: explicit routes and native connectivity checks

Version 0.4.0 adds a `route` operation to the existing preview/execute/recovery tools,
plus `check_pads_schematic` (20 domain tools total). Existing `connect` retains its
single-straight-segment contract. A route specifies every vertex, including endpoints:

```json
{
  "kind": "route",
  "a": {"target":{"design":"CORPORATE","sheet":"Powersupply","uid":"<source UID>","path":"<source path>","refdes":"C902"},"pin_uid":"<pin UID>"},
  "b": {"net_uid":"<existing net UID>","net_name":"<exact net name>","point":[600,400]},
  "points": [[800,300],[800,400],[600,400]],
  "unit": "0.01_inch"
}
```

Replace placeholders with inspected identities. Alternatively `b` can use the same
`target`/`pin_uid` structure as `a`, for another free pin. Routes allow 1–8 nonzero
orthogonal segments. Redundant collinear vertices, backtracking, self-intersection,
unrelated pins/wires and component-body crossings are refused. The start pin must
be free; this does not merge two existing nets. Existing-net anchors may be wire corners/solder joints or an unambiguous orthogonal
segment interior. Interior splitting requires native confirmation that the segment
has no label (including invisible labels) or attributes; bus and pin joints are refused.
The original segment is split with a solder joint while retaining the net UID.
Recovery selects only explicit added route edges, allowing native merging of the
original two pieces; it never selects all edges missing from the old geometry.
Named nets require both the exact native UID and exact current name; no name-only lookup.
Pin paths must leave perpendicular to one unambiguous component bounding-box edge;
tangent departures, corner pins and pins inside the bounding box are refused because
the native editor may otherwise insert an unrequested short detour. This guard also
applies to the older straight `connect` operation.

Execution begins at the existing net when extending it. Every segment must retain
that net UID. Readback checks the exact expected segments, joint types and pin
attachments, including all previously recorded state. Recovery removes only added
segments and verifies restoration of the original joint types and connectivity.
Observed exact construction prefixes can also be recovered; unrecognized partial
states or unknown timeout outcomes remain non-retryable and require inspection.
No general Undo command, whole-net replacement or automatic net renaming is used.

`check_pads_schematic` requires `session_id`, `design` and `sheet`, with an optional
`baseline_id` from an earlier successful check. It returns:

- A local physical-net connection table, independently cross-checked through
  component-to-net and native net-to-pin enumeration, plus unconnected pins.
- Native `vdrc.exe` results using `profile="connectivity"` (default): `UnconnectedPins`,
  `ZeroPin`, `NetOverlap`; or `profile="electrical"`, additionally enabling `Undriven`,
  `MultDriv`, `DriveGlobal`, `OutputDirectlyPG`, `PowerGroundPinConnection`. Unlisted
  checks are disabled; existing options in the installed definitions are preserved.
- `scope="sheet"` (default) requests the selected page with `-page`; native flat-net
  rules still use the checker's global resolution semantics. `scope="design"` omits `-page`
  and checks the root block across pages, without `-hier`. It also records root-block
  physical net identities before/after and lists cross-page same-name candidates.
  Those candidates explicitly have `electrical_equivalence_verified=false`; local
  UIDs are qualified by sheet and no same-name nets are merged. The returned pin
  table remains single-sheet. Global equivalence/hierarchy is not independently proved.
- Rule/executable fingerprints, raw local evidence, error/warning/note counts and
  findings. `status=completed` means the checker finished, not that the circuit passed.
  `profile_clean` refers only to this profile; `full_erc_verified` remains false.
- With `baseline_id`, differences in physical net membership, unconnected pins,
  native finding text and counts. Different profiles, scopes, sheets, fixtures or checker/rule
  fingerprints cannot be compared as an equivalent baseline.

The profile is generated under the state directory from installed definitions.
Neither installation defaults nor the project's `Verify.ini` is changed. The older
native checker distinguishes `-def` rule definitions from `-set` user overrides;
the generated profile is passed as definitions, with fixed empty overrides. Native
exit code zero alone is never treated as a successful check: a recognized complete
summary is required. A native check has a 90-second timeout within the existing
180-second native worker / 270-second supervisor limits. Checking verifies recorded
sheet state (including recorded segment labels/attributes) before/after and, for design
scope, root-block net identity/name inventory. It does not freeze other GUI activity,
record every other sheet's geometry, or promise byte-identical native log files.

Use a separate prepared/configured fixture, launch its Designer host, then run:

```powershell
python pads/smoke_routes.py --server pads/server/target/debug/pads-automation.exe --python C:/Users/m/AppData/Local/Programs/Python/Python313/python.exe --sdd D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --state .pads-runtime/p2c --fixture .pads-runtime/p2c/fixture
```

Acceptance deliberately creates unconnected pins as a positive control for native
checking, routes a three-segment net, adds a branch, extends an original named VDD
net, verifies persistence, then reverses changes and compares final netlist/check
results to the baseline. Existing baseline warnings are retained and reported.
See [P2 third acceptance](docs/p2c-acceptance.md) for actual outcomes.

For P2d acceptance use the same command with a separate `.pads-runtime/p2d` fixture
and `--interior`. It tests an unnamed interior branch and a named-net interior branch,
annotation refusal, persistence/recovery, electrical profiles, design-wide baseline
comparison and unchanged source/library/PCB fingerprints. See [P2 fourth acceptance](docs/p2d-acceptance.md).

Native sheet records now include `decoration_checked` and label/attribute metadata.
Create fresh previews after upgrading; old records lacking these fields intentionally
fail stale-state checks. Finish/recover old edits with the matching version before
upgrading a fixture. Records remain local for inspection.

This is not a flattened global/hierarchical pin table, complete ERC, power-integrity
simulation, hidden-pin/multi-unit verification, cross-sheet routing or PCB sync.
Annotated segment splitting and those broader capabilities remain subsequent work.

## Native logical connectivity (P2e)

`inspect_pads_logical_connectivity` is in the `edit` toolset because the same
configured disposable-fixture guard applies. It performs reads and writes local
evidence files; it does not change schematic objects or run packaging/PCB sync.

```json
{
  "session_id": "<bound session ID>",
  "design": "CORPORATE",
  "pins": [
    {"refdes": "U27", "pin_number": "15"},
    {"refdes": "U29", "pin_number": "15"}
  ]
}
```

Supply 1–16 distinct reference-designator / physical-pin-number pairs. The server
runs fixed installed `SctNetlist.exe` and `AllegroNetlist.exe` exporters against
the bound project's `DxD` snapshot / `DX` flow. It parses and compares their
flattened pin membership, then repeats the exports and live-pin reads to detect
changes. Each call uses new output paths, not old project/PCB netlist files.
The design must already be packaged. Adding a component can invalidate packaging;
`logical_design_unpacked` is reported even if a partial export file exists. The
read tool never packages, renumbers, or updates library mappings automatically.
Zero exit status alone is insufficient: diagnostics, missing/incomplete output,
conflicting pin assignments, unknown syntax and differing exports are refused.
The two exporters share Siemens' engine; this is not an independent electrical solver.

Results contain the normalized exported graph, its hash, selected live component
UIDs, **full instance paths**, sheets, pin UIDs and physical-net identities, plus
exported logical-net membership and pairwise connection results. Same local net
names or UIDs never establish a connection by themselves. Electrical-group
`E-NETS` declarations in SCT are not used to merge physical `NETS` members.
Exported net names identify nets only within that design/report; they are not
permanent database UIDs. Full graph export does not mean all physical pin mappings
were independently checked: `full_design_pin_mapping_verified` remains false.

An optional `baseline_id` compares the same project/design/selected pin set and
exporter/parser provenance. It reports graph, component declarations and selected
pin changes. Inspection failures are not saved as successful baselines. Session,
fixture, library and source database boundaries remain enforced.

Live instance pin numbers must be present and unambiguous. Some parts (e.g. U12
in the official sample) have blank instance numbers although the native exporter
resolves them through library mapping. Those selections are refused. Repeated
identical SCT power-pin declarations are retained as metadata and deduplicated
only in the declaration list; ambiguous live unit pins (e.g. U6.8) remain refused.
No hidden-pin inference, library pin mapping, shared-unit edit or full ERC claim.

Current parser limits: 5,000 exported packages, 10,000 nets, 100,000 declared pins,
10,000 live component instances and 16 MiB per export file. Each exporter has a
25-second timeout within the 180-second native worker / 270-second supervisor.
Names containing unsupported delimiter/quote syntax fail closed rather than being
silently truncated or normalized. Other local GUI changes are not locked out;
stable repeated observations are not an atomic database snapshot guarantee.

Acceptance on a separately prepared/configured/hosted fixture:

```powershell
python pads/smoke_logical.py --server pads/server/target/debug/pads-automation.exe --python C:/Users/m/AppData/Local/Programs/Python/Python313/python.exe --sdd D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --state .pads-runtime/p2e --fixture .pads-runtime/p2e/fixture
```

See [P2 fifth acceptance](docs/p2e-acceptance.md). Annotated segment splitting,
rotation, implicit library pin mapping and complete multi-unit handling remain
separate increments before PCB write development.


## Identity-preserving packaging (P2f)

Two `edit` tools close the gap between an explicitly numbered reused placement
and verified native logical connectivity:

1. Before placing the component, call `inspect_pads_logical_connectivity` and retain
   its `check_id` as the packaged design baseline.
2. Preview and execute one reused, unconnected component placement. Retain that
   placement's `preview_id` as `change_id`.
3. Call `preview_pads_packaging` with `session_id`, `change_id`, and `baseline_id`.
4. Inspect the proposed graph, fixed mode and protected-source evidence, then call
   `execute_pads_packaging` with the returned packaging `preview_id` once.
5. Use the **placement** change ID with `reopen_pads_schematic_change`, rebind,
   inspect the logical graph/pins, and use the existing guarded placement recovery
   followed by reopening to remove the unconnected test instance.

The preview runs installed `wg/win32/bin/package.exe` with fixed `Add`, `NoFill`,
`BAM-only` options and a private copy of `Integration/LocalPartsDB.pdb`. It writes
auxiliary reports and a working PDB, so it is **not a file-preserving read**. Only
an empty BAM assignment file is accepted: this increment cannot rename components,
reassign physical pin numbers, clean-build a library or process arbitrary modes.
The candidate graph must equal the prior logical baseline plus exactly the new
unconnected component. New live pin numbers must already be explicit and unique.
The tools do not fill implicit pin numbers or solve shared-unit mappings.

An unpackaged SCT export can be used only as a labelled preflight candidate, with
only its exact known unpackaged diagnostic allowed. Unpackaged Allegro exports
fall back to different logic and are not used for this candidate. The public
logical inspection tool continues to reject unpackaged designs. After packaging,
**both** clean SCT and Allegro exports must agree and match the candidate exactly.
A candidate is never recorded as a successful logical baseline.

Execution revalidates the source fingerprints and live component identities,
repeats BAM-only preflight and the candidate graph, then updates iCDB and checks
fresh dual-format exports, the new live pins, operation-page state and protected
files. The whole design's live component identities are compared; full live pin
mapping and all-page geometry verification remain false/not claimed. Project and
library paths stay inside the configured fixture. Additional project PDB search
paths are refused. Protected files include the copied library tree, PCB tree,
original PDBs and configuration files; working PDBs and logs stay in local evidence.

Previews expire after 600 seconds and execute once. Failure after a possible write
invalidates the session and retains evidence; unknown outcomes are not retried.
The successful recovery removes the recorded unconnected component and verifies
the original logical graph and recorded sheet. It is **semantic recovery**, not a
byte-for-byte rollback of packager bookkeeping or generated auxiliary reports.
Further edits make the original placement recovery stale and must be handled
in order. Native packaging is not an atomic transaction, and other GUI edits are
not locked out. Each packager has a 40-second limit within the existing
180-second native worker / 270-second supervisor limits.

Run the acceptance harness only on a prepared, configured and hosted test copy:

```powershell
python pads/smoke_packaging.py --server pads/server/target/debug/pads-automation.exe --python C:/Users/m/AppData/Local/Programs/Python/Python313/python.exe --sdd D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --state .pads-runtime/p2f --fixture .pads-runtime/p2f/fixture
```

See [P2 sixth acceptance](docs/p2f-acceptance.md). Nonempty BAM changes, assignment
of implicit/shared/hidden pins and full ERC remain separate work.

## Selected project-library pin mappings (P2g)

`inspect_pads_pin_mapping` belongs to the `edit` toolset because it requires the
same configured disposable fixture, but performs no schematic/library mutation.
Supply `session_id`, an existing `design`, and 1–4 exact distinct `refdes` values;
an optional `baseline_id` compares a previous report for the same scope and sources.

```json
{"session_id":"<bound session>","design":"CORPORATE","refdes":["U12","U6","U4"]}
```

It copies the project's effective `Integration/LocalPartsDB.pdb` into a fresh local
evidence directory, then runs the installed native `PartsDB2HKP` and `DataConvert`
on that private copy. A bounded ASCII_PDB 02.01 reader correlates symbol pin names,
swap groups and slots with full live instance paths and two clean SCT/Allegro export
rounds. This is evidence from the **project's effective PDB**; equivalence to the
current central library and correctness against a manufacturer pinout are not claimed.

The report distinguishes explicit physical numbers, blank numbers resolved by a
unique complete slot, shared physical pins across units, and non-symbol supply pins
without graphical live endpoints. Multiple candidate slots remain ambiguous; local
net names are not used to guess a slot. Conflicts return `needs_review` with observed
pins and candidate slots. `unrepresented_symbol_slots` are reported separately from
non-symbol supplies. Only selected live instances are corroborated, not every slot
or pin in the entire design.

Source PDB, protected library/PCB/configuration files, native executables, live
instances, application identity and both exported graphs must remain unchanged
during inspection. Drift rejects the read before a baseline is recorded. A changed
PDB or exporter requires a fresh baseline. Export diagnostics or unsupported formats
fail closed. DataConvert's observed exit code 1 is accepted only with silent,
well-formed, complete output for the exact requested part. Each export process has
a 25-second limit within the existing 180-second native / 270-second supervisor limits.

Limits: at most four effective parts, sixteen selected live symbol instances, 128
selected live pins and 10,000 scanned component objects; additional project PDB paths,
direct bus-segment mappings and unsupported PDB structures are refused. These reads
produce private evidence files, and the editor/database service may maintain its own
auxiliary files. They do not promise every fixture byte remains unchanged.

`write_authorized`, `full_design_pin_mapping_verified` and `full_erc_verified` remain
false. Mapping evidence does not change existing edit or packaging preconditions;
the older logical endpoint query still refuses ambiguous/shared or blank live numbers.

```powershell
python pads/smoke_pin_mapping.py --server pads/server/target/debug/pads-automation.exe --python C:/Users/m/AppData/Local/Programs/Python/Python313/python.exe --sdd D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --state .pads-runtime/p2g --fixture .pads-runtime/p2g/fixture
```

The acceptance harness uses a prepared, configured, hosted test copy and a private
close/reopen helper; it does not expose a new public lifecycle or write tool.
See [P2 seventh acceptance](docs/p2g-acceptance.md).

## Nonempty backannotation impact preview (P2h)

`preview_pads_backannotation` takes `session_id`, an existing `design`, optional
`mode` (`add` by default, or `repackage_unfixed`), and optional `baseline_id`.
It uses the configured test copy and always runs fixed native `-Add -NoFill -y`
commands with private working PDBs. `repackage_unfixed` adds `-r` and can propose
extensive renumbering and changes to package unit assignments. No arbitrary BAM,
executable path, options or application operation is accepted.

```json
{"session_id":"<bound session>","design":"CORPORATE","mode":"repackage_unfixed"}
```

The report's `impact_id` is a historical inspection record, **not** a packaging
execution token. Supplying it to `execute_pads_packaging` is refused. Existing
empty-BAM-only execution restrictions are unchanged. No BAM is applied here.

The bounded BAM reader supports native `CHANGE ... INSTANCE`, `SELECT COMP`,
reference parameter 15, physical `PIN_PARAM ... 4`, deletion of fixed parameter 23,
and assignments to `Part Name`, `Part Label` and `Tolerance`. Unknown commands,
duplicate selections/assignments, unsupported values and incomplete records fail
closed. Native log reference/pin change counts must match the parsed records.
Limits are 1 MiB per BAM/log, 512 selected instances, 256 pin changes and 1,000
scanned component objects. Other versions/formats and extra project PDB paths have
not been enabled. Each native preflight is limited to 40 seconds within the existing
180-second native worker and 270-second supervisor limits.

Full hierarchical scope and native UID identify each affected instance. Comments
about old references/pins are cross-checked with live state, SCT physical-pin names,
and clean SCT/Allegro graph membership. The report lists component changes, proposed
package splits/merges, pins, property assignments, fixed-flag deletions and projected
network membership changes. Such membership changes often mean a changed reference
or pin label on the same wire; they are not proof of a new electrical connection.
Shared/hidden pins in split packages remain explicitly unresolved where the
available evidence cannot identify a unique destination. `network_impact.complete`
means projection coverage only; post-execution connectivity is always unverified.

Two native proposals are retained. On this installation, repeated repackaging can
select different units/slots even with unchanged observed source state. The report
therefore includes `repeatability.identical` and every changed candidate instance.
An unstable proposal is useful inspection evidence and an explicit execution/recovery
blocker. It is never presented as the result a later native execution must reproduce.
The source design must already yield clean packaged SCT/Allegro baseline exports;
this tool does not treat an unpackaged fallback export as a valid baseline.

Before/after checks cover live instance identities and the three observed properties,
the first proposal's affected physical pins, clean exported graphs/labels, tool hashes,
application/project identity and protected library/PCB/PDB/configuration files.
Changes reject the read and preserve failure evidence; uncertain outcomes invalidate
the session. This does not check every geometric field or every fixed/override flag.
The packager writes auxiliary project reports and private PDB/output files, so this
is a **no-schematic-update preflight**, not a filesystem read-only operation.

Recovery is reported as conditions, not an implemented inverse: a complete closed
project/library checkpoint, exact preimages and identities, package/slot/shared-pin
proof, collision-safe renaming, fixed-flag and instance-override ownership, independent
readback and native graph/reopen comparisons. The preview does not create the checkpoint.
An unchanged effective property value does not establish identical override ownership.
`executable_preview`, `write_authorized` and `recovery_verified` remain false.

```powershell
python pads/smoke_backannotation.py --server pads/server/target/debug/pads-automation.exe --python C:/Users/m/AppData/Local/Programs/Python/Python313/python.exe --sdd D:/MentorGraphics/PADSProVX.2.4/SDD_HOME --state .pads-runtime/p2h --fixture .pads-runtime/p2h/fixture
```

See [P2 eighth acceptance](docs/p2h-acceptance.md). Stabilizing a bounded candidate and
validating the missing restoration preimages must precede any nonempty execution.
