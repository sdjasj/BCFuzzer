#!/bin/bash
# setup.sh — wire the BCFuzzer workspace contract.
#
# The platform adapters (live_node_*.py) and PoC scripts assume the four
# blockchain source trees and the inter-node bug corpus live as siblings
# under a common workspace root.  By default that root is /home/geth/tse
# (matching the paper's evaluation environment).  This script verifies the
# expected layout and, where the trees exist elsewhere, creates symlinks so
# the immutable adapter paths resolve.
#
# Usage:
#   ./setup.sh                       # default workspace /home/geth/tse
#   ./setup.sh /path/to/workspace    # custom workspace root
set -u

WORKSPACE="${1:-/home/geth/tse}"
REPO="$(cd "$(dirname "$0")" && pwd)"

# The sibling trees the adapters and PoC scripts reference by absolute path.
declare -a TREES=(
  "go-ethereum"
  "chainmaker-go"
  "FISCO-BCOS"
  "aptos-core"
  "inter-node-bugs-final"
)

echo "[setup] workspace root: $WORKSPACE"
echo "[setup] artifact repo:  $REPO"

mkdir -p "$WORKSPACE"

ok=0
for tree in "${TREES[@]}"; do
  target="$WORKSPACE/$tree"
  if [ -e "$target" ]; then
    echo "[setup]   ok  $tree (present)"
  else
    echo "[setup]   --  $tree (MISSING — build/checkout this source tree, see docs/ENVIRONMENT.md)"
    ok=1
  fi
done

# The PoC corpus is shipped inside this artifact as test_cases/.  Bind it to
# the workspace path the scripts expect (inter-node-bugs-final) so the
# regression harness resolves without an external checkout.
ln -sfn "$REPO/test_cases" "$WORKSPACE/inter-node-bugs-final" 2>/dev/null && \
  echo "[setup] linked test_cases/ -> $WORKSPACE/inter-node-bugs-final"

if [ "$ok" -ne 0 ]; then
  echo
  echo "[setup] One or more platform trees are missing.  BCFuzzer can still run"
  echo "[setup] its unit tests and the MEI/scheduler/oracle smoke without them,"
  echo "[setup] but live fuzzing and PoC reproduction require the trees."
  echo "[setup] See docs/ENVIRONMENT.md for pinned commits and build steps."
fi

echo "[setup] done"
