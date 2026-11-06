"""Developer entry point. Evidence stays outside tracked source by default."""
from __future__ import annotations

import argparse
import json
import site
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
site.addsitedir(str(ROOT / ".pads-runtime/python"))

from runtime.diagnostics import installation, registered_servers, typelib_inventory
from runtime.probe import activate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["inspect", "typelibs", "activate"])
    parser.add_argument("--sdd-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--domain")
    parser.add_argument("--timeout", type=float, default=25)
    args = parser.parse_args()
    if args.command == "inspect":
        result = installation(args.sdd_home)
        result["registrations"] = registered_servers(result["com_version"])
        result["status"] = "static_evidence_only"
    elif args.command == "typelibs":
        result = typelib_inventory(args.sdd_home)
    else:
        result = activate(args.domain, args.sdd_home, ROOT / ".pads-runtime/native" / str(args.domain), args.timeout)
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": result.get("status", "type_information_only")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
