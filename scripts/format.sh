#!/usr/bin/env bash
# House formatting chain (issue #8), run in this order:
#   autoflake (remove unused imports/variables) -> isort (sort imports)
#   -> autopep8 (format, aggressive).
#
# This is a DEVELOPER step, run by hand before committing. CI does not
# enforce formatting — the lint gate is for real defects, not style.
# Tool versions are pinned in requirements-dev.txt.
set -euo pipefail
cd "$(dirname "$0")/.."

mapfile -t files < <(git ls-files '*.py')

autoflake --in-place --remove-all-unused-imports --remove-unused-variables "${files[@]}"
isort "${files[@]}"
autopep8 --in-place --aggressive "${files[@]}"
