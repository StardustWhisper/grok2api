#!/usr/bin/env bash
set -euo pipefail

# Minimal image generation self-test.
# Verifies that /v1/images/generations returns data[0].b64_json non-empty.

BASE_URL="${BASE_URL:-https://xai.lambda.xin}"
API_KEY="${API_KEY:-}"
MODEL="${MODEL:-grok-imagine-1.0}"
SIZE="${SIZE:-1024x1024}"
N="${N:-1}"
PROMPT="${PROMPT:-一只在雨夜霓虹街头的橘猫，电影感，广角}"

if [[ -z "$API_KEY" ]]; then
  echo "API_KEY is required" >&2
  exit 2
fi

echo "[selftest] BASE_URL=$BASE_URL MODEL=$MODEL SIZE=$SIZE N=$N" >&2

resp=$(curl -sS \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"$PROMPT\",\"size\":\"$SIZE\",\"n\":$N,\"response_format\":\"b64_json\"}" \
  "$BASE_URL/v1/images/generations")

python3 - <<PY
import json,sys
obj=json.loads('''$resp''')
if 'error' in obj:
    raise SystemExit(f"error: {obj['error'].get('code')} {obj['error'].get('message')}")
data=obj.get('data') or []
if not data:
    raise SystemExit('error: empty data[]')
b64=data[0].get('b64_json')
if not isinstance(b64,str) or len(b64)<1000:
    raise SystemExit(f"error: b64_json too short: {None if b64 is None else len(b64)}")
print('ok b64_len=', len(b64))
PY
