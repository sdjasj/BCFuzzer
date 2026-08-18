"""Regression mode: re-run the minimized BCB PoC test cases.

Delegates to full_bcfuzzer's BUG_SPECS/run_bug (the exact PoC scripts the
test_cases/ directory ships) and writes regression/<bug>.json for each
selected bug.  Paper Table-1 bug id -> PoC bug id (canonical) mapping:

  ge-08=ge-10, ge-09=ge-13, fs-04=fs-01, fs-05=fs-02, fs-06=fs-03,
  fs-07=fs-04, cm-01=cm-09, cm-02=cm-11, cm-03=cm-12,
  ap-10=ap-18, ap-11=ap-19, ap-12=ap-20.

cm-02 (net.seeds peer-map race) and cm-03 (cert reconfig + logger race)
were added to the artifact as test_cases/chainmaker/11_* and 12_* with
BUG_SPECS ids cm-11 / cm-12 (the original corpus numbering).  BCB #2/#3
may not reproduce on ChainMaker v3.0.0 (RWMutex-hardened); the PoC scripts
print [POC VERSION-GUARDED] and point to the original issue evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from full_bcfuzzer import BUG_INDEX, selected_specs, run_bug  # noqa: E402

PAPER_TO_POC = {
    "ge-08": "ge-10", "ge-09": "ge-13",
    "fs-04": "fs-01", "fs-05": "fs-02", "fs-06": "fs-03", "fs-07": "fs-04",
    "cm-01": "cm-09", "cm-02": "cm-11", "cm-03": "cm-12",
    "ap-10": "ap-18", "ap-11": "ap-19", "ap-12": "ap-20",
}


def resolve_bugs(target: str, bugs: list[str] | None) -> list[str]:
    """Paper bug ids -> PoC bug ids for the target."""
    if bugs is None:
        return [poc for paper, poc in PAPER_TO_POC.items()
                if BUG_INDEX[poc].target == target]
    resolved = []
    for bug in bugs:
        poc = PAPER_TO_POC.get(bug, bug)  # allow PoC ids directly
        if poc not in BUG_INDEX:
            raise SystemExit(f"unknown bug id: {bug}")
        resolved.append(poc)
    return resolved


def run_regression(target: str, bugs: list[str] | None, out_dir: Path) -> list[dict]:
    poc_ids = resolve_bugs(target, bugs)
    specs = selected_specs({target}, set(poc_ids))
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for spec in specs:
        print(f"[regress] {spec.bug_id} ({spec.target}): {spec.description}",
              flush=True)
        record = run_bug(spec, out_dir)
        records.append(record)
        (out_dir / f"{spec.bug_id}.json").write_text(
            json.dumps(record, indent=2, default=str), encoding="utf-8")
        print(f"[regress] {spec.bug_id}: {record.get('status')}", flush=True)
    summary = {"total": len(records),
               "passed": sum(1 for r in records if r.get("status") == "pass"),
               "records": records}
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return records
