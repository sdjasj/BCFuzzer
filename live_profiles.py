"""Live-system interaction profiles for the full BCFuzzer implementation.

These profiles are first-class BCFuzzer interactions, not a separate harness.
They encode the strongest known transaction / inter-node-message mutation
workflows needed to exercise the audited inter-node bug corpus on local private
clusters.  Some profiles are simple one-node config skew plus normal traffic;
others require fake consensus clients, governance transactions, or malicious
proposer/message mutation.  Those stronger profiles are represented here as
named interaction operators so the scheduler can select them explicitly.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
BUG_ROOT = WORKSPACE_ROOT / "inter-node-bugs-final"
TARGET_ROOTS = {
    "geth": WORKSPACE_ROOT / "go-ethereum",
    "chainmaker": WORKSPACE_ROOT / "chainmaker-go",
    "fisco": WORKSPACE_ROOT / "FISCO-BCOS",
    "aptos": WORKSPACE_ROOT / "aptos-core",
}


@dataclass(frozen=True)
class LiveProfile:
    interaction: str
    target: str
    title: str
    category: str
    rel_script: str
    timeout_seconds: int
    success_patterns: tuple[str, ...]
    bug_ids: tuple[str, ...] = ()
    argv_suffix: tuple[str, ...] = ()

    @property
    def script_path(self) -> Path:
        return BUG_ROOT / self.rel_script

    @property
    def workdir(self) -> Path:
        return TARGET_ROOTS[self.target]

    @property
    def argv(self) -> list[str]:
        argv = ["bash", str(self.script_path)]
        if self.argv_suffix:
            argv.extend(self.argv_suffix)
        return argv


LIVE_PROFILES: tuple[LiveProfile, ...] = (
    LiveProfile(
        "geth-engine-gaslimit-collapse",
        "geth",
        "Fake-consensus payload drift that collapses chain gas limit",
        "config+fake-consensus",
        "geth/01_miner_gaslimit_collapse/poc_bcb10_gaslimit_collapse.sh",
        1800,
        ("[POC 复现成功",),
        bug_ids=("ge-10",),
    ),
    LiveProfile(
        "geth-blob-replacement-stickiness",
        "geth",
        "Blob replacement mutation rejected by malicious blobpool price bump",
        "strong-transaction+fake-consensus",
        "geth/02_blobpool_pricebump/poc_bcb13_blobpool_pricebump.sh",
        1200,
        ("[POC 复现成功",),
        bug_ids=("ge-13",),
    ),
    LiveProfile(
        "chainmaker-governance-tbft-timeout",
        "chainmaker",
        "Governance mutation sets extreme TBFT propose timeout",
        "governance+config",
        "chainmaker/02_tbft_propose_timeout/poc.sh",
        900,
        ("结果: PASS",),
        bug_ids=("cm-02",),
    ),
    LiveProfile(
        "chainmaker-governance-tbft-timeout-negative",
        "chainmaker",
        "Governance mutation sets negative TBFT propose timeout",
        "governance+config",
        "chainmaker/03_tbft_propose_timeout_negative/poc.sh",
        900,
        ("结果: PASS",),
        bug_ids=("cm-03",),
    ),
    LiveProfile(
        "chainmaker-governance-maxbft-timeout",
        "chainmaker",
        "Governance mutation drives MaxBFT timeout storm",
        "governance+config",
        "chainmaker/04_maxbft_timeout_storm/poc.sh",
        900,
        ("结果: PASS",),
        bug_ids=("cm-04",),
    ),
    LiveProfile(
        "chainmaker-governance-tbft-delta-negative",
        "chainmaker",
        "Governance mutation sets negative TBFT propose delta timeout",
        "governance+config",
        "chainmaker/05_tbft_propose_delta_timeout/poc.sh",
        900,
        ("结果: PASS",),
        bug_ids=("cm-05",),
    ),
    LiveProfile(
        "chainmaker-malicious-batch-index",
        "chainmaker",
        "Malicious proposer mutates batch metadata and crashes verifiers",
        "strong-message",
        "chainmaker/06_batch_index_out_of_bounds_crash/poc.sh",
        1200,
        ("结果: PASS",),
        bug_ids=("cm-06",),
    ),
    LiveProfile(
        "chainmaker-malicious-txcount",
        "chainmaker",
        "Malicious proposer inflates TxCount and crashes verifiers",
        "strong-message",
        "chainmaker/07_txcount_oob_crash_verifiers/poc.sh",
        1200,
        ("结果: PASS",),
        bug_ids=("cm-07",),
    ),
    LiveProfile(
        "chainmaker-malicious-nil-payload",
        "chainmaker",
        "Malicious proposer injects nil payload transaction into a block",
        "strong-message",
        "chainmaker/08_nil_payload_crash_verifiers/poc.sh",
        1200,
        ("结果: PASS",),
        bug_ids=("cm-08",),
    ),
    LiveProfile(
        "chainmaker-batch-turbo-crash",
        "chainmaker",
        "One-node batch pool skew with turbo+gas crashes peers",
        "config+normal-traffic",
        "chainmaker/09_batch_turbo_cutblock_crash/poc.sh",
        900,
        ("结果: PASS",),
        bug_ids=("cm-09",),
    ),
    LiveProfile(
        "chainmaker-gas-enable-paralysis",
        "chainmaker",
        "Governance enables gas and paralyzes all user transactions",
        "governance+transaction",
        "chainmaker/10_gas_enable_network_paralysis/poc.sh",
        900,
        ("结果: PASS",),
        bug_ids=("cm-10",),
    ),
    LiveProfile(
        "fisco-min-seal-time-drift",
        "fisco",
        "Malicious min_seal_time slows consensus for the whole PBFT network",
        "config+normal-traffic",
        "fisco/01_min_seal_time/poc_reproduce.sh",
        900,
        ("复现成功",),
        bug_ids=("fs-01",),
    ),
    LiveProfile(
        "fisco-invalid-signature-acceptance",
        "fisco",
        "Signature-check bypass lets a node inject invalid signed transactions",
        "config+strong-transaction",
        "fisco/02_check_transaction_signature/poc_reproduce.sh",
        900,
        ("复现成功",),
        bug_ids=("fs-02",),
    ),
    LiveProfile(
        "fisco-expired-blocklimit-acceptance",
        "fisco",
        "Block-limit check bypass lets a node inject expired transactions",
        "config+strong-transaction",
        "fisco/03_check_block_limit/poc_reproduce.sh",
        900,
        ("复现成功",),
        bug_ids=("fs-03",),
    ),
    LiveProfile(
        "fisco-chain-block-limit-collapse",
        "fisco",
        "Genesis block-limit skew starves the leader transaction pool",
        "config+normal-traffic",
        "fisco/04_chain_block_limit/poc_reproduce.sh",
        900,
        ("复现成功",),
        bug_ids=("fs-04",),
    ),
    LiveProfile(
        "aptos-round-timeout-zero",
        "aptos",
        "Consensus initial timeout zero causes permanent TC-only liveness loss",
        "config+liveness",
        "aptos/01_round_initial_timeout_zero/poc.sh",
        900,
        ("结果: PASS", "复现成功"),
        bug_ids=("ap-18",),
        argv_suffix=(str(TARGET_ROOTS["aptos"]),),
    ),
    LiveProfile(
        "aptos-sync-only",
        "aptos",
        "Consensus sync_only isolates a validator from block proposal progress",
        "config+liveness",
        "aptos/02_sync_only_true/poc.sh",
        900,
        ("结果: PASS", "复现成功"),
        bug_ids=("ap-19",),
        argv_suffix=(str(TARGET_ROOTS["aptos"]),),
    ),
    LiveProfile(
        "aptos-safety-rules-dead",
        "aptos",
        "Consensus safety-rules process misconfiguration freezes signing",
        "config+liveness",
        "aptos/03_safety_rules_dead_process/poc.sh",
        900,
        ("结果: PASS", "复现成功"),
        bug_ids=("ap-20",),
        argv_suffix=(str(TARGET_ROOTS["aptos"]),),
    ),
)

LIVE_PROFILE_INDEX = {profile.interaction: profile for profile in LIVE_PROFILES}


def has_live_profile(interaction: str) -> bool:
    return interaction in LIVE_PROFILE_INDEX


def terminate_process_group(proc: subprocess.Popen[str]) -> None:
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


def classify_status(output: str, returncode: int, timed_out: bool,
                    success_patterns: tuple[str, ...]) -> tuple[str, list[str]]:
    matches = [pattern for pattern in success_patterns if pattern in output]
    if timed_out:
        return "timeout", matches
    if returncode != 0 and not matches:
        return "error", matches
    if matches:
        return "success", matches
    return "failed", matches


def run_live_profile(interaction: str, run_dir: Path, coverage_dir: Path, seed: int) -> dict:
    profile = LIVE_PROFILE_INDEX[interaction]
    coverage_dir.mkdir(parents=True, exist_ok=True)
    log_path = coverage_dir / f"{interaction}.live.log"
    started = time.monotonic()
    timed_out = False
    returncode = -1
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "BCFUZZER_SEED": str(seed),
        "BCFUZZER_RUN_DIR": str(run_dir),
        "BCFUZZER_COVERAGE_DIR": str(coverage_dir),
    }
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ cwd={profile.workdir}\n")
        log.write(f"$ argv={' '.join(profile.argv)}\n\n")
        log.flush()
        proc = subprocess.Popen(
            profile.argv,
            cwd=profile.workdir,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
        try:
            returncode = proc.wait(timeout=profile.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_group(proc)
            returncode = proc.poll() if proc.poll() is not None else -9
    elapsed = time.monotonic() - started
    output = log_path.read_text(encoding="utf-8", errors="replace")
    status, matches = classify_status(output, returncode, timed_out, profile.success_patterns)
    bug_triggered = 1.0 if status == "success" else 0.0
    result = {
        "status": status,
        "argv": profile.argv,
        "returncode": returncode,
        "timed_out": timed_out,
        "metrics": {
            "bug_triggered": bug_triggered,
            "bug_profile_count": float(len(profile.bug_ids) or 1),
        },
        "profile": {
            "interaction": profile.interaction,
            "title": profile.title,
            "category": profile.category,
            "bug_ids": list(profile.bug_ids),
            "script": str(profile.script_path),
            "matched_success_patterns": matches,
            "elapsed_seconds": round(elapsed, 1),
            "log": str(log_path),
        },
    }
    (coverage_dir / "live_profile_result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result
