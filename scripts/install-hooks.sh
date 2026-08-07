#!/usr/bin/env bash
# Installs this repo's git hooks. Hooks are not cloned, so run once per checkout.

set -euo pipefail

root="$(git rev-parse --show-toplevel)"
install -m 0755 "$root/scripts/pre-push" "$(git rev-parse --git-path hooks)/pre-push"
chmod +x "$root/scripts/secrecy-check.sh"
echo "installed pre-push; verify with scripts/secrecy-check.sh"
