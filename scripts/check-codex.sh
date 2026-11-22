#!/usr/bin/env bash
set -eu

usage() {
  printf '%s\n' \
    'Usage: check-codex.sh [--codex-home DIR] [PROFILE ...]' \
    'Check Codex login status and profile readability.' \
    'The default directory is ${CODEX_HOME:-$HOME/.codex}.' \
    'Default profiles: worker-codex worker-codex-xhigh reviewer-codex' \
    'Use -h or --help to show this message.'
}

CODEX_HOME_DIR=${CODEX_HOME:-$HOME/.codex}
profiles=()
while (( $# > 0 )); do
  case $1 in
    -h|--help)
      usage
      exit 0
      ;;
    --codex-home)
      if [[ $# -lt 2 || -z $2 || $2 == -* ]]; then
        printf 'Error: --codex-home requires a directory\n' >&2
        exit 2
      fi
      CODEX_HOME_DIR=$2
      shift 2
      ;;
    -*|''|*/*)
      printf 'Error: invalid argument: %s\n' "$1" >&2
      exit 2
      ;;
    *)
      profiles+=("$1")
      shift
      ;;
  esac
done

if [[ ${#profiles[@]} -eq 0 ]]; then
  profiles=(worker-codex worker-codex-xhigh reviewer-codex)
fi

CODEX_BIN=$(command -v codex || true)
if [[ ! -f $CODEX_BIN || ! -x $CODEX_BIN ]]; then
  CODEX_BIN="$HOME/.local/bin/codex"
fi
if [[ ! -f $CODEX_BIN || ! -x $CODEX_BIN ]]; then
  printf 'Error: no executable codex found on PATH or at %s\n' "$CODEX_BIN" >&2
  exit 127
fi

# Codex release builds skip creating PATH aliases when their home is under TMPDIR.
alias_guard_dir=$(CDPATH= cd -- "$CODEX_HOME_DIR" 2>/dev/null && pwd -P) || alias_guard_dir=$CODEX_HOME_DIR
if login_status=$(CODEX_HOME="$CODEX_HOME_DIR" TMPDIR="$alias_guard_dir" "$CODEX_BIN" login status 2>&1); then
  if [[ $login_status != *'Logged in'* ]]; then
    printf 'Error: codex login status did not report "Logged in": %s\n' "$login_status" >&2
    exit 1
  fi
else
  printf 'Error: codex login status exited %s: %s\n' "$?" "$login_status" >&2
  exit 1
fi
printf '%s\n' "$login_status"

status=0
for profile in "${profiles[@]}"; do
  profile_path="$CODEX_HOME_DIR/$profile.config.toml"
  if test -f "$profile_path" && test -r "$profile_path"; then
    printf 'Readable profile: %s\n' "$profile_path"
  else
    printf 'Error: profile is not a readable regular file: %s\n' "$profile_path" >&2
    status=1
  fi
done
exit "$status"
