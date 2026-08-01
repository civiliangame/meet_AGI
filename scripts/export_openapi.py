"""Dump the OpenAPI document without starting a server.

    python scripts/export_openapi.py openapi.json

Importing the app is enough — FastAPI builds the schema on demand. Keeping this separate
from `gen-types.sh` means CI can diff the committed schema against a fresh one to catch
a contract change that forgot to regenerate the TypeScript.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.main import app  # noqa: E402


def main() -> int:
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "openapi.json"
    schema = app.openapi()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    paths = len(schema.get("paths", {}))
    models = len(schema.get("components", {}).get("schemas", {}))
    print(f"wrote {destination}  ({paths} paths, {models} schemas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
