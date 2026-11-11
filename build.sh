#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

docker build -t cybergym-harness-base:1 "${root}/base"

for dockerfile in "${root}"/harnesses/*/Dockerfile; do
  [[ -f "$dockerfile" ]] || continue
  dir="$(dirname "$dockerfile")"
  read -r image upstream < <(python3 - "${dir}/harness.toml" <<'PY'
import os
import sys
import tomllib

manifest = tomllib.load(open(sys.argv[1], "rb"))
upstream = (manifest.get("build") or {}).get("upstream", "")
print(manifest["image"], os.path.expanduser(upstream) if upstream else "-")
PY
)
  if [[ "$upstream" == "-" ]]; then
    docker build -t "$image" "$dir"
  else
    docker build --build-context "upstream=${upstream}" -t "$image" "$dir"
  fi
done
