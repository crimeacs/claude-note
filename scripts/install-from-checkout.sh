#!/usr/bin/env bash
#
# Install (or reinstall) claude-note from this source tree, without asking anything.
#
#   scripts/install-from-checkout.sh [--non-interactive] [--vault PATH] [--author EMAIL]
#                                    [--push-agent] [--claude-hooks] [--codex-hooks]
#                                    [--install-uv] [--source-ref REF]
#                                    [--allow-dirty] [--allow-temp]
#
# The source tree is a git checkout or a bundled copy (for example the one the
# Sweat app ships; a copy records its ref in a SOURCE_REF file). Without
# --non-interactive this updates an existing install only: it replaces the
# package, restarts the worker if one is loaded, and never touches config.toml,
# the vault or the worker plist. The installed ref is recorded in
# ~/.local/share/claude-note/installed-from.json.
#
#   --non-interactive  set up a new machine too: write config.toml if there is
#                      none, create the vault (from vault-template) and install
#                      and start the worker LaunchAgent if missing. Never prompts;
#                      stdin is closed. Existing files are left alone.
#   --vault PATH       vault for a new config.toml (default ~/Documents/claude-notes)
#   --author EMAIL     set `author` in config.toml (stamped on every note and push)
#   --push-agent       also (re)install the daily 03:00 `claude-note push` launchd job
#   --claude-hooks     also add `claude-note enqueue` to ~/.claude/settings.json
#   --codex-hooks      also add `claude-note enqueue` to ~/.codex/hooks.json
#   --install-uv       install uv (astral.sh standalone installer, ~/.local/bin,
#                      shell profiles untouched) when it is missing
#   --source-ref REF   the ref to record for a tree that is not a git checkout
#   --allow-dirty      install even with uncommitted changes in the checkout
#   --allow-temp       install even though the tree is under a temp directory
#
# Exit codes: 0 installed, 1 failed, 2 bad option, 3 a prerequisite is missing
# (uv, without --install-uv). `claude-note status --json` reports health after.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
NON_INTERACTIVE=0 PUSH_AGENT=0 CLAUDE_HOOKS=0 CODEX_HOOKS=0 INSTALL_UV=0 ALLOW_DIRTY=0 ALLOW_TEMP=0
VAULT="" AUTHOR="" SOURCE_REF="" WORKER_STARTED=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --non-interactive) NON_INTERACTIVE=1 ;;
        --vault) VAULT="${2:?--vault needs a path}"; shift ;;
        --author) AUTHOR="${2:?--author needs an email}"; shift ;;
        --source-ref) SOURCE_REF="${2:?--source-ref needs a ref}"; shift ;;
        --push-agent) PUSH_AGENT=1 ;;
        --claude-hooks) CLAUDE_HOOKS=1 ;;
        --codex-hooks) CODEX_HOOKS=1 ;;
        --install-uv) INSTALL_UV=1 ;;
        --allow-dirty) ALLOW_DIRTY=1 ;;
        --allow-temp) ALLOW_TEMP=1 ;;
        -h|--help) sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

[[ $NON_INTERACTIVE -eq 1 ]] && exec </dev/null

# A GUI app or launchd hands us a minimal PATH; add where uv, qmd and claude live.
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

[[ -f "$ROOT/pyproject.toml" && -d "$ROOT/src/claude_note" ]] || { echo "not a claude-note source tree: $ROOT" >&2; exit 1; }

if [[ -n "$AUTHOR" && ! "$AUTHOR" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]+$ ]]; then
    echo "--author is not an email address: $AUTHOR" >&2; exit 2
fi

if ! command -v uv >/dev/null; then
    if [[ $INSTALL_UV -eq 1 ]]; then
        echo "Installing uv into ~/.local/bin"
        curl -fLsS --max-time 120 https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null \
            || { echo "uv install failed" >&2; exit 1; }
        command -v uv >/dev/null || { echo "uv installed but not found in ~/.local/bin" >&2; exit 1; }
    else
        echo "uv is required: https://docs.astral.sh/uv/ (or pass --install-uv)" >&2
        exit 3
    fi
fi

# A tree under a temp dir disappears on reboot, and then nobody can tell
# which code the running jobs came from.
case "$ROOT" in
    /tmp/*|/private/tmp/*|/var/folders/*|/private/var/folders/*)
        if [[ $ALLOW_TEMP -eq 0 ]]; then
            echo "refusing to install from a temp directory ($ROOT); clone the repo somewhere permanent, or pass --allow-temp" >&2
            exit 1
        fi ;;
esac

# Only ask git about a real checkout of this repo: a bundled copy may sit inside
# some other repository, and on a Mac without developer tools /usr/bin/git
# opens an "install command line tools" dialog.
DIRTY=""
if [[ -e "$ROOT/.git" ]] && command -v git >/dev/null && [[ "$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null)" == "$ROOT" ]]; then
    SOURCE_KIND=git
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
else
    SOURCE_KIND=bundle
    BRANCH=bundle
    [[ -z "$SOURCE_REF" && -f "$ROOT/SOURCE_REF" ]] && SOURCE_REF="$(head -1 "$ROOT/SOURCE_REF" | tr -d '[:space:]')"
    COMMIT="${SOURCE_REF:-unknown}"
fi

echo "Installing claude-note from $ROOT ($SOURCE_KIND: $BRANCH @ ${COMMIT:0:12}${DIRTY:+, with uncommitted changes})"
uv tool install "$ROOT" --python 3.11 --force --reinstall --quiet

BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")/claude-note"
[[ -x "$BIN" ]] || BIN="$(command -v claude-note || true)"
[[ -n "$BIN" && -x "$BIN" ]] || { echo "install finished but claude-note is not on PATH" >&2; exit 1; }
# Resolved through PATH by the agent and hook installers, so make sure it is this binary.
export PATH="$(dirname "$BIN"):$PATH"

STATE_DIR="$HOME/.local/share/claude-note"
mkdir -p "$STATE_DIR"
printf '{"path": "%s", "source": "%s", "branch": "%s", "commit": "%s", "dirty": %s, "installed_at": "%s"}\n' \
    "$ROOT" "$SOURCE_KIND" "$BRANCH" "$COMMIT" "$([[ -n "$DIRTY" ]] && echo true || echo false)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    > "$STATE_DIR/installed-from.json"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/claude-note"
CONFIG="$CONFIG_DIR/config.toml"
if [[ $NON_INTERACTIVE -eq 1 && ! -f "$CONFIG" ]]; then
    VAULT="${VAULT:-$HOME/Documents/claude-notes}"
    VAULT="${VAULT/#\~/$HOME}"
    [[ "$VAULT" != *\"* ]] || { echo "--vault must not contain a double quote" >&2; exit 2; }
    command -v claude >/dev/null && MODE=route || MODE=log
    command -v qmd >/dev/null && QMD=true || QMD=false
    mkdir -p "$CONFIG_DIR"
    cat > "$CONFIG.tmp" <<EOF
# Claude Note configuration (written by install-from-checkout.sh --non-interactive)
# See docs/configuration.md for all options

vault_root = "$VAULT"

# Synthesis mode: log | inbox | route ("log" when the claude CLI was missing at install)
[synthesis]
mode = "$MODE"

[qmd]
enabled = $QMD
synth_max_notes = 5
EOF
    mv "$CONFIG.tmp" "$CONFIG"
    echo "Wrote $CONFIG (vault $VAULT, synthesis $MODE, qmd $QMD)"
fi

# A config this script wrote in "log" mode because the claude CLI was missing then
# is switched to "route" once claude is present, or sessions never become notes.
# A config a person wrote (no "written by" header) is left alone.
if [[ -f "$CONFIG" ]] && grep -q 'written by install-from-checkout.sh' "$CONFIG" \
    && grep -qE '^mode[[:space:]]*=[[:space:]]*"log"' "$CONFIG" && command -v claude >/dev/null; then
    sed -E 's|^mode[[:space:]]*=[[:space:]]*"log"|mode = "route"|' "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
    echo "Synthesis switched from log to route (claude CLI found)"
fi

if [[ -n "$AUTHOR" ]]; then
    [[ -f "$CONFIG" ]] || { echo "no $CONFIG to record the author in; run with --non-interactive for a new machine" >&2; exit 1; }
    if grep -qE '^author[[:space:]]*=' "$CONFIG"; then
        sed -E "s|^author[[:space:]]*=.*|author = \"$AUTHOR\"|" "$CONFIG" > "$CONFIG.tmp"
    else
        # A top-level key must come before the first [section]: put it first.
        { echo "author = \"$AUTHOR\""; cat "$CONFIG"; } > "$CONFIG.tmp"
    fi
    if cmp -s "$CONFIG" "$CONFIG.tmp"; then rm -f "$CONFIG.tmp"; else mv "$CONFIG.tmp" "$CONFIG"; echo "Author: $AUTHOR"; fi
fi

if [[ $NON_INTERACTIVE -eq 1 ]]; then
    VAULT_ROOT="$(sed -nE 's/^vault_root[[:space:]]*=[[:space:]]*"?([^"]*)"?.*/\1/p' "$CONFIG" | head -1)"
    VAULT_ROOT="${VAULT_ROOT/#\~/$HOME}"
    [[ -n "$VAULT_ROOT" ]] || { echo "$CONFIG has no vault_root" >&2; exit 1; }
    mkdir -p "$VAULT_ROOT/.claude-note/queue" "$VAULT_ROOT/.claude-note/state" "$VAULT_ROOT/.claude-note/logs"
    TEMPLATE="$ROOT/vault-template"
    if [[ -d "$TEMPLATE" ]]; then
        for f in CLAUDE.md claude-note-inbox.md open-questions.md; do
            [[ -e "$VAULT_ROOT/$f" || ! -f "$TEMPLATE/$f" ]] || cp "$TEMPLATE/$f" "$VAULT_ROOT/$f"
        done
        [[ -e "$VAULT_ROOT/templates" || ! -d "$TEMPLATE/templates" ]] || cp -R "$TEMPLATE/templates" "$VAULT_ROOT/templates"
    fi

    if [[ "$(uname -s)" == "Darwin" ]]; then
        PLIST="$HOME/Library/LaunchAgents/com.claude-note.worker.plist"
        if [[ ! -f "$PLIST" ]]; then
            mkdir -p "$HOME/Library/LaunchAgents"
            cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.claude-note.worker</string>
    <key>ProgramArguments</key>
    <array><string>$BIN</string><string>worker</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$VAULT_ROOT/.claude-note/logs/worker-stdout.log</string>
    <key>StandardErrorPath</key><string>$VAULT_ROOT/.claude-note/logs/worker-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>$(dirname "$BIN"):$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
EOF
            echo "Installed $PLIST"
        fi
        if ! launchctl print "gui/$(id -u)/com.claude-note.worker" >/dev/null 2>&1; then
            if launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
                echo "Started the worker"; WORKER_STARTED=1
            else
                echo "could not start the worker ($PLIST); claude-note status --json will show it" >&2
            fi
        fi
    fi
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
    if [[ $WORKER_STARTED -eq 0 ]] && launchctl print "gui/$(id -u)/com.claude-note.worker" >/dev/null 2>&1; then
        launchctl kickstart -k "gui/$(id -u)/com.claude-note.worker" && echo "Restarted the worker"
    fi
    if [[ $PUSH_AGENT -eq 1 ]]; then
        "$BIN" push --install-agent
    fi
elif command -v systemctl >/dev/null && systemctl --user is-active --quiet claude-note.service 2>/dev/null; then
    systemctl --user restart claude-note.service && echo "Restarted the worker"
fi
if [[ $PUSH_AGENT -eq 1 && "$(uname -s)" != "Darwin" ]]; then
    echo "--push-agent installs a launchd job and is macOS-only; skipped" >&2
fi

if [[ $CLAUDE_HOOKS -eq 1 ]]; then
    "$BIN" install-claude-hooks
fi
if [[ $CODEX_HOOKS -eq 1 ]]; then
    "$BIN" install-codex-hooks
fi

echo "Installed $(uv tool list 2>/dev/null | grep -m1 '^claude-note ' || echo claude-note) at $BIN from ${COMMIT:0:12}"
