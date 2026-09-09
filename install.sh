#!/bin/sh
# traj-capture one-line installer (optional; the README's native commands do the same thing).
#
#   curl -fsSL https://raw.githubusercontent.com/micro1-partners/traj-capture/main/install.sh | sh -s -- <ENROLLMENT-CODE>
#
# Registers the micro1-traj marketplace and installs the plugin in every supported tool
# found on this machine (Claude Code, Codex CLI, Codex Desktop), then leaves the
# enrollment code in ~/.traj-capture/enroll-code. The plugin enrolls itself on the next
# session and deletes the file. Idempotent. Nothing here needs sudo.
#
#   --dry-run   print what would happen, change nothing
set -eu

REPO="micro1-partners/traj-capture"
MARKET="micro1-traj"
PLUGIN="traj-capture"
CODE=""
DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) CODE="$a" ;;
  esac
done

say() { printf 'traj-capture: %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then printf '  would run: %s\n' "$*"; else "$@"; fi; }
did=0

# Claude Code
if command -v claude >/dev/null 2>&1; then
  say "Claude Code found"
  if [ "$DRY" = 0 ] && claude plugin marketplace list 2>/dev/null | grep -q "$MARKET"; then
    say "  marketplace $MARKET already registered"
  else
    run claude plugin marketplace add "$REPO"
  fi
  run claude plugin install "$PLUGIN@$MARKET"
  did=1
fi

# Codex CLI
if command -v codex >/dev/null 2>&1; then
  say "Codex CLI found"
  run codex plugin marketplace add "$REPO"
  did=1
fi

# Codex Desktop (no CLI): register the marketplace in config.toml
CODEX_CFG="${CODEX_HOME:-$HOME/.codex}/config.toml"
if [ -f "$CODEX_CFG" ] && ! command -v codex >/dev/null 2>&1; then
  say "Codex Desktop config found at $CODEX_CFG"
  if grep -q "^\[marketplaces.$MARKET\]" "$CODEX_CFG"; then
    say "  marketplace $MARKET already in config.toml"
  else
    if [ "$DRY" = 1 ]; then
      printf '  would append [marketplaces.%s] + [plugins."%s@%s"] to %s\n' "$MARKET" "$PLUGIN" "$MARKET" "$CODEX_CFG"
    else
      cat >> "$CODEX_CFG" <<TOML

[marketplaces.$MARKET]
source_type = "git"
source = "https://github.com/$REPO.git"
ref = "main"

[plugins."$PLUGIN@$MARKET"]
enabled = true
TOML
      say "  added; restart Codex Desktop to load it"
    fi
  fi
  did=1
fi

if [ "$did" = 0 ]; then
  say "no supported tool found (claude, codex, or ~/.codex/config.toml). Install one and re-run."
  exit 1
fi

# Enrollment code → picked up by the plugin at the next session start
if [ -n "$CODE" ]; then
  if [ "$DRY" = 1 ]; then
    printf '  would write enrollment code to %s/.traj-capture/enroll-code\n' "$HOME"
  else
    mkdir -p "$HOME/.traj-capture"
    umask 077
    printf '%s\n' "$CODE" > "$HOME/.traj-capture/enroll-code"
    say "enrollment code saved; the plugin enrolls on your next session"
  fi
else
  say "no enrollment code given; run again with your code, or: echo CODE > ~/.traj-capture/enroll-code"
fi
say "done"
