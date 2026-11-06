"""Bounded activation probe; no project/library is opened, saved or edited."""
from __future__ import annotations

import configparser
import csv
import json
import os
import subprocess
from pathlib import Path

from .diagnostics import PROGIDS, installation


def existing_editor(domain: str) -> list[int]:
    """Refuse application activation while a same-name editor is running.

    Some COM servers can reuse an existing application even with CreateObject.
    Refusing by name is conservative; never terminate processes by name.
    """
    name = {'designer': 'viewdraw.exe', 'layout': 'ExpeditionPE.exe',
            'cell_application': 'CellEditor.exe'}.get(domain)
    if name is None:
        return []
    result = subprocess.run([str(Path(os.environ['WINDIR']) / 'System32/tasklist.exe'), '/FO', 'CSV', '/NH'],
                            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    output = result.stdout.decode('mbcs', errors='replace')
    rows = list(csv.reader(output.splitlines()))
    if result.returncode != 0 or not any(len(row) >= 2 and row[1].isdigit() for row in rows):
        raise RuntimeError('cannot_verify_editor_process_baseline')
    return [int(row[1]) for row in rows if len(row) >= 2 and row[0].casefold() == name.casefold()]


def native_environment(sdd_home: Path, state_dir: Path) -> dict[str, str]:
    """Keep license settings in memory only; never persist the environment."""
    import winreg
    info = installation(sdd_home)
    env = dict(os.environ)
    for root, keypath in [(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, "Environment")]:
        try:
            with winreg.OpenKey(root, keypath) as key:
                for name in ("MGLS_LICENSE_FILE", "LM_LICENSE_FILE", "MGC_LICENSE_FILE"):
                    try:
                        value = winreg.QueryValueEx(key, name)[0]
                        if name not in env:
                            env[name] = os.path.expandvars(value)
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            pass
    ini = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "mgcSDD/win32" / info["product"] / "env.ini"
    config = configparser.ConfigParser(interpolation=None, strict=False)
    if ini.is_file():
        config.read(ini, encoding="utf-8-sig")
    common3d = config.get("CONFIGENV", "SDD_COMMON3D", fallback="common3D")
    mgc_home = (sdd_home / config.get("CONFIGENV", "SDD_MGC_HOME", fallback="../MGC_HOME.ixn")).resolve()
    env.update(SDD_HOME=str(sdd_home), SDD_PLATFORM="win32", SDD_VERSION=info["product"],
               COM_VERSION=info["com_version"], SDD_COMMON3D=common3d,
               MGC_HOME=str(mgc_home),
               SDD_LOCAL=str(state_dir), WDIR=str(state_dir) + ";" + str(sdd_home / "standard"))
    search = [sdd_home / p for p in ("common/win32/_bin", "common/win32/bin", "common/win32/lib",
                                   "wg/win32/bin", "wg/win32/lib", "wv/win32/bin", "lm/win32/bin")]
    search.append(sdd_home / common3d / "win32/lib")
    env["PATH"] = ";".join(map(str, search)) + ";" + env.get("PATH", "")
    return env


def activate(domain: str, sdd_home: Path, state_dir: Path, timeout: float = 25) -> dict:
    if domain not in PROGIDS:
        raise ValueError("unsupported activation domain")
    if not 1 <= timeout <= 60:
        raise ValueError("timeout must be between 1 and 60 seconds")
    if existing_editor(domain):
        raise RuntimeError('editor_already_running: close it before a diagnostic activation')
    info = installation(sdd_home)
    state_dir = state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    host = Path(os.environ["WINDIR"]) / "SysWOW64/cscript.exe"
    script = Path(__file__).with_name("activation.vbs")
    progid = PROGIDS[domain] + "." + info["com_version"]
    command = [str(host), "//nologo", str(script), progid,
               "application" if domain in ("designer", "layout", "cell_application") else "addin"]
    try:
        completed = subprocess.run(command, cwd=state_dir, env=native_environment(sdd_home, state_dir),
                                   capture_output=True, timeout=timeout,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        stdout = completed.stdout.decode("mbcs", errors="replace").strip()
        rows = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
        final = next((row for row in reversed(rows) if row.get("stage") == "result"), None)
        return {"domain": domain, "progid": progid, "host_bits": 32,
                "exit_code": completed.returncode, "events": rows,
                "status": final["status"] if final and completed.returncode == 0 else "probe_failed",
                "document_opened": False, "project_write_attempted": False}
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"").decode("mbcs", errors="replace")
        rows = []
        for line in partial.splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except ValueError:
                pass  # A timeout can interrupt the last JSON line.
        return {"domain": domain, "progid": progid, "host_bits": 32, "status": "timeout",
                "events": rows, "timeout_seconds": timeout,
                "activation_outcome": "uncertain", "cleanup": "host_stopped_server_state_unverified",
                "document_opened": False, "project_write_attempted": False,
                "recovery": "Inspect processes before another activation; do not terminate by process name."}
