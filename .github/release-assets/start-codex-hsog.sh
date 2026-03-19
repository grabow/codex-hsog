#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CODEX_BIN="$SCRIPT_DIR/codex-hsog"

if [ ! -x "$CODEX_BIN" ]; then
  echo "codex-hsog not found next to launcher: $CODEX_BIN" >&2
  exit 1
fi

if [ -z "${HSOG_API_KEY:-}" ]; then
  if [ -t 0 ]; then
    printf "HSOG API key: " >&2
    stty -echo
    trap 'stty echo' EXIT INT TERM
    IFS= read -r HSOG_API_KEY
    stty echo
    trap - EXIT INT TERM
    printf "\n" >&2
  else
    IFS= read -r HSOG_API_KEY
  fi
  export HSOG_API_KEY
fi

if [ -z "${HSOG_API_KEY}" ]; then
  echo "HSOG_API_KEY is required." >&2
  exit 1
fi

exec "$CODEX_BIN" \
  -c 'model_provider="hs_og"' \
  -c 'model="gpt-5.2"' \
  -c 'model_providers.hs_og={name="HS_OG",base_url="https://llm-proxy.imla.hs-offenburg.de/v1",env_key="HSOG_API_KEY",wire_api="responses",fallback_chat=true,fallback_chat_path="/chat/completions"}' \
  "$@"
