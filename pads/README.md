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

## Read-only MCP (P1 first increment)

The standalone Rust server reuses Konnect's JSON-RPC and MCP result types.
Its group loading follows Konnect's progressive-discovery pattern; PADS schemas,
dispatch and worker isolation are implemented here. It does not link KiCad IPC,
run arbitrary scripts supplied by an agent, or require KiCad to be installed.

Five PADS tools are available:

| Tool | Purpose |
| --- | --- |
| `get_pads_environment` | Installed product, COM registration and binary architecture |
| `get_pads_capabilities` | Historical activation evidence; document read/write flags remain false |
| `list_pads_interface_members` | Member names and parameter names from the installed type library |
| `search_pads_reference` | Search all three local PDFs with page and source fingerprint |
| `get_pads_reference_page` | Retrieve a complete PDF page |

`environment` is loaded initially. Use `list_toolboxes` and
`load_toolset {"name":"reference"}` to discover the reference tools.
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
read-only Python worker with a 30-second deadline; a failure is returned as an
MCP tool error, never as successful empty data. Startup arguments are trusted
local configuration; callers cannot replace the worker or installation path.
The smoke test is specifically for the inspected VX.2.4/COM 66 installation and
the supplied 776-page Designer manual. No MCP client configuration is changed
automatically by these scripts.

## Delivery gates

1. P0: freeze source/license baseline, establish environment and interface paths.
2. P1: isolated MCP transport, read-only tools, target/state contracts, reference index.
3. P2: schematic edits on a disposable test project, save/reopen/netlist checks.
4. P3: schematic/PCB identity and native sync, placement and specified-path routing.
5. P4: selected existing objects in a test library, dependency and readback checks.
6. P5: packaged skills and three repeatable end-to-end acceptance scenarios.

Do not enable unverified writes in the public MCP catalogue. Library creation or
derivation requires a concrete candidate selection under the user's existing
reuse policy. Production GLK writes are outside test execution.
