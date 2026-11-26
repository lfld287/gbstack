#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"

check_node() {
  local bin="${1:-}"
  [ -n "$bin" ] && [ -x "$bin" ] && [ ! -d "$bin" ] || return 1
  "$bin" -e 'process.exit(parseInt(process.versions.node.split(".")[0], 10) >= 18 ? 0 : 1)' >/dev/null 2>&1
}

pick_latest_node() {
  local best=""
  local best_ver=""
  local candidate ver norm
  for candidate in "$@"; do
    [ -x "$candidate" ] && [ ! -d "$candidate" ] || continue
    ver="$("$candidate" -p 'process.versions.node' 2>/dev/null || true)"
    case "$ver" in
      [0-9]*.[0-9]*.[0-9]*)
        local major="${ver%%.*}"
        [ "$major" -ge 18 ] 2>/dev/null || continue
        local rest="${ver#*.}"
        local minor="${rest%%.*}"
        local patch="${rest#*.}"
        patch="${patch%%-*}"
        norm="$(printf '%05d.%05d.%05d' "$major" "$minor" "$patch" 2>/dev/null || true)"
        if [ -n "$norm" ] && { [ -z "$best_ver" ] || [ "$norm" \> "$best_ver" ]; }; then
          best_ver="$norm"
          best="$candidate"
        fi
        ;;
    esac
  done
  [ -n "$best" ] && printf '%s\n' "$best"
}

NODE_BIN=""

if [ -n "${GBSTACK_NODE_BIN:-}" ]; then
  if check_node "$GBSTACK_NODE_BIN"; then
    NODE_BIN="$GBSTACK_NODE_BIN"
  else
    printf 'Error: GBSTACK_NODE_BIN is not a valid Node.js >= 18 executable: %s\n' "$GBSTACK_NODE_BIN" >&2
    exit 127
  fi
fi

if [ -z "$NODE_BIN" ]; then
  PATH_NODE="$(command -v node 2>/dev/null || true)"
  if check_node "$PATH_NODE"; then
    NODE_BIN="$PATH_NODE"
  fi
fi

if [ -z "$NODE_BIN" ] && [ -n "${NVM_BIN:-}" ] && check_node "${NVM_BIN}/node"; then
  NODE_BIN="${NVM_BIN}/node"
fi

if [ -z "$NODE_BIN" ] && [ -n "${HOME:-}" ]; then
  shopt -s nullglob
  [ -n "$NODE_BIN" ] || NODE_BIN="$(pick_latest_node "${HOME}/.nvm/versions/node/"*/bin/node || true)"
  [ -n "$NODE_BIN" ] || NODE_BIN="$(pick_latest_node "${HOME}/.volta/bin/node" || true)"
  [ -n "$NODE_BIN" ] || NODE_BIN="$(pick_latest_node "${HOME}/.fnm/node-versions/"*/installation/bin/node || true)"
  [ -n "$NODE_BIN" ] || NODE_BIN="$(pick_latest_node "${HOME}/.local/share/fnm/node-versions/"*/installation/bin/node || true)"
  [ -n "$NODE_BIN" ] || NODE_BIN="$(pick_latest_node "${HOME}/.local/share/mise/installs/node/"*/bin/node || true)"
  [ -n "$NODE_BIN" ] || NODE_BIN="$(pick_latest_node "${HOME}/.asdf/installs/nodejs/"*/bin/node || true)"
  shopt -u nullglob
fi

if [ -z "$NODE_BIN" ]; then
  for fixed in /opt/homebrew/bin/node /usr/local/bin/node; do
    if check_node "$fixed"; then
      NODE_BIN="$fixed"
      break
    fi
  done
fi

if [ -z "$NODE_BIN" ]; then
  printf 'Error: Node.js >= 18 executable not found\n' >&2
  exit 127
fi

NODE_DIR="$(dirname -- "$NODE_BIN")"
export PATH="${NODE_DIR}${PATH:+:${PATH}}"

exec "$NODE_BIN" "$SCRIPT_DIR/rpc/pi-rpc-ctl.mjs" "$@"
