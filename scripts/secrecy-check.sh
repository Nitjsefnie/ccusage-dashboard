#!/usr/bin/env bash
# Fails if a forbidden literal appears in the tree or in committed history.
# Literals come from .secrecy-literals (gitignored, one per line) and from
# .env's DATABASE_URL_AUTH. Findings name commits and files, never the value.
#
#   scripts/secrecy-check.sh          tree + history
#   scripts/secrecy-check.sh --tree   tree only

set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1

MODE="${1:-full}"
FAIL=0
declare -a NEEDLES=()
declare -a LABELS=()

if [ -f .secrecy-literals ]; then
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    [ -n "$line" ] || continue
    NEEDLES+=("$line")
    LABELS+=("a listed literal (${#line} chars)")
  done < .secrecy-literals
fi

if [ -f .env ]; then
  AUTH_DB="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?DATABASE_URL_AUTH[[:space:]]*=[[:space:]]*//p' .env \
    | head -n1 | tr -d '"'"'"'' | sed 's/?.*$//' | sed 's#.*/##' | tr -d '[:space:]')"
  if [ -n "${AUTH_DB:-}" ] && [ "${#AUTH_DB}" -ge 4 ]; then
    NEEDLES+=("$AUTH_DB")
    LABELS+=("the auth-DB name")
  fi
fi

# Nothing to check must not look like nothing found.
if [ "${#NEEDLES[@]}" -eq 0 ]; then
  echo "secrecy-check: ERROR — no literals available; add .secrecy-literals or .env" >&2
  exit 1
fi

for i in "${!NEEDLES[@]}"; do
  hits="$(git grep -I -l -i -e "${NEEDLES[$i]}" -- . 2>/dev/null)"
  if [ -n "$hits" ]; then
    echo "secrecy-check: FAIL — ${LABELS[$i]} is in the working tree:" >&2
    echo "$hits" | sed 's/^/    /' >&2
    FAIL=1
  fi
done

if [ "$MODE" = "--tree" ]; then
  [ "$FAIL" -eq 0 ] && echo "secrecy-check: tree clean (${#NEEDLES[@]} literal(s))"
  exit "$FAIL"
fi

for i in "${!NEEDLES[@]}"; do
  # --regexp-ignore-case is load-bearing: without it this misses what the
  # -i tree check above catches, and reports clean.
  hits="$(git log --branches --tags --oneline --regexp-ignore-case \
            -S"${NEEDLES[$i]}" --format='%h' 2>/dev/null)"
  if [ -n "$hits" ]; then
    echo "secrecy-check: FAIL — ${LABELS[$i]} is in committed history:" >&2
    echo "$hits" | sed 's/^/    /' >&2
    echo "    Deleting it from the tree does not unpublish it: rewrite history," >&2
    echo "    and rotate the value if it was ever pushed." >&2
    FAIL=1
  fi
done

if [ "$FAIL" -eq 0 ]; then
  echo "secrecy-check: clean (tree + history, ${#NEEDLES[@]} literal(s))"
fi
exit "$FAIL"
