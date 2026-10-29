#!/usr/bin/env bash
# bootstrap.sh - a Sage agent from a bare macOS / Linux machine (sage-setup skill). Idempotent.
#   bash bootstrap.sh --org "Acme Bakery" --agent Pip --owner "Ana Ruiz:ana@acme.test" --engine <folder|git url> --out "$HOME/Documents/Acme OS"
# Windows is the engine's first platform: the operator and dreaming wrappers are PowerShell and run only there
# (serve.json marks them windows_only). On macOS/Linux the spine, probe, deadman, Mission Control, Chat bridge and
# the doer run; the operator wrapper is the known gap `sage doctor` reports.
set -euo pipefail
ORG=""; AGENT=""; OWNERS=(); ENGINE="${SAGE_ENGINE:-}"; OUT=""; WHAT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --org) ORG="$2"; shift 2;; --agent) AGENT="$2"; shift 2;; --owner) OWNERS+=("$2"); shift 2;;
    --engine) ENGINE="$2"; shift 2;; --out) OUT="$2"; shift 2;; --what) WHAT="$2"; shift 2;;
    *) echo "unknown arg $1"; exit 2;;
  esac
done
[[ -n "$ORG" && -n "$AGENT" && ${#OWNERS[@]} -gt 0 && -n "$ENGINE" ]] || { echo "need --org, --agent (the owner names their agent), --owner, --engine"; exit 2; }
say() { echo "[sage-setup] $*"; }
have() { command -v "$1" >/dev/null 2>&1; }
SLUG=$(echo "$ORG" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-|-$//g')
[[ -n "$OUT" ]] || OUT="$HOME/Documents/$ORG OS"

if [[ "$(uname)" == "Darwin" ]]; then
  have brew || say "Homebrew missing (brew.sh); installing the rest needs it"
  for pkg in git python@3.11 node; do have "${pkg%%@*}" || { say "brew install $pkg"; brew install "$pkg"; }; done
  have gcloud || { say "brew install google-cloud-sdk"; brew install --cask google-cloud-sdk || true; }
else
  have git && have python3 && have node || { say "apt install git python3 python3-venv nodejs npm"; sudo apt-get update -qq && sudo apt-get install -y -qq git python3 python3-venv nodejs npm; }
  have gcloud || say "gcloud: see cloud.google.com/sdk/docs/install (optional until the Google step)"
fi
have claude || { say "npm install -g @anthropic-ai/claude-code"; npm install -g @anthropic-ai/claude-code; }

ENGINE_DIR="$ENGINE"
if [[ "$ENGINE" =~ ^(https?://|git@) ]]; then
  ENGINE_DIR="$HOME/sage-core"
  if [[ -f "$ENGINE_DIR/engine_manifest.json" ]]; then git -C "$ENGINE_DIR" pull --ff-only; else git clone --depth 1 "$ENGINE" "$ENGINE_DIR"; fi
fi
[[ -f "$ENGINE_DIR/assistant/new_org.py" ]] || { echo "no engine at $ENGINE_DIR"; exit 1; }

VENV="$HOME/.venvs/$SLUG-sage"; PY="$VENV/bin/python"
[[ -x "$PY" ]] || python3 -m venv "$VENV"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$ENGINE_DIR/assistant/requirements.txt"
"$PY" -m playwright install chromium >/dev/null

if [[ ! -f "$OUT/assistant/org.json" ]]; then
  ARGS=("$ENGINE_DIR/assistant/new_org.py" "$ORG" --agent "$AGENT" --out "$OUT")
  for o in "${OWNERS[@]}"; do ARGS+=(--owner "$o"); done
  [[ -n "$WHAT" ]] && ARGS+=(--what "$WHAT")
  say "creating the pack at $OUT"; "$PY" "${ARGS[@]}"
fi
"$PY" - "$OUT/assistant/org.json" "$PY" <<'PYEOF'
import json, sys
p, py = sys.argv[1], sys.argv[2]
d = json.load(open(p, encoding="utf-8-sig")); d["python"] = py
open(p, "w", encoding="utf-8").write(json.dumps(d, indent=1, ensure_ascii=False) + "\n")
PYEOF
say "doctor:"; "$PY" "$OUT/assistant/sage.py" doctor --no-probe --fix || true
say "next: SKILL.md step 2 (the owner's Claude token, in a fresh terminal), then \"$PY\" \"$OUT/assistant/sage.py\" serve"
