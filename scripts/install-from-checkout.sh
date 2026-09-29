#!/usr/bin/env bash
#
# Install (or reinstall) claude-note from this git checkout, non-interactively.
#
#   scripts/install-from-checkout.sh [--push-agent] [--codex-hooks] [--allow-dirty] [--allow-temp]
#
# Unlike install.sh this asks nothing and never rewrites config.toml, the vault
# or the worker plist: it replaces the installed package with this checkout's
# code, restarts the worker if one is loaded, and records which commit is
# installed in ~/.local/share/claude-note/installed-from.json.
#
#   --push-agent   also (re)install the daily 03:00 `claude-note push` launchd job
#   --codex-hooks  also add `claude-note enqueue` to ~/.codex/hooks.json
#   --allow-dirty  install even with uncommitted changes in the checkout
#   --allow-temp   install even though the checkout is under a temp directory
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PUSH_AGENT=0 CODEX_HOOKS=0 ALLOW_DIRTY=0 ALLOW_TEMP=0
for arg in "$@"; do
    case "$arg" in
        --push-agent) PUSH_AGENT=1 ;;
        --codex-hooks) CODEX_HOOKS=1 ;;
        --allow-dirty) ALLOW_DIRTY=1 ;;
        --allow-temp) ALLOW_TEMP=1 ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

[[ -f "$ROOT/pyproject.toml" && -d "$ROOT/src/claude_note" ]] || { echo "not a claude-note checkout: $ROOT" >&2; exit 1; }
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

# A checkout under a temp dir disappears on reboot, and then nobody can tell
# which code the running jobs came from.
case "$ROOT" in
    /tmp/*|/private/tmp/*|/var/folders/*|/private/var/folders/*)
        if [[ $ALLOW_TEMP -eq 0 ]]; then
            echo "refusing to install from a temp directory ($ROOT); clone the repo somewhere permanent, or pass --allow-temp" >&2
            exit 1
        fi ;;
esac

COMMIT="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
BRANCH="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
DIRTY="$(git -C "$ROOT" status --porcelain --untracked-files=no -- src pyproject.toml 2>/dev/null || true)"
if [[ -n "$DIRTY" ]]; then
    echo "uncommitted changes in the package source:" >&2
    echo "$DIRTY" | sed 's/^/  /' >&2
    if [[ $ALLOW_DIRTY -eq 0 ]]; then
        echo "commit them, or pass --allow-dirty to install them anyway" >&2
        exit 1
    fi
fi

echo "Installing claude-note from $ROOT ($BRANCH @ ${COMMIT:0:12}${DIRTY:+, with uncommitted changes})"
uv tool install "$ROOT" --python 3.11 --force --reinstall --quiet

BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")/claude-note"
[[ -x "$BIN" ]] || BIN="$(command -v claude-note || true)"
[[ -n "$BIN" && -x "$BIN" ]] || { echo "install finished but claude-note is not on PATH" >&2; exit 1; }

STATE_DIR="$HOME/.local/share/claude-note"
mkdir -p "$STATE_DIR"
printf '{"path": "%s", "branch": "%s", "commit": "%s", "dirty": %s, "installed_at": "%s"}\n' \
    "$ROOT" "$BRANCH" "$COMMIT" "$([[ -n "$DIRTY" ]] && echo true || echo false)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    > "$STATE_DIR/installed-from.json"

if [[ "$(uname -s)" == "Darwin" ]]; then
    if launchctl print "gui/$(id -u)/com.claude-note.worker" >/dev/null 2>&1; then
        launchctl kickstart -k "gui/$(id -u)/com.claude-note.worker" && echo "Restarted the worker"
    fi
    if [[ $PUSH_AGENT -eq 1 ]]; then
        # Resolved through PATH by the push agent installer, so make sure it is this binary.
        PATH="$(dirname "$BIN"):$PATH" "$BIN" push --install-agent
    fi
elif command -v systemctl >/dev/null && systemctl --user is-active --quiet claude-note.service 2>/dev/null; then
    systemctl --user restart claude-note.service && echo "Restarted the worker"
fi
if [[ $PUSH_AGENT -eq 1 && "$(uname -s)" != "Darwin" ]]; then
    echo "--push-agent installs a launchd job and is macOS-only; skipped" >&2
fi

if [[ $CODEX_HOOKS -eq 1 ]]; then
    "$BIN" install-codex-hooks
fi

echo "Installed $(uv tool list 2>/dev/null | grep -m1 '^claude-note ' || echo claude-note) at $BIN from ${COMMIT:0:12}"
