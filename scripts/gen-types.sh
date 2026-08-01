#!/usr/bin/env bash
# Regenerate the frontend's API types from the backend schemas.
#
#   ./scripts/gen-types.sh
#
# Run this after ANY change under backend/app/schemas/, then commit both
# openapi.json and frontend/src/lib/api/generated.ts. Committing the generated
# output is deliberate: it means the frontend always has current types even when
# the backend is not running, and a schema change shows up as a reviewable diff.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SCHEMA="openapi.json"
OUT="frontend/src/lib/api/generated.ts"

# Prefer the backend venv so this works without an activated shell.
if [ -x "backend/.venv/Scripts/python.exe" ]; then
  PY="backend/.venv/Scripts/python.exe"      # Windows
elif [ -x "backend/.venv/bin/python" ]; then
  PY="backend/.venv/bin/python"              # macOS / Linux
else
  PY="python"
fi

echo "==> exporting OpenAPI"
"$PY" scripts/export_openapi.py "$SCHEMA"

echo "==> generating TypeScript"
mkdir -p "$(dirname "$OUT")"
npx -y openapi-typescript@^7 "$SCHEMA" -o "$OUT"

# openapi-typescript emits the schema types but no banner; add one so nobody
# hand-edits the file and loses their work on the next run.
TMP="$(mktemp)"
cat > "$TMP" <<'BANNER'
/**
 * GENERATED FILE — DO NOT EDIT.
 *
 * Source of truth: backend/app/schemas/ (Pydantic) -> openapi.json -> this file.
 * Regenerate with: ./scripts/gen-types.sh
 *
 * Hand-edits are lost on the next run. If a type here is wrong, fix the Pydantic
 * model and regenerate.
 */
BANNER
cat "$OUT" >> "$TMP"
mv "$TMP" "$OUT"

echo "==> done"
echo "    $SCHEMA"
echo "    $OUT"
