# Shared interpreter selection for the checkout and interactive installers.
# Sourced by Bash; no interpreter is executed while choosing the request.

claude_note_physical_directory() {
    local directory
    # The sentinel preserves trailing newlines until we can reject ambiguity.
    directory="$(cd -P "$1" 2>/dev/null && pwd && printf '.')" || return 1
    directory="${directory%.}"
    directory="${directory%$'\n'}"
    [[ "$directory" != *$'\n'* ]] || return 1
    printf '%s\n' "$directory"
}

claude_note_external_python() {
    local runtime="$1" tool_root="$2" directory target hops=0
    [[ "$tool_root" != / && "$runtime" != *$'\n'* ]] || return 1
    while :; do
        directory="$(claude_note_physical_directory "$(dirname "$runtime")")" || return 1
        runtime="$directory/$(basename "$runtime")"
        if [[ ! -L "$runtime" ]]; then break; fi
        ((hops += 1))
        [[ $hops -le 40 ]] || return 1
        target="$(readlink -n "$runtime" && printf '.')" || return 1
        target="${target%.}"
        [[ "$target" != *$'\n'* ]] || return 1
        case "$target" in
            /*) runtime="$target" ;;
            *) runtime="$directory/$target" ;;
        esac
    done
    # uv may replace the tool environment before creating its replacement.
    # Never give it an interpreter it can remove, including another tool's.
    case "$runtime" in "$tool_root"|"$tool_root"/*) return 1 ;; esac
    [[ -f "$runtime" && -x "$runtime" ]] || return 1
    printf '%s\n' "$runtime"
}

claude_note_select_install_python() {
    local request="${1:-}" tools runtime
    CLAUDE_NOTE_INSTALL_PYTHON=3.11
    if [[ -n "$request" ]]; then
        CLAUDE_NOTE_INSTALL_PYTHON="$request"
    elif [[ -n "${UV_PYTHON:-}" ]]; then
        CLAUDE_NOTE_INSTALL_PYTHON="$UV_PYTHON"
    elif [[ ${UV_MANAGED_PYTHON+x} || ${UV_NO_MANAGED_PYTHON+x} || ${UV_PYTHON_PREFERENCE+x} ]]; then
        # Let uv resolve the owner's explicit selector; add no competing family.
        return 0
    else
        tools="$(uv tool dir && printf '.')" || { echo "could not locate uv's tool directory" >&2; return 1; }
        tools="${tools%.}"
        tools="${tools%$'\n'}"
        [[ "$tools" != *$'\n'* ]] || { echo "uv returned an ambiguous tool directory" >&2; return 1; }
        case "$tools" in /*) ;; *) echo "uv returned a non-absolute tool directory" >&2; return 1 ;; esac
        runtime="$tools/claude-note/bin/python"
        if [[ ! -e "$runtime" && ! -L "$runtime" ]]; then
            if [[ -e "$tools/claude-note" || -L "$tools/claude-note" ]]; then
                echo "claude-note's existing tool has no Python; pass --python 3.11 (or set UV_PYTHON) to select a replacement" >&2
                return 1
            fi
            return 0
        fi
        tools="$(claude_note_physical_directory "$tools")" || return 1
        if ! CLAUDE_NOTE_INSTALL_PYTHON="$(claude_note_external_python "$runtime" "$tools")"; then
            echo "could not reuse claude-note's external Python; pass --python 3.11 (or set UV_PYTHON) to select a replacement" >&2
            return 1
        fi
        echo "Reusing claude-note's existing Python: $CLAUDE_NOTE_INSTALL_PYTHON"
    fi
}
