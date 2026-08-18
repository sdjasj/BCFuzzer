#!/bin/bash
# run_campaign.sh — drive a BCFuzzer fuzz campaign across the four targets.
#
# Each leg builds a 13-node network, assigns 4 controlled nodes, runs the
# two-level scheduler with T/M corpora and the BCB Oracle for the budget,
# then tears down before the next leg.  Legs run serially because the
# network factories cannot boot reliably under concurrent 13-node loads.
set -u
cd "$(dirname "$0")" || exit 1

OUT="${BCFZ_OUT:-/tmp/bcfz-campaign}"
SEED="${BCFZ_SEED:-42}"
MINUTES="${BCFZ_MINUTES:-360}"   # 6h per leg
mkdir -p "$OUT"

for target in geth fisco aptos chainmaker; do
  echo "=== $(date '+%F %H:%M:%S') campaign $target starting ==="
  rm -rf "$OUT/$target" "$OUT/$target.log"
  python3 -u bcfuzzer_campaign.py --target "$target" --output "$OUT/$target" \
      --nodes 13 --controlled 4 --budget-minutes "$MINUTES" --seed "$SEED" \
      > "$OUT/$target.log" 2>&1
  echo "$target exit=$? $(date '+%F %H:%M:%S')" >> "$OUT/$target.log"
done

echo "=== $(date '+%F %H:%M:%S') ALL DONE ==="
