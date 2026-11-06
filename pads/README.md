# PADS native integration (development)

This is a PADS-specific derivative of Konnect, based on upstream commit
`676efce43c9779f9f9e59a9181cb0746c6452650`. The upstream AGPL-3.0-only license
and copyright notices remain applicable. Local development began 2026-11-06.
The original KiCad executable and its installed configuration are not replaced.

阶段结论和后续验收条件见 [开发记录](docs/implementation-status.md)。

## Current scope

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
integers in 1/100 inch (0.254 mm), bounded to 0..10000; no automatic grid snapping,
sheet-boundary check or collision avoidance is promised.

Review the returned `before`/`after`, then pass its `preview_id` to execution. The
preview expires after ten minutes and can be executed once. Attributes, base and
instance values, coordinates, orientation, symbol name and component pin/net
identities must still match. Other sheet objects, wire geometry and ERC are outside
this comparison. Native editing is not an atomic transaction; avoid manual editing
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

It intentionally leaves the modified test copy for review. Results and recovery
limits are recorded in [P2 acceptance](docs/p2-acceptance.md). P2 placement and
pin-connection creation, P3 PCB edits and P4 library writes are still pending.
