#!/bin/bash

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'resolve-roles.sh: python3 is required' >&2
    exit 127
fi

source_path=${BASH_SOURCE[0]}
case "$source_path" in
    /*) ;;
    *) source_path="$PWD/$source_path" ;;
esac
while [[ -L "$source_path" ]]; do
    link_target=$(readlink "$source_path") || exit 1
    case "$link_target" in
        /*) source_path=$link_target ;;
        *) source_path="${source_path%/*}/$link_target" ;;
    esac
done
SCRIPT_DIR=$(CDPATH= cd -P -- "${source_path%/*}" && pwd -P) || exit 1
exec python3 "$SCRIPT_DIR/lib/resolve_roles.py" "$@"
