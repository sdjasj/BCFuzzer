#!/usr/bin/env python3
"""Run Aptos live-node coverage arms on a real two-validator local swarm.

For every arm, Forge creates a fresh two-validator network. Forge is then
detached and both validators are restarted with the coverage-instrumented
aptos-node; validator 0 receives the arm's configuration while validator 1
keeps its peer configuration. Coverage is merged across both validators so
multi-node traffic is not hidden behind a single designated node.

The long-running node cannot reach LLVM's normal atexit profile writer when
it is killed.  ``llvm_profile_flush.c`` supplies an explicit, marker-driven
flush hook without modifying Aptos source code.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/home/geth/tse/BCFuzzer_upstream/source_code/common")
from targets import apply_case  # noqa: E402

ROOT = Path("/home/geth/tse/aptos-core")
COVERED_NODE = ROOT / "target/llvm-cov-target/performance/aptos-node"
PEER_NODE = ROOT / "target-x86-64/release/aptos-node"
FORGE = ROOT / "target-x86-64/release/forge"
LLVM_BIN = (Path("/home/geth/.rustup/toolchains/1.94.1-x86_64-unknown-linux-gnu")
            / "lib/rustlib/x86_64-unknown-linux-gnu/bin")
LLVM_PROFDATA = LLVM_BIN / "llvm-profdata"
LLVM_COV = LLVM_BIN / "llvm-cov"
FLUSH_SOURCE = Path(__file__).with_name("llvm_profile_flush.c")
FLUSH_LIBRARY = Path("/tmp/libbcfuzzer_profile_flush.so")
ROOT_ACCOUNT = "0xa550c18"
BCFUZZER_ARMS = {"fixed", "varied"}
ACTIVE_WORKLOAD_ARMS = {"varied"}
CASES = ["mempool-validator-local", "fullnode-forwarding-local"]
APTOS_COVERAGE_MODULES = ["/mempool/", "/crates/validator-transaction-pool/"]

for proxy_key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(proxy_key, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

# Explicit validator defaults form the common configuration input layer.  The
# same eligible options are exposed to every comparison tool.
MEMPOOL_INPUT = {
    "capacity": 2_000_000,
    "capacity_bytes": 2 * 1024 * 1024 * 1024,
    "capacity_per_user": 100,
    "default_failovers": 1,
    "max_broadcasts_per_peer": 2,
    "max_network_channel_size": 1024,
    "shared_mempool_ack_timeout_ms": 2_000,
    "shared_mempool_backoff_interval_ms": 30_000,
    "shared_mempool_batch_size": 200,
    "shared_mempool_max_batch_bytes": 4_194_304,
    "shared_mempool_max_concurrent_inbound_syncs": 4,
    "shared_mempool_tick_interval_ms": 10,
    "shared_mempool_peer_update_interval_ms": 1_000,
    "shared_mempool_failover_delay_ms": 500,
    "system_transaction_timeout_secs": 600,
    "system_transaction_gc_interval_ms": 60_000,
}
MEMPOOL_BOUNDS = {
    "capacity": (1, 10_000_000),
    "capacity_bytes": (1024, 8 * 1024 * 1024 * 1024),
    "capacity_per_user": (1, 100_000),
    "default_failovers": (0, 100),
    "max_broadcasts_per_peer": (1, 1_000),
    "max_network_channel_size": (1, 1_000_000),
    "shared_mempool_ack_timeout_ms": (1, 600_000),
    "shared_mempool_backoff_interval_ms": (1, 600_000),
    "shared_mempool_batch_size": (1, 100_000),
    "shared_mempool_max_batch_bytes": (1024, 256 * 1024 * 1024),
    "shared_mempool_max_concurrent_inbound_syncs": (1, 1_000),
    "shared_mempool_tick_interval_ms": (1, 60_000),
    "shared_mempool_peer_update_interval_ms": (1, 600_000),
    "shared_mempool_failover_delay_ms": (1, 600_000),
    "system_transaction_timeout_secs": (1, 86_400),
    "system_transaction_gc_interval_ms": (1, 3_600_000),
}


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _bounded_int(value, low: int, high: int, default: int) -> int:
    if isinstance(value, bool):
        number = default
    else:
        try:
            number = int(str(value).strip().strip('"').strip("'"), 0)
        except (TypeError, ValueError):
            number = default
    return max(low, min(high, number))


def sanitize_mempool_config(config: Path) -> int:
    data = read_yaml(config)
    mempool = data.setdefault("mempool", {})
    changed = 0
    for key, default in MEMPOOL_INPUT.items():
        low, high = MEMPOOL_BOUNDS[key]
        new_value = _bounded_int(mempool.get(key, default), low, high, default)
        if mempool.get(key) != new_value:
            mempool[key] = new_value
            changed += 1
    if changed:
        config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return changed


def api_url(config: Path) -> str:
    return f"http://{read_yaml(config)['api']['address']}/v1"


def ledger_version(config: Path) -> int | None:
    try:
        with urllib.request.urlopen(api_url(config), timeout=3) as response:
            return int(json.loads(response.read())["ledger_version"])
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return None


def wait_for_network(configs: list[Path], timeout: int = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        versions = [ledger_version(config) for config in configs]
        if all(version is not None and version > 0 for version in versions):
            return True
        time.sleep(2)
    return False


def pids_for_config(config: Path) -> list[int]:
    needle = str(config).encode()
    pids = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if needle in argv:
            pids.append(int(proc.name))
    return pids


def stop_config_processes(config: Path, timeout: int = 15) -> None:
    pids = pids_for_config(config)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [pid for pid in pids if Path(f"/proc/{pid}").exists()]
        if not alive:
            return
        time.sleep(0.2)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def kill_processes_under(path: Path, timeout: int = 15) -> None:
    needle = str(path).encode()
    victims = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if needle in cmdline:
            victims.append(int(proc.name))
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while victims and time.monotonic() < deadline:
        victims = [pid for pid in victims if Path(f"/proc/{pid}").exists()]
        if victims:
            time.sleep(0.2)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def launch_swarm(work: Path, log_path: Path) -> tuple[Path, Path, str]:
    """Create and start a fresh two-validator swarm, then detach Forge."""
    command = [
        str(FORGE), "--suite", "run_forever", "--num-validators", "2",
        "test", "local-swarm", "--swarmdir", str(work),
        "--aptos-node-binary", str(PEER_NODE),
    ]
    errors: list[str] = []
    for attempt in range(1, 4):
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n=== launch attempt {attempt} ===\n")
            log.flush()
            forge = subprocess.Popen(
                command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            cfg0, cfg1 = work / "0/node.yaml", work / "1/node.yaml"
            launch_error = None
            try:
                if not wait_for_network([cfg0, cfg1]):
                    launch_error = "Forge did not produce a live two-validator swarm"
                else:
                    time.sleep(2)
                    stop_config_processes(cfg0)
                    if ledger_version(cfg1) is None:
                        peer_log = (work / "1/peer-restart.log").open("a", encoding="utf-8")
                        subprocess.Popen(
                            [str(PEER_NODE), "-f", str(cfg1)], cwd=cfg1.parent,
                            stdout=peer_log, stderr=subprocess.STDOUT, start_new_session=True)
                        if not wait_for_network([cfg1], timeout=90):
                            launch_error = "unmodified validator 1 did not restart"
                    if launch_error is None:
                        return cfg0, cfg1, (work / "root_key").read_text().strip()
            finally:
                forge.terminate()
                try:
                    forge.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    forge.kill()
                    forge.wait()
            errors.append(f"attempt {attempt}: {launch_error or 'unknown launch error'}")
        kill_processes_under(work)
    raise RuntimeError("; ".join(errors) if errors else "Forge did not produce a live two-validator swarm")


def add_common_config(config: Path) -> str:
    data = read_yaml(config)
    mempool = data.setdefault("mempool", {})
    for key, value in MEMPOOL_INPUT.items():
        mempool.setdefault(key, value)
    rendered = yaml.safe_dump(data, sort_keys=False)
    config.write_text(rendered, encoding="utf-8")
    return rendered


def _set_yaml_path(root, dotted_path: str, value) -> None:
    parts = dotted_path.split(".")
    current = root
    for index, part in enumerate(parts[:-1]):
        if isinstance(current, list):
            current = current[int(part)]
            continue
        next_part = parts[index + 1]
        try:
            int(next_part)
            next_part_is_index = True
        except ValueError:
            next_part_is_index = False
        if part not in current or current[part] is None:
            current[part] = [] if next_part_is_index else {}
        current = current[part]
    leaf = parts[-1]
    if isinstance(current, list):
        current[int(leaf)] = value
    else:
        current[leaf] = value


def overlay_case_config(config: Path, case: str, seed: int) -> None:
    run_dir = Path("/tmp") / f"aptos-case-{seed}-{os.getpid()}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = apply_case("aptos", ROOT, case, run_dir, seed)
    data = read_yaml(config)
    for patch in meta.get("patches", []):
        _set_yaml_path(data, patch["path"], patch["value"])
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def mutate_node_config(config: Path, tool: str, seed: int) -> int:
    if tool in ("fixed", "varied"):
        return 0
    from campaign_baseline_real import mutate_with_tool

    mutated = mutate_with_tool(
        config, tool, seed, target="aptos", eligible_sections={"mempool"})
    sanitize_mempool_config(config)
    return mutated


def build_flush_helper() -> str:
    if (not FLUSH_LIBRARY.exists() or
            FLUSH_LIBRARY.stat().st_mtime < FLUSH_SOURCE.stat().st_mtime):
        subprocess.run(
            ["gcc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra",
             "-Werror", "-pthread", "-ldl", str(FLUSH_SOURCE),
             "-o", str(FLUSH_LIBRARY)],
            check=True, timeout=120)
    result = subprocess.run(
        ["nm", str(COVERED_NODE)], check=True, capture_output=True,
        text=True, timeout=120)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == "__llvm_profile_write_file":
            return "0x" + fields[0]
    raise RuntimeError("instrumented aptos-node has no LLVM profile writer")


def start_covered_node(config: Path, log_path: Path, profile: Path,
                       marker: Path, write_offset: str) -> subprocess.Popen:
    marker.unlink(missing_ok=True)
    profile.unlink(missing_ok=True)
    marker_path = marker.resolve()
    profile_path = profile.resolve()
    env = os.environ.copy()
    env.update({
        "LLVM_PROFILE_FILE": str(profile_path),
        "LD_PRELOAD": str(FLUSH_LIBRARY),
        "BCFUZZER_PROFILE_FLUSH_MARKER": str(marker_path),
        "BCFUZZER_PROFILE_WRITE_OFFSET": write_offset,
        "RUST_LOG": "info",
    })
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(COVERED_NODE), "-f", str(config)], cwd=config.parent,
        env=env, stdout=log, stderr=subprocess.STDOUT)
    process._bcfuzzer_log = log  # type: ignore[attr-defined]
    return process


def wait_for_node(process: subprocess.Popen, config: Path,
                  timeout: int = 120) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if ledger_version(config) is not None:
            return True
        time.sleep(2)
    return False


def flush_and_stop(process: subprocess.Popen, marker: Path) -> None:
    if process.poll() is None:
        marker.touch()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    log = getattr(process, "_bcfuzzer_log", None)
    if log is not None:
        log.close()


async def submit_transfers(api: str, root_key: str, count: int,
                           varied: bool) -> tuple[int, int]:
    from aptos_sdk import async_client, ed25519
    from aptos_sdk.account import Account
    from aptos_sdk.account_address import AccountAddress

    account = Account(
        AccountAddress.from_str_relaxed(ROOT_ACCOUNT),
        ed25519.PrivateKey.from_str(root_key))
    client = async_client.RestClient(api)
    accepted = rejected = 0
    recipients = [
        account.address(),
        AccountAddress.from_str_relaxed("0x1"),
        AccountAddress.from_str_relaxed("0xcafe"),
        AccountAddress.from_str_relaxed("0xbeef"),
        AccountAddress.from_str_relaxed("0x1234"),
    ]
    amounts = [1] if not varied else [1, 2, 3, 5, 8, 13, 21, 34]
    try:
        for index in range(count):
            try:
                tx_hash = await asyncio.wait_for(
                    client.bcs_transfer(
                        account, recipients[index % len(recipients)],
                        amounts[index % len(amounts)]),
                    timeout=8,
                )
                await asyncio.wait_for(
                    client.wait_for_transaction(tx_hash),
                    timeout=12,
                )
                accepted += 1
            except Exception:
                rejected += 1
    finally:
        await client.close()
    return accepted, rejected


def malformed_transaction_probes(api: str) -> int:
    payloads = [b"", b"\x00", b"not-a-signed-transaction", bytes(range(64))]
    observed = 0
    for payload in payloads:
        request = urllib.request.Request(
            api + "/transactions", data=payload, method="POST",
            headers={"Content-Type": "application/x.aptos.signed_transaction+bcs"})
        try:
            urllib.request.urlopen(request, timeout=5).read()
        except urllib.error.HTTPError:
            observed += 1
        except OSError:
            pass
    return observed


def interact(arm: str, config: Path, root_key: str) -> dict:
    varied = arm == "varied"
    if not varied:
        return {"accepted_transactions": 0,
                "rejected_transactions": 0,
                "malformed_probes": 0}
    accepted, rejected = asyncio.run(submit_transfers(
        api_url(config), root_key, 48, True))
    malformed = malformed_transaction_probes(api_url(config))
    return {"accepted_transactions": accepted,
            "rejected_transactions": rejected,
            "malformed_probes": malformed}


def coverage_metrics(profile_dir: Path, artifact_dir: Path,
                     include_patterns: list[str] | None = None) -> dict:
    profiles = [path for path in profile_dir.glob("*.profraw")
                if path.stat().st_size > 0]
    if not profiles:
        return {"covered_lines": 0, "total_lines": 0,
                "coverage_pct": 0.0, "profile_count": 0}
    profdata = artifact_dir / "node.profdata"
    subprocess.run(
        [str(LLVM_PROFDATA), "merge", "-sparse", *map(str, profiles),
         "-o", str(profdata)], check=True, timeout=300)
    sources = [
        str(path) for path in ROOT.rglob("*.rs")
        if (not include_patterns or any(pattern in str(path) for pattern in include_patterns))
        and "tests" not in path.parts
        and "benches" not in path.parts
        and "/target/" not in str(path)
    ]
    report_path = artifact_dir / "coverage-summary.txt"
    completed = subprocess.run(
        [str(LLVM_COV), "report", str(COVERED_NODE),
         f"-instr-profile={profdata}", *sources],
        check=True, capture_output=True, text=True, timeout=300)
    report_path.write_text(completed.stdout, encoding="utf-8")
    covered = total = files = 0
    for line in completed.stdout.splitlines():
        line = line.rstrip()
        if (not line or line.startswith("Filename") or
                line.startswith("---") or line.startswith("TOTAL")):
            continue
        fields = line.split()
        if len(fields) < 13:
            continue
        line_count = int(fields[-6])
        missed_lines = int(fields[-5])
        total += line_count
        covered += line_count - missed_lines
        files += 1
    return {
        "covered_lines": covered,
        "total_lines": total,
        "coverage_pct": covered / total * 100.0 if total else 0.0,
        "profile_count": len(profiles),
        "source_files": files,
    }


def lcov_line_sets(profdata: Path,
                   include_patterns: list[str] | None = None) -> tuple[set[str], set[str]]:
    if not profdata.exists():
        return set(), set()
    command = [
        str(LLVM_COV), "export", str(COVERED_NODE),
        f"-instr-profile={profdata}", "--format=lcov",
        "--ignore-filename-regex=(/home/geth/\\.cargo/|/rustc/|/target/)",
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    assert process.stdout is not None
    assert process.stderr is not None
    root_prefix = str(ROOT) + "/"
    universe: set[str] = set()
    hit: set[str] = set()
    current_file: str | None = None
    current_lines: set[int] = set()
    current_hit: set[int] = set()
    for line in process.stdout:
        if line.startswith("SF:"):
            current_file = line[3:].strip()
            current_lines = set()
            current_hit = set()
        elif line.startswith("DA:") and current_file:
            fields = line[3:].strip().split(",", 1)
            line_no = int(fields[0])
            count = int(fields[1])
            current_lines.add(line_no)
            if count > 0:
                current_hit.add(line_no)
        elif line.startswith("end_of_record") and current_file:
            if current_file.startswith(root_prefix):
                path_parts = Path(current_file).parts
                if ("tests" not in path_parts and "benches" not in path_parts and
                        (not include_patterns or
                         any(pattern in current_file for pattern in include_patterns))):
                    rel = current_file[len(root_prefix):]
                    universe.update(f"{rel}:{line_no}"
                                    for line_no in current_lines)
                    hit.update(f"{rel}:{line_no}" for line_no in current_hit)
            current_file = None
    stderr = process.stderr.read()
    if process.wait(timeout=60) != 0:
        raise RuntimeError(stderr.strip())
    return universe, hit


def normalize_lcov_results(out_dir: Path, results: dict,
                           *, full_coverage: bool = False) -> dict:
    per_arm = {}
    line_universe: set[str] = set()
    for arm in results:
        universe, hit = lcov_line_sets(
            out_dir / arm / "node.profdata",
            include_patterns=None if full_coverage else APTOS_COVERAGE_MODULES)
        per_arm[arm] = {"universe": universe, "hit": hit}
        line_universe.update(universe)
    total_lines = len(line_universe)
    for arm, result in results.items():
        covered_lines = len(per_arm[arm]["hit"] & line_universe)
        result["summary_only_covered_lines"] = result.get("covered_lines", 0)
        result["summary_only_total_lines"] = result.get("total_lines", 0)
        result["covered_lines"] = covered_lines
        result["total_lines"] = total_lines
        result["coverage_pct"] = (
            covered_lines / total_lines * 100.0 if total_lines else 0.0)
        result["coverage_method"] = (
            "LCOV DA line hits over union line universe across Aptos arms")
        result["line_universe_arms"] = sorted(results)
        result["coverage_scope"] = (
            "merged full line coverage across both covered validators"
            if full_coverage else
            "merged Aptos mempool-relevant line coverage across both covered validators"
        )
        arm_dir = out_dir / arm
        (arm_dir / "result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return results


def run_arm(arm: str, out_dir: Path, seed: int, max_rounds: int,
            converge_rounds: int, budget_minutes: float = 0.0,
            full_budget: bool = False, *,
            full_coverage: bool = False) -> dict:
    arm_dir = out_dir / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = arm_dir / "profiles"
    profile_dir.mkdir(exist_ok=True)
    work = Path("/tmp") / f"aptos-live-{arm}-{os.getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    write_offset = build_flush_helper()
    started = time.monotonic()
    deadline = (started + budget_minutes * 60.0) if budget_minutes > 0 else None
    round_limit = max_rounds
    if budget_minutes > 0 and max_rounds <= 0:
        round_limit = 0
    timeline = []
    cfg0 = cfg1 = None
    config_only_baseline = arm not in ACTIVE_WORKLOAD_ARMS
    try:
        cfg0, cfg1, root_key = launch_swarm(work, arm_dir / "forge.log")
        base_config = add_common_config(cfg0)
        round_no = 1
        while True:
            if round_limit > 0 and round_no > round_limit:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            cfg0.write_text(base_config, encoding="utf-8")
            round_seed = seed + round_no
            case = CASES[(round_no - 1) % len(CASES)]
            overlay_case_config(cfg0, case, round_seed)
            mutated = mutate_node_config(cfg0, arm, round_seed)
            stop_config_processes(cfg0)
            stop_config_processes(cfg1)
            marker0 = profile_dir / f"round{round_no:03d}-validator0.flush"
            marker1 = profile_dir / f"round{round_no:03d}-validator1.flush"
            profile0 = profile_dir / f"round{round_no:03d}-validator0.profraw"
            profile1 = profile_dir / f"round{round_no:03d}-validator1.profraw"
            process0 = start_covered_node(
                cfg0, arm_dir / f"node0-round{round_no:03d}.log",
                profile0, marker0, write_offset)
            process1 = start_covered_node(
                cfg1, arm_dir / f"node1-round{round_no:03d}.log",
                profile1, marker1, write_offset)
            admitted0 = wait_for_node(process0, cfg0)
            admitted1 = wait_for_node(process1, cfg1)
            admitted = admitted0 and admitted1
            before0 = ledger_version(cfg0)
            before1 = ledger_version(cfg1)
            activity = {"accepted_transactions": 0,
                        "rejected_transactions": 0,
                        "malformed_probes": 0,
                        "peer_restart_ok": False,
                        "post_restart_accepts": 0,
                        "post_restart_rejects": 0}
            if admitted:
                if config_only_baseline:
                    time.sleep(8)
                else:
                    activity = interact(arm, cfg0, root_key)
                    if arm == "varied":
                        more_acc, more_rej = asyncio.run(
                            submit_transfers(api_url(cfg1), root_key, 24, True))
                        time.sleep(3)
                        acc1, rej1 = asyncio.run(
                            submit_transfers(api_url(cfg0), root_key, 24, True))
                        post_malformed = (
                            malformed_transaction_probes(api_url(cfg0)) +
                            malformed_transaction_probes(api_url(cfg1))
                        )
                        activity["peer_restart_ok"] = admitted
                        activity["post_restart_accepts"] = more_acc + acc1
                        activity["post_restart_rejects"] = more_rej + rej1
                        activity["malformed_probes"] += post_malformed
                time.sleep(8)
            flush_and_stop(process0, marker0)
            flush_and_stop(process1, marker1)
            after0 = ledger_version(cfg0)
            after1 = ledger_version(cfg1)
            metrics = coverage_metrics(
                profile_dir, arm_dir,
                include_patterns=None if full_coverage else APTOS_COVERAGE_MODULES)
            trial = {
                "round": round_no,
                "case": case,
                "seed": round_seed,
                "mutated_options": mutated,
                "network_admitted": admitted,
                "validator0_admitted": admitted0,
                "validator1_admitted": admitted1,
                "validator0_ledger_before": before0,
                "validator1_ledger_before": before1,
                "validator0_ledger_after": after0,
                "validator1_ledger_after": after1,
                "profile_bytes": (
                    (profile0.stat().st_size if profile0.exists() else 0) +
                    (profile1.stat().st_size if profile1.exists() else 0)
                ),
                **activity,
                **metrics,
            }
            timeline.append(trial)
            print(
                f"{arm} round={round_no} admitted={admitted} "
                f"covered={metrics['covered_lines']} mutated={mutated}",
                flush=True)
            if not full_budget and len(timeline) >= converge_rounds:
                window = timeline[-converge_rounds:]
                if len({item["covered_lines"] for item in window}) == 1:
                    break
            round_no += 1
    finally:
        if cfg0 is not None:
            stop_config_processes(cfg0)
        if cfg1 is not None:
            stop_config_processes(cfg1)
        shutil.rmtree(work, ignore_errors=True)

    converged = False
    if len(timeline) >= converge_rounds:
        window = timeline[-converge_rounds:]
        converged = len({item["covered_lines"] for item in window}) == 1
    final = timeline[-1] if timeline else coverage_metrics(profile_dir, arm_dir)
    if not timeline:
        final = coverage_metrics(
            profile_dir, arm_dir,
            include_patterns=None if full_coverage else APTOS_COVERAGE_MODULES)
    result = {
        "arm": arm,
        "config_only_baseline": config_only_baseline,
        "topology": "two-validator local swarm",
        "coverage_scope": (
            "merged full line coverage across both covered validators"
            if full_coverage else
            "merged Aptos mempool-relevant line coverage across both covered validators"
        ),
        "rounds": len(timeline),
        "converged": converged,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "timeline": timeline,
        "covered_lines": final["covered_lines"],
        "total_lines": final["total_lines"],
        "coverage_pct": final["coverage_pct"],
    }
    (arm_dir / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--arms", default="fixed,varied,ecfuzz,conferr,conftest,confdiag")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20270803)
    parser.add_argument("--max-rounds", type=int, default=3,
                        help="maximum rounds per arm; set 0 with --budget-minutes for unlimited looping")
    parser.add_argument("--converge-rounds", type=int, default=2)
    parser.add_argument("--budget-minutes", type=float, default=0.0,
                        help="wall-clock budget per arm; 0 disables budgeted looping")
    parser.add_argument("--full-budget", action="store_true",
                        help="run for the full wall-clock budget even after convergence")
    parser.add_argument("--full-coverage", action="store_true",
                        help="collect full line coverage instead of scoped mempool modules")
    parser.add_argument(
        "--normalize-only", action="store_true",
        help="reuse existing per-arm profdata and rewrite normalized summary")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.normalize_only:
        summary_path = args.output / "summary.json"
        if summary_path.exists():
            results = json.loads(summary_path.read_text(encoding="utf-8"))
        else:
            results = {}
            for arm_dir in args.output.iterdir():
                result_path = arm_dir / "result.json"
                if result_path.exists():
                    results[arm_dir.name] = json.loads(
                        result_path.read_text(encoding="utf-8"))
        normalize_lcov_results(args.output, results, full_coverage=args.full_coverage)
        return 0

    results = {}
    for index, arm_name in enumerate(args.arms.split(",")):
        arm = arm_name.strip()
        results[arm] = run_arm(
            arm, args.output, args.seed + index * 1000,
            args.max_rounds, args.converge_rounds,
            args.budget_minutes, args.full_budget,
            full_coverage=args.full_coverage)
        (args.output / "summary.json").write_text(
            json.dumps(results, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    normalize_lcov_results(args.output, results, full_coverage=args.full_coverage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
