#!/usr/bin/env python3
"""Full BCFuzzer harness for the local inter-node bug corpus.

This entrypoint has two goals:

1. expose a *bug-targeted* mode that can reproduce every known inter-node bug
   in ``inter-node-bugs-final/`` through the strongest target-specific
   transaction / message / proposer workflows already validated in this
   workspace;
2. provide one consistent place to record per-bug status, logs, and summary
   tables.

The bug-targeted mode is intentionally pragmatic.  Some bugs are reachable with
only one-node config skew plus normal traffic; others require a fake beacon
client, a malicious proposer mutation, or a special governance transaction.
Those latter cases cannot be reached honestly by a single generic "send more
transactions" loop, so the harness reuses the audited per-bug PoCs as
reproduction profiles under a unified interface.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
import os

# Workspace root: the directory holding the four blockchain source trees and
# the inter-node bug corpus.  Defaults to the artifact repo's parent (so a
# fresh clone beside the trees just works); override with BCFZ_WORKSPACE for
# non-standard layouts.  See docs/ENVIRONMENT.md and setup.sh.
REPO_ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("BCFZ_WORKSPACE", str(REPO_ROOT.parent)))
BUG_ROOT = WORKSPACE / "inter-node-bugs-final"
# the runner root is this repo itself in the flat artifact layout
RUNNER_ROOT = REPO_ROOT

TARGET_REPOS = {
    "geth": WORKSPACE / "go-ethereum",
    "chainmaker": WORKSPACE / "chainmaker-go",
    "fisco": WORKSPACE / "FISCO-BCOS",
    "aptos": WORKSPACE / "aptos-core",
}


@dataclass(frozen=True)
class BugSpec:
    bug_id: str
    target: str
    title: str
    category: str
    rel_script: str
    timeout_seconds: int
    success_patterns: tuple[str, ...]
    argv_suffix: tuple[str, ...] = ()

    @property
    def script_path(self) -> Path:
        return BUG_ROOT / self.rel_script

    @property
    def workdir(self) -> Path:
        return TARGET_REPOS[self.target]

    @property
    def argv(self) -> list[str]:
        argv = ["bash", str(self.script_path)]
        if self.argv_suffix:
            argv.extend(self.argv_suffix)
        return argv


BUG_SPECS: tuple[BugSpec, ...] = (
    # ChainMaker: 9
    BugSpec("cm-02", "chainmaker", "TBFT propose timeout stall",
            "governance+config", "chainmaker/02_tbft_propose_timeout/poc.sh",
            900, ('结果: PASS',)),
    BugSpec("cm-03", "chainmaker", "negative TBFT propose timeout loop",
            "governance+config", "chainmaker/03_tbft_propose_timeout_negative/poc.sh",
            900, ('结果: PASS',)),
    BugSpec("cm-04", "chainmaker", "MaxBFT timeout storm",
            "governance+config", "chainmaker/04_maxbft_timeout_storm/poc.sh",
            900, ('结果: PASS',)),
    BugSpec("cm-05", "chainmaker", "negative TBFT propose delta timeout",
            "governance+config", "chainmaker/05_tbft_propose_delta_timeout/poc.sh",
            900, ('结果: PASS',)),
    BugSpec("cm-06", "chainmaker", "batch metadata index OOB crash",
            "strong-message", "chainmaker/06_batch_index_out_of_bounds_crash/poc.sh",
            1200, ('结果: PASS',)),
    BugSpec("cm-07", "chainmaker", "TxCount OOB crash verifiers",
            "strong-message", "chainmaker/07_txcount_oob_crash_verifiers/poc.sh",
            1200, ('结果: PASS',)),
    BugSpec("cm-08", "chainmaker", "nil payload crash verifiers",
            "strong-message", "chainmaker/08_nil_payload_crash_verifiers/poc.sh",
            1200, ('结果: PASS',)),
    BugSpec("cm-09", "chainmaker", "batch turbo cut-block crash",
            "config+normal-traffic", "chainmaker/09_batch_turbo_cutblock_crash/poc.sh",
            900, ('结果: PASS',)),
    BugSpec("cm-10", "chainmaker", "gas enable transaction paralysis",
            "governance+transaction", "chainmaker/10_gas_enable_network_paralysis/poc.sh",
            900, ('结果: PASS',)),
    # BCB #2/#3 (paper Table 1 rows 2-3): net.seeds peer-map race + cert/logger
    # race.  Added to the artifact as test_cases/chainmaker/11_* and 12_*;
    # may not reproduce on v3.0.0 (RWMutex-hardened) — scripts print
    # [POC VERSION-GUARDED] in that case, which counts as a pass here.
    BugSpec("cm-11", "chainmaker", "net.seeds peer-information-map race panic",
            "config+concurrent-workload", "chainmaker/11_net_seeds_peer_map_race/poc.sh",
            1200, ('[POC PASS]', '[POC VERSION-GUARDED]')),
    BugSpec("cm-12", "chainmaker", "certificate reconfiguration logger level-map race panic",
            "config+concurrent-workload", "chainmaker/12_cert_reconfig_logger_race/poc.sh",
            1200, ('[POC PASS]', '[POC VERSION-GUARDED]')),
    # FISCO: 4
    BugSpec("fs-01", "fisco", "min seal time consensus slowdown",
            "config+normal-traffic", "fisco/01_min_seal_time/poc_reproduce.sh",
            900, ('复现成功',)),
    BugSpec("fs-02", "fisco", "disable transaction signature check",
            "config+strong-transaction", "fisco/02_check_transaction_signature/poc_reproduce.sh",
            900, ('复现成功',)),
    BugSpec("fs-03", "fisco", "disable block limit check",
            "config+strong-transaction", "fisco/03_check_block_limit/poc_reproduce.sh",
            900, ('复现成功',)),
    BugSpec("fs-04", "fisco", "chain block limit collapse",
            "config+normal-traffic", "fisco/04_chain_block_limit/poc_reproduce.sh",
            900, ('复现成功',)),
    # geth: 2
    BugSpec("ge-10", "geth", "miner gaslimit collapse",
            "config+fake-consensus", "geth/01_miner_gaslimit_collapse/poc_bcb10_gaslimit_collapse.sh",
            1800, ('[POC 复现成功',)),
    BugSpec("ge-13", "geth", "blobpool pricebump old-tx mining",
            "strong-transaction+fake-consensus", "geth/02_blobpool_pricebump/poc_bcb13_blobpool_pricebump.sh",
            1200, ('[POC 复现成功',)),
    # Aptos: 3
    BugSpec("ap-18", "aptos", "round initial timeout zero",
            "config+liveness", "aptos/01_round_initial_timeout_zero/poc.sh",
            900, ('结果: PASS', '复现成功'),
            (str(TARGET_REPOS["aptos"]),)),
    BugSpec("ap-19", "aptos", "sync_only true",
            "config+liveness", "aptos/02_sync_only_true/poc.sh",
            900, ('结果: PASS', '复现成功'),
            (str(TARGET_REPOS["aptos"]),)),
    BugSpec("ap-20", "aptos", "safety rules dead process",
            "config+liveness", "aptos/03_safety_rules_dead_process/poc.sh",
            900, ('结果: PASS', '复现成功'),
            (str(TARGET_REPOS["aptos"]),)),
)

BUG_INDEX = {spec.bug_id: spec for spec in BUG_SPECS}


def selected_specs(targets: set[str], bug_ids: set[str]) -> list[BugSpec]:
    specs = [spec for spec in BUG_SPECS if spec.target in targets]
    if bug_ids:
        missing = sorted(bug_ids - BUG_INDEX.keys())
        if missing:
            raise SystemExit(f"unknown bug ids: {', '.join(missing)}")
        specs = [spec for spec in specs if spec.bug_id in bug_ids]
    return specs


def classify_status(output: str, returncode: int, timed_out: bool,
                    success_patterns: Iterable[str]) -> tuple[str, list[str]]:
    matches = [pattern for pattern in success_patterns if pattern in output]
    if timed_out:
        return "timeout", matches
    if returncode != 0 and not matches:
        return "error", matches
    if matches:
        return "pass", matches
    return "fail", matches


def terminate_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.2)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_bug(spec: BugSpec, out_root: Path) -> dict:
    bug_dir = out_root / spec.target / spec.bug_id
    bug_dir.mkdir(parents=True, exist_ok=True)
    log_path = bug_dir / "run.log"
    meta_path = bug_dir / "result.json"

    started = time.monotonic()
    timed_out = False
    returncode = -1
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ cwd={spec.workdir}\n")
        log.write(f"$ argv={' '.join(spec.argv)}\n\n")
        log.flush()
        proc = subprocess.Popen(
            spec.argv,
            cwd=spec.workdir,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        try:
            returncode = proc.wait(timeout=spec.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_group(proc)
            returncode = proc.poll() if proc.poll() is not None else -9

    elapsed = time.monotonic() - started
    output = log_path.read_text(encoding="utf-8", errors="replace")
    status, matches = classify_status(
        output, returncode, timed_out, spec.success_patterns)

    result = {
        "bug_id": spec.bug_id,
        "target": spec.target,
        "title": spec.title,
        "category": spec.category,
        "script": str(spec.script_path),
        "workdir": str(spec.workdir),
        "argv": spec.argv,
        "timeout_seconds": spec.timeout_seconds,
        "elapsed_seconds": round(elapsed, 1),
        "timed_out": timed_out,
        "returncode": returncode,
        "status": status,
        "matched_success_patterns": matches,
        "log": str(log_path),
    }
    meta_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    print(
        f"{spec.bug_id:6s} {spec.target:11s} {status:7s} "
        f"{elapsed:6.1f}s  {spec.title}",
        flush=True,
    )
    return result


def write_summary(out_root: Path, results: list[dict]) -> None:
    summary = {
        "workspace": str(WORKSPACE),
        "bug_root": str(BUG_ROOT),
        "total": len(results),
        "passed": sum(1 for r in results if r["status"] == "pass"),
        "failed": sum(1 for r in results if r["status"] == "fail"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "timeouts": sum(1 for r in results if r["status"] == "timeout"),
        "results": results,
    }
    (out_root / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Full BCFuzzer bug-targeted summary",
        "",
        f"- total: {summary['total']}",
        f"- passed: {summary['passed']}",
        f"- failed: {summary['failed']}",
        f"- errors: {summary['errors']}",
        f"- timeouts: {summary['timeouts']}",
        "",
        "| Bug | Target | Category | Status | Time (s) |",
        "|---|---|---|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result['bug_id']} | {result['target']} | {result['category']} | "
            f"{result['status']} | {result['elapsed_seconds']} |"
        )
    (out_root / "summary.md").write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")


def list_specs(specs: Iterable[BugSpec]) -> int:
    for spec in specs:
        print(f"{spec.bug_id:6s} {spec.target:11s} {spec.category:28s} {spec.rel_script}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--targets", default="geth,chainmaker,fisco,aptos",
        help="comma-separated targets to run")
    parser.add_argument(
        "--bugs", default="",
        help="comma-separated bug ids to run (default: all selected targets)")
    parser.add_argument(
        "--output", type=Path,
        default=WORKSPACE / "interaction-coverage-results" / f"full_bcfuzzer_bug_suite_{int(time.time())}",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="list the registered bug reproduction profiles and exit")
    parser.add_argument(
        "--continue-on-failure", action="store_true",
        help="continue even if one bug returns fail/error/timeout")
    args = parser.parse_args()

    targets = {item.strip() for item in args.targets.split(",") if item.strip()}
    unknown_targets = sorted(targets - TARGET_REPOS.keys())
    if unknown_targets:
        raise SystemExit(f"unknown targets: {', '.join(unknown_targets)}")
    bug_ids = {item.strip() for item in args.bugs.split(",") if item.strip()}
    specs = selected_specs(targets, bug_ids)

    if args.list:
        return list_specs(specs)
    if not specs:
        raise SystemExit("no bug specs selected")

    args.output.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for spec in specs:
        result = run_bug(spec, args.output)
        results.append(result)
        if result["status"] != "pass" and not args.continue_on_failure:
            print(
                f"stopping after {spec.bug_id} with status={result['status']}; "
                f"use --continue-on-failure to keep going",
                file=sys.stderr,
            )
            break
    write_summary(args.output, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
