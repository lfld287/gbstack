#!/usr/bin/env bash
set -eu

usage() {
  printf '%s\n' \
    'Usage: setup-codex.sh [OPTIONS] [PROFILE ...]' \
    'Install Codex profile templates. Without explicit profiles, use active roles.' \
    'Use --codex-home DIR to override ${CODEX_HOME:-$HOME/.codex}.' \
    'Role options: --cwd DIR, --user-config-dir DIR, --project-config-dir DIR,' \
    '              --no-user-config, --no-project-config.' \
    'Use --dry-run to preview actions without writing.' \
    'Use --force to replace existing profiles, which are skipped by default.' \
    'Use -h or --help to show this message.'
}

CODEX_HOME_DIR=${CODEX_HOME:-$HOME/.codex}
dry_run=false
force=false
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
    --dry-run)
      dry_run=true
      shift
      ;;
    --force)
      force=true
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

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
resolver_args+=(codex-profiles --format tsv)
explicit_profiles=false
if (( ${#profiles[@]} > 0 )); then
  explicit_profiles=true
  resolver_args+=(-- "${profiles[@]}")
fi
profile_rows=$("$SCRIPT_DIR/resolve-roles.sh" "${resolver_args[@]}") || exit "$?"
if [[ -z $profile_rows ]] && ! "$explicit_profiles"; then
  printf '%s\n' 'No Codex profiles configured in active roles; nothing to install.'
  exit 0
fi

case $CODEX_HOME_DIR in
  /*) ;;
  *) CODEX_HOME_DIR="$PWD/$CODEX_HOME_DIR" ;;
esac
while [[ $CODEX_HOME_DIR == /*/ ]]; do
  CODEX_HOME_DIR=${CODEX_HOME_DIR%/}
done

profiles=()
template_paths=()
rest=$profile_rows
while [[ -n $rest ]]; do
  row=${rest%%$'\n'*}
  # Split at each tab to preserve empty fields, ignoring the template source.
  profiles+=("${row%%$'\t'*}")
  template_path=${row#*$'\t'}
  template_paths+=("${template_path%%$'\t'*}")
  if [[ $rest == *$'\n'* ]]; then
    rest=${rest#*$'\n'}
  else
    rest=
  fi
done

if "$explicit_profiles"; then
  for (( i = 0; i < ${#profiles[@]}; i++ )); do
    if [[ -z ${template_paths[i]} ]]; then
      profile=${profiles[i]}
      printf 'Error: no template for profile: %s (expected at %s)\n' "$profile" "$CODEX_HOME_DIR/$profile.config.toml" >&2
      exit 1
    fi
  done
fi

tmp_file=
cleanup_temp() {
  if [[ -n $tmp_file ]]; then
    rm -f -- "$tmp_file"
    tmp_file=
  fi
}

prepare_codex_home() {
  local probe=$CODEX_HOME_DIR
  while [[ ! -e $probe && ! -L $probe ]]; do
    probe=$(dirname -- "$probe")
  done
  if [[ ! -d $probe ]]; then
    printf 'Error: Codex home is not a directory: %s\n' "$probe" >&2
    exit 1
  fi

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
}

home_prepared=false
for (( i = 0; i < ${#profiles[@]}; i++ )); do
  profile=${profiles[i]}
  template_path=${template_paths[i]}
  filename="$profile.config.toml"
  target="$CODEX_HOME_DIR/$filename"
  if [[ -z $template_path ]]; then
    printf 'Skipping custom profile without template: %s (expected at %s)\n' "$profile" "$target"
    continue
  fi
  if ! "$home_prepared"; then
    prepare_codex_home
    home_prepared=true
  fi
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
  if ! cat -- "$template_path" > "$tmp_file" || ! chmod 0644 "$tmp_file"; then
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
