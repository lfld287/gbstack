#!/usr/bin/env bash
set -eu

usage() {
  printf '%s\n' \
    'Usage: check-codex.sh [OPTIONS] [PROFILE ...]' \
    'Check Codex login status and profile readability.' \
    'Without explicit profiles, use the profiles configured in active roles.' \
    'Use --codex-home DIR to override ${CODEX_HOME:-$HOME/.codex}.' \
    'Role options: --cwd DIR, --user-config-dir DIR, --project-config-dir DIR,' \
    '              --no-user-config, --no-project-config.' \
    'Use -h or --help to show this message.'
}

CODEX_HOME_DIR=${CODEX_HOME:-$HOME/.codex}
profiles=()
resolver_args=()
while (( $# > 0 )); do
  case $1 in
    -h|--help)
      usage
      exit 0
      ;;
    --codex-home|--cwd|--user-config-dir|--project-config-dir)
      if [[ $# -lt 2 || -z $2 || $2 == -* ]]; then
        printf 'Error: %s requires a directory\n' "$1" >&2
        exit 2
      fi
      if [[ $1 == --codex-home ]]; then
        CODEX_HOME_DIR=$2
      else
        resolver_args+=("$1" "$2")
      fi
      shift 2
      ;;
    --no-user-config|--no-project-config)
      resolver_args+=("$1")
      shift
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

if (( ${#profiles[@]} == 0 )); then
  SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  resolver_args+=(codex-profiles --format lines)
  resolved_profiles=$("$SCRIPT_DIR/resolve-roles.sh" "${resolver_args[@]}") || exit "$?"
  if [[ -z $resolved_profiles ]]; then
    printf '%s\n' 'No Codex profiles configured in active roles; skipping Codex check.'
    exit 0
  fi
  rest=$resolved_profiles
  while [[ -n $rest ]]; do
    profiles+=("${rest%%$'\n'*}")
    if [[ $rest == *$'\n'* ]]; then
      rest=${rest#*$'\n'}
    else
      rest=
    fi
  done
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
