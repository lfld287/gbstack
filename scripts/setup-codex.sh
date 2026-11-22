#!/usr/bin/env bash
set -eu

usage() {
  printf '%s\n' \
    'Usage: setup-codex.sh [--codex-home DIR] [--dry-run] [--force]' \
    'Install the three bundled GBStack Codex profiles.' \
    'The default directory is ${CODEX_HOME:-$HOME/.codex}.' \
    'Use --dry-run to preview actions without writing.' \
    'Use --force to replace existing profiles, which are skipped by default.' \
    'Use -h or --help to show this message.'
}

CODEX_HOME_DIR=${CODEX_HOME:-$HOME/.codex}
dry_run=false
force=false
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
    --dry-run)
      dry_run=true
      shift
      ;;
    --force)
      force=true
      shift
      ;;
    *)
      printf 'Error: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TEMPLATE_DIR="$SCRIPT_DIR/../templates/codex"
case $CODEX_HOME_DIR in
  /*) ;;
  *) CODEX_HOME_DIR="$PWD/$CODEX_HOME_DIR" ;;
esac
while [[ $CODEX_HOME_DIR == /*/ ]]; do
  CODEX_HOME_DIR=${CODEX_HOME_DIR%/}
done

probe=$CODEX_HOME_DIR
while [[ ! -e $probe && ! -L $probe ]]; do
  probe=$(dirname -- "$probe")
done
if [[ ! -d $probe ]]; then
  printf 'Error: Codex home is not a directory: %s\n' "$probe" >&2
  exit 1
fi

tmp_file=
cleanup_temp() {
  if [[ -n $tmp_file ]]; then
    rm -f -- "$tmp_file"
    tmp_file=
  fi
}

if "$dry_run"; then
  if [[ ! -d $CODEX_HOME_DIR ]]; then
    printf 'Would create directory: %s\n' "$CODEX_HOME_DIR"
  fi
else
  if "$force" && ! command -v perl >/dev/null; then
    printf 'Error: --force requires Perl to replace profiles atomically\n' >&2
    exit 1
  fi
  mkdir -p "$CODEX_HOME_DIR"
  trap cleanup_temp EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
fi

for profile in worker-codex worker-codex-xhigh reviewer-codex; do
  filename="$profile.config.toml"
  target="$CODEX_HOME_DIR/$filename"
  action=install

  if [[ -e $target || -L $target ]]; then
    if ! "$force"; then
      printf 'Skipping existing profile: %s\n' "$target"
      continue
    fi
    if [[ -d $target ]]; then
      printf 'Error: profile path is a directory: %s\n' "$target" >&2
      exit 1
    fi
    action=replace
  fi

  if "$dry_run"; then
    printf 'Would %s profile: %s\n' "$action" "$target"
    continue
  fi

  if ! tmp_file=$(mktemp "$CODEX_HOME_DIR/.${filename}.XXXXXX"); then
    printf 'Error: could not create temporary file for profile: %s\n' "$target" >&2
    exit 1
  fi
  if ! cat -- "$TEMPLATE_DIR/$filename" > "$tmp_file" || ! chmod 0644 "$tmp_file"; then
    cleanup_temp
    printf 'Error: could not prepare profile: %s\n' "$target" >&2
    exit 1
  fi

  if "$force"; then
    if ! perl -e 'rename($ARGV[0], $ARGV[1]) or die "$!\n"' -- "$tmp_file" "$target"; then
      cleanup_temp
      printf 'Error: could not replace profile: %s\n' "$target" >&2
      exit 1
    fi
    tmp_file=
  # Use link(2) with the exact target path, including if it becomes a directory.
  elif ! link "$tmp_file" "$target" 2>/dev/null; then
    cleanup_temp
    if [[ -e $target || -L $target ]]; then
      printf 'Skipping existing profile: %s\n' "$target"
      continue
    fi
    printf 'Error: could not create profile: %s\n' "$target" >&2
    exit 1
  fi
  cleanup_temp
  printf 'Installed profile: %s\n' "$target"
done
