"""Read-only installation/typelib evidence. Registry presence is not capability."""
from __future__ import annotations

import hashlib
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

TYPELIBS = {
    "designer": "wv/win32/bin/viewdraw.tlb",
    "layout": "common/win32/lib/MGCPCB.tlb",
    "cell": "wg/win32/lib/CellEditorAddin.tlb",
    "parts": "common/win32/lib/PDBEditor.tlb",
    "padstack": "common/win32/lib/PadstackEditor.tlb",
    "library": "lm/win32/bin/LibraryManager.tlb",
}
PROGIDS = {
    "designer": "ViewDraw.Application",
    "layout": "MGCPCB.ExpeditionPEApplication",
    "cell_application": "MGCPCB.CellEditorApplication",
    "cell": "CellEditorAddin.CellEditorDlg",
    "parts": "MGCPCBLibraries.PartsEditorDlg",
    "padstack": "MGCPCBLibraries.PadstackEditorDlg",
}


def fingerprint(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pe_architecture(path: Path) -> str:
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("not a PE executable")
        stream.seek(0x3C)
        offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(offset)
        if stream.read(4) != b"PE\0\0":
            raise ValueError("invalid PE signature")
        machine = struct.unpack("<H", stream.read(2))[0]
    return {0x14C: "x86", 0x8664: "x64", 0xAA64: "arm64"}.get(machine, hex(machine))


def installation(sdd_home: Path) -> dict:
    sdd_home = sdd_home.resolve(strict=True)
    source = sdd_home / "standard/COMVersion.xml"
    values = {v.findtext("name"): v.findtext("data") for v in ET.parse(source).getroot().findall("var")}
    version = values.get("PROG_ID_VER", "")
    if not version.isdigit():
        raise ValueError("COMVersion.xml has no numeric PROG_ID_VER")
    files = {}
    for name, relative in TYPELIBS.items():
        path = sdd_home / relative
        files[name] = {"path": str(path), "exists": path.is_file()}
        if path.is_file():
            files[name]["sha256"] = fingerprint(path)
    return {"sdd_home": str(sdd_home), "product": values.get("PCB_VER_STR", "").strip('"'),
            "com_version": version, "typelib_version": values.get("TLB_VER"),
            "source": str(source), "source_sha256": fingerprint(source), "typelibs": files}


def registered_servers(version: str) -> list[dict]:
    import winreg
    result = []
    for domain, base in PROGIDS.items():
        progid = f"{base}.{version}"
        for bits, flag in [(32, winreg.KEY_WOW64_32KEY), (64, winreg.KEY_WOW64_64KEY)]:
            row = {"domain": domain, "progid": progid, "registry_view": bits, "registered": False}
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + "\\CLSID", 0, winreg.KEY_READ | flag) as key:
                    clsid = winreg.QueryValueEx(key, "")[0]
                row.update(registered=True, clsid=clsid, servers={})
                for kind in ("InprocServer32", "LocalServer32"):
                    try:
                        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"CLSID\\{clsid}\\{kind}", 0, winreg.KEY_READ | flag) as key:
                            server = winreg.QueryValueEx(key, "")[0]
                        item = {"command": server}
                        # Record a binary only if the registry contains a single file path.
                        path = Path(server.strip('"'))
                        if path.is_file():
                            item.update(architecture=pe_architecture(path), sha256=fingerprint(path))
                        row["servers"][kind] = item
                    except FileNotFoundError:
                        pass
            except FileNotFoundError:
                pass
            result.append(row)
    return result


def typelib_inventory(sdd_home: Path) -> dict:
    import pythoncom
    inventory = {}
    for domain, relative in TYPELIBS.items():
        path = (sdd_home / relative).resolve(strict=True)
        # A fully qualified path prevents LoadTypeLib's legacy registration behavior.
        lib = pythoncom.LoadTypeLib(str(path))
        attr = lib.GetLibAttr()
        interfaces = []
        for i in range(lib.GetTypeInfoCount()):
            info = lib.GetTypeInfo(i)
            type_attr = info.GetTypeAttr()
            members = []
            for j in range(type_attr.cFuncs):
                function = info.GetFuncDesc(j)
                names = info.GetNames(function.memid)
                members.append({"name": names[0], "parameter_names": list(names[1:]),
                                "dispid": function.memid, "invoke_kind": function.invkind,
                                "optional_parameter_count": function.cParamsOpt})
            if members:
                interfaces.append({"name": lib.GetDocumentation(i)[0], "members": members})
        inventory[domain] = {"path": str(path), "sha256": fingerprint(path),
                             "name": lib.GetDocumentation(-1)[0], "version": f"{attr[3]}.{attr[4]}",
                             "status": "type_information_only", "interfaces": interfaces}
    return inventory
