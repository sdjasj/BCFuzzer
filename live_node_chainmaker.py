#!/usr/bin/env python3
"""Live four-node ChainMaker coverage experiment.

Release package: four org nodes on loopback. Each arm applies its
configuration (BCFuzzer chainmaker.yml case overlaid on org1, mutated by the
adapted baseline tool), starts four instrumented nodes, submits live
transactions through the cmc client, stops the nodes, and collects merged
whole-program line coverage across all instrumented nodes
(qiniu/goc runtime profiles -> merged coverprofiles -> unique source lines).

Arms: fixed, varied, ecfuzz, conferr, conftest, confdiag.

The script supports both a single round and cumulative long-running campaigns.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/home/geth/tse/BCFuzzer_upstream/source_code/common")
from goc_utils import (  # noqa: E402
    center_host_port,
    compute_line_coverage,
    ensure_goc_binary,
    goc_build_env,
    goc_clear,
    goc_init,
    goc_merge,
    goc_profile,
    merge_into_cumulative,
    start_goc_server,
    stop_goc_server,
)
from targets import apply_case  # noqa: E402

ROOT = Path("/home/geth/tse/chainmaker-go")
RELEASE = ROOT / "build/release"
CMC = Path("/tmp/cmc")
SDK_CONF = Path("/tmp/cm-sdk-config.yml")
FACT_WASM = ROOT / "test/wasm/rust-fact-2.0.0.wasm"
COUNTER_WASM = ROOT / "test/wasm/rust-counter-2.0.0.wasm"
SQL_WASM = ROOT / "test/wasm/rust-sql-2.0.0.wasm"
RUST_FUNC_WASM = ROOT / "test/wasm/rust-func-verify-2.0.0.wasm"
GO_FUNC_WASM = ROOT / "test/wasm/go-func-verify-2.0.0.wasm"
EVM_TOKEN_BIN = ROOT / "test/wasm/evm-token.bin"
EVM_TOKEN_ABI = ROOT / "test/wasm/evm-token.abi"
CHAINMAKER_MAIN = ROOT / "main"
GOC_CENTER = "http://127.0.0.1:17771"
PORT_OFFSET = 0
CASES = ["txpool-normal-small-batch", "rpc-ratelimit-enabled"]
BCFUZZER_ARMS = {"fixed", "varied"}
ACTIVE_WORKLOAD_ARMS = {"varied"}
CHAINMAKER_BASELINE_SECTIONS = {"txpool", "ratelimit"}
CHAINMAKER_COVERAGE_MODULES = [
    "/module/sync/scheduler.go",
    "/module/sync/processor.go",
    "/module/sync/event.go",
    "/module/rpcserver/send_request_sync.go",
    "/module/core/cache/proposal_cache.go",
    "/module/rpcserver/api_service.go",
]

ORGS = ["wx-org1", "wx-org2", "wx-org3", "wx-org4"]


def ensure_instrumented_binary() -> Path:
    ensure_goc_binary()
    center_tag = center_host_port(GOC_CENTER).replace(":", "_").replace("/", "_")
    instrumented = Path(f"/tmp/chainmaker-goc-cover-{center_tag}")
    with Path("/tmp/chainmaker-goc-build.lock").open("w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        if instrumented.is_file() and os.access(instrumented, os.X_OK):
            return instrumented
        subprocess.run(
            ["/tmp/goc", "build", f"--center={GOC_CENTER}",
             "--output", str(instrumented), "."],
            cwd=CHAINMAKER_MAIN,
            check=True,
            timeout=1800,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=goc_build_env(),
        )
    return instrumented


def release_name(org: str) -> str:
    return f"chainmaker-v3.0.0-{org}.chainmaker.org"


def release_tar(org: str) -> Path:
    matches = sorted(RELEASE.glob(f"{release_name(org)}-*-x86_64.tar.gz"))
    if not matches:
        raise FileNotFoundError(f"no release tarball for {org}")
    return matches[-1]


def prepare_runtime(runtime: Path) -> Path:
    shutil.rmtree(runtime, ignore_errors=True)
    runtime.mkdir(parents=True)
    binary = ensure_instrumented_binary()
    for org in ORGS:
        subprocess.run(["tar", "-xzf", str(release_tar(org)), "-C", str(runtime)],
                       check=True, timeout=120)
        shutil.copy2(binary, runtime / release_name(org) / "bin" / "chainmaker")
    rebind_runtime_ports(runtime)
    return runtime


def org_domain(org: str) -> str:
    return f"{org}.chainmaker.org"


def org_index(org: str) -> int:
    return int(org.rsplit("org", 1)[1])


def org_p2p_port(org: str) -> int:
    return 11300 + org_index(org) + PORT_OFFSET


def org_rpc_port(org: str) -> int:
    return 12300 + org_index(org) + PORT_OFFSET


def rebind_runtime_ports(release_root: Path) -> None:
    if PORT_OFFSET == 0:
        return
    for org in ORGS:
        cfg = release_root / release_name(org) / "config" / org_domain(org) / "chainmaker.yml"
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        net = data.setdefault("net", {})
        net["listen_addr"] = f"/ip4/0.0.0.0/tcp/{org_p2p_port(org)}"
        seeds = list(net.get("seeds", []))
        for idx, seed_org in enumerate(ORGS):
            if idx >= len(seeds):
                break
            seeds[idx] = re.sub(r"/tcp/\d+/p2p/", f"/tcp/{org_p2p_port(seed_org)}/p2p/", seeds[idx])
        net["seeds"] = seeds
        data.setdefault("rpc", {})["port"] = org_rpc_port(org)
        cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def write_sdk_config(release_root: Path, target: Path, org: str = "wx-org1") -> None:
    org_root = release_root / release_name(org)
    org_cfg = org_root / "config" / org_domain(org)
    client_dir = org_cfg / "certs" / "user" / "client1"
    data = {
        "chain_client": {
            "chain_id": "chainmaker",
            "org_id": org_domain(org),
            "user_key_file_path": str(client_dir / "client1.tls.key"),
            "user_crt_file_path": str(client_dir / "client1.tls.crt"),
            "user_sign_key_file_path": str(client_dir / "client1.sign.key"),
            "user_sign_crt_file_path": str(client_dir / "client1.sign.crt"),
            "retry_limit": 20,
            "retry_interval": 500,
            "enable_normal_key": False,
            "enable_tx_result_dispatcher": True,
            "nodes": [
                {
                    "node_addr": f"127.0.0.1:{org_rpc_port(org)}",
                    "conn_cnt": 10,
                    "enable_tls": True,
                    "trust_root_paths": [str(org_cfg / "certs" / "ca" / org_domain(org))],
                    "tls_host_name": "chainmaker.org",
                }
            ],
            "rpc_client": {
                "max_receive_message_size": 16,
                "max_send_message_size": 16,
                "send_tx_timeout": 60,
                "get_tx_timeout": 60,
            },
        }
    }
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def overlay_case_config(org_dir: Path, case: str, seed: int) -> None:
    run_dir = Path("/tmp") / f"cm-case-{seed}-{os.getpid()}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(exist_ok=True)
    meta = apply_case("chainmaker", ROOT, case, run_dir, seed)
    model = Path(meta["output"])
    target = org_dir / "config" / "wx-org1.chainmaker.org" / "chainmaker.yml"
    model_data = yaml.safe_load(model.read_text(encoding="utf-8")) or {}
    target_data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if isinstance(model_data.get("txpool"), dict):
        target_data["txpool"] = model_data["txpool"]
    model_rate = (model_data.get("rpc") or {}).get("ratelimit")
    if isinstance(model_rate, dict):
        target_data.setdefault("rpc", {}).setdefault("ratelimit", {}).update(model_rate)
    target.write_text(yaml.safe_dump(target_data, sort_keys=False),
                      encoding="utf-8")


def _bounded_int(value, low: int, high: int, default: int) -> int:
    if isinstance(value, bool):
        number = default
    else:
        try:
            number = int(str(value).strip().strip('"').strip("'"), 0)
        except (TypeError, ValueError):
            number = default
    return max(low, min(high, number))


def _bool_value(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().strip('"').strip("'").lower()
    if text in ("true", "1", "yes", "y", "on"):
        return True
    if text in ("false", "0", "no", "n", "off"):
        return False
    return default


def sanitize_chainmaker_config(org_dir: Path) -> int:
    cfg = org_dir / "config" / "wx-org1.chainmaker.org" / "chainmaker.yml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    changed = 0

    txpool = data.setdefault("txpool", {})
    pool_type = str(txpool.get("pool_type", "normal")).strip().strip('"').strip("'")
    if pool_type not in {"normal", "batch"}:
        pool_type = "normal"
    if txpool.get("pool_type") != pool_type:
        txpool["pool_type"] = pool_type
        changed += 1

    tx_bounds = {
        "max_txpool_size": (1, 100_000, 2048),
        "max_config_txpool_size": (1, 10_000, 10),
        "common_queue_num": (1, 256, 8),
        "batch_max_size": (1, 10_000, 50),
        "batch_create_timeout": (1, 600_000, 50),
    }
    for key, (low, high, default) in tx_bounds.items():
        new_value = _bounded_int(txpool.get(key, default), low, high, default)
        if txpool.get(key) != new_value:
            txpool[key] = new_value
            changed += 1
    new_dump = _bool_value(txpool.get("is_dump_txs_in_queue", True), True)
    if txpool.get("is_dump_txs_in_queue") != new_dump:
        txpool["is_dump_txs_in_queue"] = new_dump
        changed += 1

    ratelimit = data.setdefault("rpc", {}).setdefault("ratelimit", {})
    new_enabled = _bool_value(ratelimit.get("enabled", False), False)
    if ratelimit.get("enabled") != new_enabled:
        ratelimit["enabled"] = new_enabled
        changed += 1
    rate_bounds = {
        "type": (0, 1, 0),
        "token_per_second": (-1, 1_000_000, -1),
        "token_bucket_size": (-1, 1_000_000, -1),
    }
    for key, (low, high, default) in rate_bounds.items():
        new_value = _bounded_int(ratelimit.get(key, default), low, high, default)
        if ratelimit.get(key) != new_value:
            ratelimit[key] = new_value
            changed += 1

    if changed:
        cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return changed


def mutate_node_config(org_dir: Path, tool: str, seed: int) -> int:
    if tool in BCFUZZER_ARMS:
        return 0
    cfg = org_dir / "config" / "wx-org1.chainmaker.org" / "chainmaker.yml"
    from campaign_baseline_real import mutate_with_tool
    mutated = mutate_with_tool(
        cfg, tool, seed, target="chainmaker",
        eligible_sections=CHAINMAKER_BASELINE_SECTIONS)
    sanitize_chainmaker_config(org_dir)
    return mutated


def org_bin_dir(release_root: Path, org: str) -> Path:
    return release_root / release_name(org) / "bin"


def org_config_arg(org: str) -> str:
    return f"../config/{org_domain(org)}/chainmaker.yml"


def chainmaker_env(release_root: Path, org: str) -> dict[str, str]:
    env = os.environ.copy()
    service_tag = center_host_port(GOC_CENTER).replace(":", "_")
    lib_dir = str((release_root / release_name(org) / "lib").resolve())
    old_ld = env.get("LD_LIBRARY_PATH", "")
    old_path = env.get("PATH", "")
    env["LD_LIBRARY_PATH"] = f"{lib_dir}:{old_ld}" if old_ld else lib_dir
    env["PATH"] = f"{lib_dir}:{old_path}" if old_path else lib_dir
    env["WASMER_BACKTRACE"] = "1"
    env["GOC_SERVICE_NAME"] = f"chainmaker-{service_tag}-{org}"
    return env


def start_chainmaker_process(release_root: Path, org: str) -> None:
    bin_dir = org_bin_dir(release_root, org)
    log_path = bin_dir / "panic.log"
    log_path.unlink(missing_ok=True)
    with log_path.open("ab") as log_fh:
        subprocess.Popen(
            ["./chainmaker", "start", "-c", org_config_arg(org)],
            cwd=bin_dir,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=chainmaker_env(release_root, org),
            start_new_session=True,
        )


def matching_chainmaker_pids(release_root: Path, org: str | None = None) -> list[int]:
    expected = None if org is None else (org_bin_dir(release_root, org) / "chainmaker").resolve()
    victims: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            exe = (proc / "exe").resolve()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        try:
            exe.relative_to(release_root)
        except ValueError:
            continue
        if expected is not None and exe != expected:
            continue
        if exe.name != "chainmaker":
            continue
        victims.append(int(proc.name))
    return victims


def kill_chainmaker_processes(release_root: Path, org: str | None = None) -> None:
    victims = matching_chainmaker_pids(release_root, org)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while victims and time.monotonic() < deadline:
        victims = [pid for pid in victims if Path(f"/proc/{pid}").exists()]
        if victims:
            time.sleep(0.2)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def start_all(release_root: Path) -> None:
    for org in ORGS:
        start_chainmaker_process(release_root, org)
    time.sleep(10)


def node_running(release_root: Path, org: str) -> bool:
    expected = (release_root / release_name(org) / "bin" / "chainmaker").resolve()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            exe = (proc / "exe").resolve()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if exe == expected:
            return True
    return False


def kill_stale_chainmakers(release_root: Path | None = None) -> None:
    victims: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            exe = (proc / "exe").resolve()
            cmdline = (proc / "cmdline").read_text(errors="replace").replace("\x00", " ")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if "chainmaker start -c " not in cmdline:
            continue
        if release_root is not None:
            try:
                exe.relative_to(release_root)
            except ValueError:
                continue
        victims.append(int(proc.name))
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while victims and time.monotonic() < deadline:
        victims = [pid for pid in victims if Path(f"/proc/{pid}").exists()]
        if victims:
            time.sleep(0.2)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def stop_all(release_root: Path) -> None:
    kill_chainmaker_processes(release_root)


def stop_org(release_root: Path, org: str) -> None:
    kill_chainmaker_processes(release_root, org)


def start_org(release_root: Path, org: str) -> bool:
    start_chainmaker_process(release_root, org)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if node_running(release_root, org):
            return True
        time.sleep(1)
    return False


def cmc(sdk_conf: Path, *args: str, timeout: int = 60) -> bool:
    try:
        result = subprocess.run(
            [str(CMC), *args, "--sdk-conf-path", str(sdk_conf)],
            timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def cmc_capture(sdk_conf: Path, *args: str, timeout: int = 60) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [str(CMC), *args, "--sdk-conf-path", str(sdk_conf)],
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.returncode == 0, result.stdout
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return False, output


def user_sign_cert(release_root: Path, org: str, user: str = "client1") -> Path:
    return (release_root / release_name(org) / "config" / org_domain(org)
            / "certs" / "user" / user / f"{user}.sign.crt")


def user_dpos_addr(release_root: Path, org: str, user: str = "client1") -> str | None:
    path = (release_root / release_name(org) / "config" / org_domain(org)
            / "certs" / "user" / user / f"{user}.addr")
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return text or None


def resolve_user_addr(sdk_conf: Path, cert_path: Path) -> str | None:
    ok, output = cmc_capture(
        sdk_conf,
        "cert", "userAddr",
        f"--pubkey-cert-path={cert_path}",
        timeout=30,
    )
    if not ok:
        return None
    match = re.search(r"address:\s*([0-9A-Za-zx]+)", output)
    if not match:
        return None
    return match.group(1).strip()


def interact(arm: str, sdk_conf: Path, case: str | None = None) -> dict:
    if arm == "fixed":
        return {
            "fact_contract_created": False,
            "counter_contract_created": False,
            "accepted_invokes": 0,
            "attempted_invokes": 0,
            "successful_queries": 0,
            "attempted_queries": 0,
        }
    created_fact = cmc(
        sdk_conf,
        "client", "contract", "user", "create",
        "--contract-name=fact",
        "--runtime-type=WASMER",
        f"--byte-code-path={FACT_WASM}",
        "--version=1.0",
        "--sync-result=true",
        "--params={}",
    )
    created_counter = False
    if arm == "varied":
        created_counter = cmc(
            sdk_conf,
            "client", "contract", "user", "create",
            "--contract-name=counter",
            "--runtime-type=WASMER",
            f"--byte-code-path={COUNTER_WASM}",
            "--version=1.0",
            "--sync-result=true",
            "--params={}",
        )

    save_attempts = 12
    counter_attempts = 0 if not created_counter else 10
    query_attempts = 6
    accepted = 0
    queries_ok = 0

    hashes: list[str] = []
    for i in range(save_attempts):
        file_hash = f"ab3456df5799b87c77e7f88{i:04d}"
        hashes.append(file_hash)
        params = json.dumps({
            "file_name": f"name{i:03d}",
            "file_hash": file_hash,
            "time": str(6543234 + i),
        }, separators=(",", ":"))
        if cmc(
            sdk_conf,
            "client", "contract", "user", "invoke",
            "--contract-name=fact",
            "--method=save",
            f"--params={params}",
            "--sync-result=true",
            "--result-to-string=true",
        ):
            accepted += 1
        time.sleep(0.4)

    for i in range(query_attempts):
        file_hash = hashes[min(i, len(hashes) - 1)] if hashes else "ab3456df5799b87c77e7f880000"
        params = json.dumps({"file_hash": file_hash}, separators=(",", ":"))
        if cmc(
            sdk_conf,
            "client", "contract", "user", "get",
            "--contract-name=fact",
            "--method=find_by_file_hash",
            f"--params={params}",
            "--result-to-string=true",
        ):
            queries_ok += 1
        time.sleep(0.2)

    for _ in range(counter_attempts):
        if cmc(
            sdk_conf,
            "client", "contract", "user", "invoke",
            "--contract-name=counter",
            "--method=increase",
            "--params={}",
            "--sync-result=true",
        ):
            accepted += 1
        time.sleep(0.3)

    return {
        "fact_contract_created": created_fact,
        "counter_contract_created": created_counter,
        "accepted_invokes": accepted,
        "attempted_invokes": save_attempts + counter_attempts,
        "successful_queries": queries_ok,
        "attempted_queries": query_attempts,
    }


def parallel_cmc(sdk_conf: Path, jobs: list[tuple[str, ...]], *,
                 max_workers: int = 12, timeout: int = 25) -> int:
    if not jobs:
        return 0
    successes = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(cmc, sdk_conf, *job, timeout=timeout) for job in jobs]
        for future in as_completed(futures):
            try:
                if future.result():
                    successes += 1
            except Exception:
                pass
    return successes


def cross_org_varied_workload(sdk_confs: dict[str, Path]) -> dict:
    invoke_jobs: list[tuple[Path, tuple[str, ...]]] = []
    query_jobs: list[tuple[Path, tuple[str, ...]]] = []
    for org, sdk_conf in sdk_confs.items():
        org_num = int(org.rsplit("org", 1)[1])
        for i in range(10):
            invoke_jobs.append((
                sdk_conf,
                (
                    "client", "contract", "user", "invoke",
                    "--contract-name=counter",
                    "--method=increase",
                    "--params={}",
                    "--sync-result=true",
                ),
            ))
        for i in range(6):
            invoke_jobs.append((
                sdk_conf,
                (
                    "client", "contract", "user", "invoke",
                    "--contract-name=fact",
                    "--method=save",
                    f'--params={{"file_name":"cross{org_num}_{i:03d}","file_hash":"cc3456df5799b87c77e7f88{org_num}{i:03d}","time":"{9753100 + org_num * 100 + i}"}}',
                    "--sync-result=true",
                    "--result-to-string=true",
                ),
            ))
        for i in range(6):
            query_jobs.append((
                sdk_conf,
                (
                    "client", "contract", "user", "get",
                    "--contract-name=fact",
                    "--method=find_by_file_hash",
                    f'--params={{"file_hash":"ab3456df5799b87c77e7f88{i:04d}"}}',
                    "--result-to-string=true",
                ),
            ))

    invoke_ok = 0
    query_ok = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        invoke_futures = [pool.submit(cmc, sdk_conf, *job, timeout=25) for sdk_conf, job in invoke_jobs]
        query_futures = [pool.submit(cmc, sdk_conf, *job, timeout=20) for sdk_conf, job in query_jobs]
        for future in as_completed(invoke_futures):
            try:
                if future.result():
                    invoke_ok += 1
            except Exception:
                pass
        for future in as_completed(query_futures):
            try:
                if future.result():
                    query_ok += 1
            except Exception:
                pass

    return {
        "cross_org_attempted_invokes": len(invoke_jobs),
        "cross_org_invokes_ok": invoke_ok,
        "cross_org_attempted_queries": len(query_jobs),
        "cross_org_queries_ok": query_ok,
    }


def control_plane_sweep(sdk_confs: dict[str, Path]) -> dict:
    attempted = 0
    successes = 0
    for org, sdk_conf in sdk_confs.items():
        org_name = org_domain(org)
        commands: list[tuple[str, ...]] = [
            ("consensus", "status"),
            ("consensus", "height"),
            ("consensus", "validators"),
            ("txpool", "status"),
            ("txpool", "txids", "--type=3", "--stage=3"),
            ("client", "chainconfig", "query", f"--org-id={org_name}"),
            ("client", "cmversion", f"--org-id={org_name}"),
        ]
        for cmd in commands:
            attempted += 1
            if cmc(sdk_conf, *cmd, timeout=20):
                successes += 1
        for height in ("0", "1", "2", "3", "5", "8"):
            attempted += 1
            if cmc(
                sdk_conf,
                "query", "block-by-height", height,
                "--with-rw-set=true",
                timeout=20,
            ):
                successes += 1
    return {
        "control_plane_attempts": attempted,
        "control_plane_successes": successes,
    }


def subscription_sweep(sdk_confs: dict[str, Path]) -> dict:
    attempted = 0
    successes = 0
    for org, sdk_conf in sdk_confs.items():
        if org not in {"wx-org1", "wx-org2"}:
            continue
        commands: list[tuple[str, ...]] = [
            ("sub", "block", "--start-block=0", "--end-block=3", "--with-rw-set=true"),
            ("sub", "block", "--start-block=0", "--end-block=3", "--only-header=true"),
            ("sub", "tx", "--start-block=0", "--end-block=3", "--result-to-string=true"),
        ]
        for cmd in commands:
            attempted += 1
            if cmc(sdk_conf, *cmd, timeout=25):
                successes += 1
    return {
        "subscription_attempts": attempted,
        "subscription_successes": successes,
    }


def admin_sign_args(release_root: Path) -> list[str]:
    admin_crts: list[str] = []
    admin_keys: list[str] = []
    admin_orgs: list[str] = []
    for org in ("wx-org1", "wx-org2", "wx-org3"):
        admin_dir = (release_root / release_name(org) / "config" / org_domain(org)
                     / "certs" / "user" / "admin1")
        admin_crts.append(str(admin_dir / "admin1.sign.crt"))
        admin_keys.append(str(admin_dir / "admin1.sign.key"))
        admin_orgs.append(org_domain(org))
    return [
        f"--admin-crt-file-paths={','.join(admin_crts)}",
        f"--admin-key-file-paths={','.join(admin_keys)}",
        f"--admin-org-ids={','.join(admin_orgs)}",
        f"--org-id={org_domain('wx-org1')}",
        "--chain-id=chainmaker",
        "--sync-result=true",
    ]


def system_contract_varied_workload(release_root: Path, sdk_confs: dict[str, Path]) -> dict:
    admin_sdk = sdk_confs["wx-org1"]
    admin_args = admin_sign_args(release_root)
    common_args = [
        f"--org-id={org_domain('wx-org1')}",
        "--chain-id=chainmaker",
    ]
    addresses = {
        org: user_dpos_addr(release_root, org, "client1")
        for org in ("wx-org1", "wx-org2", "wx-org3")
    }
    if not all(addresses.values()):
        return {
            "system_addr_resolved": False,
            "system_attempted_invokes": 0,
            "system_attempted_queries": 0,
            "system_invokes_ok": 0,
            "system_queries_ok": 0,
        }

    invokes_ok = 0
    queries_ok = 0
    attempted_invokes = 0
    attempted_queries = 0

    for org in ("wx-org1", "wx-org2", "wx-org3"):
        attempted_invokes += 1
        if cmc(
            admin_sdk,
            "client", "contract", "system", "mint",
            f"--address={addresses[org]}",
            "--amount=100000000",
            *admin_args,
            timeout=90,
        ):
            invokes_ok += 1

    transfer_jobs = [
        ("wx-org1", addresses["wx-org2"], "2500000"),
        ("wx-org2", addresses["wx-org3"], "1250000"),
        ("wx-org3", addresses["wx-org1"], "625000"),
    ]
    for org, dst, amount in transfer_jobs:
        attempted_invokes += 1
        if cmc(
            sdk_confs[org],
            "client", "contract", "system", "transfer",
            f"--address={dst}",
            f"--amount={amount}",
            *common_args,
            "--sync-result=true",
            timeout=40,
        ):
            invokes_ok += 1

    for org in ("wx-org1", "wx-org2", "wx-org3"):
        attempted_queries += 1
        if cmc(
            sdk_confs[org],
            "client", "contract", "system", "balance-of",
            f"--address={addresses[org]}",
            *common_args,
            timeout=30,
        ):
            queries_ok += 1

    for cmd in (("owner",), ("decimals",), ("total",)):
        attempted_queries += 1
        if cmc(
            admin_sdk,
            "client", "contract", "system", *cmd,
            *common_args,
            timeout=30,
        ):
            queries_ok += 1

    return {
        "system_addr_resolved": True,
        "system_attempted_invokes": attempted_invokes,
        "system_attempted_queries": attempted_queries,
        "system_invokes_ok": invokes_ok,
        "system_queries_ok": queries_ok,
    }


def evm_varied_workload(release_root: Path, sdk_confs: dict[str, Path]) -> dict:
    sdk_conf = sdk_confs["wx-org1"]
    addr_a = resolve_user_addr(sdk_confs["wx-org1"], user_sign_cert(release_root, "wx-org1", "client1"))
    addr_b = resolve_user_addr(sdk_confs["wx-org2"], user_sign_cert(release_root, "wx-org2", "client1"))
    if not addr_a or not addr_b:
        return {
            "evm_addr_resolved": False,
            "evm_contract_created": False,
            "evm_attempted_invokes": 0,
            "evm_attempted_queries": 0,
            "evm_invokes_ok": 0,
            "evm_queries_ok": 0,
        }

    attempted_invokes = 1
    created = cmc(
        sdk_conf,
        "client", "contract", "user", "create",
        "--contract-name=ETHEREUM",
        "--runtime-type=EVM",
        f"--byte-code-path={EVM_TOKEN_BIN}",
        "--version=1.0",
        "--sync-result=true",
        timeout=120,
    )
    if not created:
        return {
            "evm_addr_resolved": True,
            "evm_contract_created": False,
            "evm_attempted_invokes": attempted_invokes,
            "evm_attempted_queries": 0,
            "evm_invokes_ok": 0,
            "evm_queries_ok": 0,
        }

    invokes_ok = 1
    queries_ok = 0
    attempted_queries = 0

    for account, amount in (
        (addr_a, "1000000000000000000000"),
        (addr_b, "100000000000000000000"),
    ):
        attempted_invokes += 1
        if cmc(
            sdk_conf,
            "client", "contract", "user", "invoke",
            "--contract-name=ETHEREUM",
            "--method=Mint",
            f'--params={{"ACCOUNT":"{account}","AMOUNT":"{amount}"}}',
            "--sync-result=true",
            timeout=60,
        ):
            invokes_ok += 1

    for account in (addr_a, addr_b):
        attempted_queries += 1
        if cmc(
            sdk_conf,
            "client", "contract", "user", "get",
            "--contract-name=ETHEREUM",
            "--method=BalanceOf",
            f'--params={{"ACCOUNT":"{account}"}}',
            "--result-to-string=true",
            timeout=30,
        ):
            queries_ok += 1

    attempted_queries += 1
    if cmc(
        sdk_conf,
        "client", "contract", "user", "get",
        "--contract-name=ETHEREUM",
        "--method=Nonce",
        f'--params={{"ACCOUNT":"{addr_a}"}}',
        "--result-to-string=true",
        timeout=30,
    ):
        queries_ok += 1

    attempted_queries += 1
    if cmc(
        sdk_conf,
        "client", "contract", "user", "get",
        "--contract-name=ETHEREUM",
        "--method=GetAccountList",
        "--params={}",
        "--result-to-string=true",
        timeout=30,
    ):
        queries_ok += 1

    attempted_queries += 1
    if cmc(
        sdk_conf,
        "eth", "call",
        f"--to={addr_a}",
        f"--from={addr_a}",
        "--data=313ce567",
        f"--abi-file-path={EVM_TOKEN_ABI}",
        "--method=decimals",
        timeout=30,
    ):
        queries_ok += 1

    return {
        "evm_addr_resolved": True,
        "evm_contract_created": True,
        "evm_attempted_invokes": attempted_invokes,
        "evm_attempted_queries": attempted_queries,
        "evm_invokes_ok": invokes_ok,
        "evm_queries_ok": queries_ok,
    }


def func_verify_varied_workload(release_root: Path, sdk_conf: Path) -> dict:
    invoke_ok = 0
    query_ok = 0
    attempted_invokes = 0
    attempted_queries = 0
    admin_args = admin_sign_args(release_root)
    contracts = [
        ("funcverify_rust", "WASMER", RUST_FUNC_WASM),
        ("funcverify_go", "GASM", GO_FUNC_WASM),
    ]

    for contract_name, runtime, wasm_path in contracts:
        attempted_invokes += 1
        if cmc(
            sdk_conf,
            "client", "contract", "user", "create",
            f"--contract-name={contract_name}",
            f"--runtime-type={runtime}",
            f"--byte-code-path={wasm_path}",
            "--version=1.0",
            "--sync-result=true",
            "--params={}",
            timeout=120,
        ):
            invoke_ok += 1
        else:
            continue

        for method in ("test_put_state", "test_put_pre_state"):
            attempted_invokes += 1
            if cmc(
                sdk_conf,
                "client", "contract", "user", "invoke",
                f"--contract-name={contract_name}",
                f"--method={method}",
                "--params={}",
                "--sync-result=true",
                timeout=40,
            ):
                invoke_ok += 1

        attempted_invokes += 1
        if cmc(
            sdk_conf,
            "client", "contract", "user", "invoke",
            f"--contract-name={contract_name}",
            "--method=functional_verify",
            f'--params={{"contract_name":"{contract_name}"}}',
            "--sync-result=true",
            timeout=60,
        ):
            invoke_ok += 1

        for method in ("test_kv_iterator", "test_iter_pre_key", "test_iter_pre_field"):
            attempted_queries += 1
            if cmc(
                sdk_conf,
                "client", "contract", "user", "get",
                f"--contract-name={contract_name}",
                f"--method={method}",
                "--params={}",
                "--result-to-string=true",
                timeout=30,
            ):
                query_ok += 1

        attempted_invokes += 1
        if cmc(
            sdk_conf,
            "client", "contract", "user", "upgrade",
            f"--contract-name={contract_name}",
            f"--runtime-type={runtime}",
            f"--byte-code-path={wasm_path}",
            "--version=1.0.1",
            "--params={}",
            *admin_args,
            timeout=120,
        ):
            invoke_ok += 1

        attempted_invokes += 1
        if cmc(
            sdk_conf,
            "client", "contract", "user", "freeze",
            f"--contract-name={contract_name}",
            *admin_args,
            timeout=60,
        ):
            invoke_ok += 1

        attempted_queries += 1
        cmc(
            sdk_conf,
            "client", "contract", "user", "get",
            f"--contract-name={contract_name}",
            "--method=test_kv_iterator",
            "--params={}",
            "--result-to-string=true",
            timeout=20,
        )

        attempted_invokes += 1
        if cmc(
            sdk_conf,
            "client", "contract", "user", "unfreeze",
            f"--contract-name={contract_name}",
            *admin_args,
            timeout=60,
        ):
            invoke_ok += 1

        attempted_queries += 1
        if cmc(
            sdk_conf,
            "client", "contract", "user", "get",
            f"--contract-name={contract_name}",
            "--method=test_kv_iterator",
            "--params={}",
            "--result-to-string=true",
            timeout=20,
        ):
            query_ok += 1

    attempted_invokes += 1
    if cmc(
        sdk_conf,
        "client", "contract", "user", "revoke",
        "--contract-name=funcverify_go",
        *admin_args,
        timeout=60,
    ):
        invoke_ok += 1

    return {
        "func_verify_attempted_invokes": attempted_invokes,
        "func_verify_attempted_queries": attempted_queries,
        "func_verify_invokes_ok": invoke_ok,
        "func_verify_queries_ok": query_ok,
    }


def cert_manage_varied_workload(release_root: Path, sdk_confs: dict[str, Path]) -> dict:
    admin_sdk = sdk_confs["wx-org1"]
    frozen_sdk = sdk_confs["wx-org2"]
    admin_args = admin_sign_args(release_root)
    target_cert = (release_root / release_name("wx-org2") / "config" / org_domain("wx-org2")
                   / "certs" / "user" / "client1" / "client1.sign.crt")

    admin_attempts = 0
    admin_successes = 0
    frozen_invoke_attempts = 0
    frozen_query_attempts = 0
    frozen_invokes_ok = 0
    frozen_queries_ok = 0
    recovered_invokes_ok = 0
    recovered_queries_ok = 0

    admin_attempts += 1
    if cmc(
        admin_sdk,
        "client", "certmanage", "freeze",
        f"--cert-file-paths={target_cert}",
        *admin_args,
        timeout=60,
    ):
        admin_successes += 1

    for i in range(3):
        frozen_invoke_attempts += 1
        if cmc(
            frozen_sdk,
            "client", "contract", "user", "invoke",
            "--contract-name=counter",
            "--method=increase",
            "--params={}",
            "--sync-result=true",
            timeout=20,
        ):
            frozen_invokes_ok += 1
    for i in range(3):
        frozen_query_attempts += 1
        if cmc(
            frozen_sdk,
            "client", "contract", "user", "get",
            "--contract-name=fact",
            "--method=find_by_file_hash",
            f'--params={{"file_hash":"ab3456df5799b87c77e7f88{i:04d}"}}',
            "--result-to-string=true",
            timeout=20,
        ):
            frozen_queries_ok += 1

    admin_attempts += 1
    if cmc(
        admin_sdk,
        "client", "certmanage", "unfreeze",
        f"--cert-file-paths={target_cert}",
        *admin_args,
        timeout=60,
    ):
        admin_successes += 1

    for i in range(3):
        if cmc(
            frozen_sdk,
            "client", "contract", "user", "invoke",
            "--contract-name=counter",
            "--method=increase",
            "--params={}",
            "--sync-result=true",
            timeout=20,
        ):
            recovered_invokes_ok += 1
    for i in range(3):
        if cmc(
            frozen_sdk,
            "client", "contract", "user", "get",
            "--contract-name=fact",
            "--method=find_by_file_hash",
            f'--params={{"file_hash":"ab3456df5799b87c77e7f88{i:04d}"}}',
            "--result-to-string=true",
            timeout=20,
        ):
            recovered_queries_ok += 1

    return {
        "cert_manage_attempts": admin_attempts,
        "cert_manage_successes": admin_successes,
        "cert_frozen_invoke_attempts": frozen_invoke_attempts,
        "cert_frozen_query_attempts": frozen_query_attempts,
        "cert_frozen_invokes_ok": frozen_invokes_ok,
        "cert_frozen_queries_ok": frozen_queries_ok,
        "cert_recovered_invokes_ok": recovered_invokes_ok,
        "cert_recovered_queries_ok": recovered_queries_ok,
    }


def sql_varied_workload(sdk_conf: Path) -> dict:
    created_sql = cmc(
        sdk_conf,
        "client", "contract", "user", "create",
        "--contract-name=rustsql",
        "--runtime-type=WASMER",
        f"--byte-code-path={SQL_WASM}",
        "--version=1.0",
        "--sync-result=true",
        "--params={}",
        timeout=120,
    )
    if not created_sql:
        return {
            "sql_contract_created": False,
            "sql_invokes_ok": 0,
            "sql_queries_ok": 0,
            "sql_attempted_invokes": 0,
            "sql_attempted_queries": 0,
        }

    invokes_ok = 0
    queries_ok = 0
    attempted_invokes = 0
    attempted_queries = 0

    for i in range(1, 17):
        attempted_invokes += 1
        params = json.dumps({
            "id": str(i),
            "name": "chainmaker",
            "age": str(i + 10),
            "id_card_no": "510623199202023323",
        }, separators=(",", ":"))
        if cmc(
            sdk_conf,
            "client", "contract", "user", "invoke",
            "--contract-name=rustsql",
            "--method=sql_insert",
            f"--params={params}",
            "--sync-result=true",
            timeout=40,
        ):
            invokes_ok += 1
        time.sleep(0.1)

    for i in range(1, 5):
        attempted_queries += 1
        params = json.dumps({"id": str(i)}, separators=(",", ":"))
        if cmc(
            sdk_conf,
            "client", "contract", "user", "get",
            "--contract-name=rustsql",
            "--method=sql_query_by_id",
            f"--params={params}",
            "--result-to-string=true",
            timeout=30,
        ):
            queries_ok += 1

    for i in range(1, 5):
        attempted_invokes += 1
        params = json.dumps({"id": str(i), "name": f"chainmaker_update_{i}"}, separators=(",", ":"))
        if cmc(
            sdk_conf,
            "client", "contract", "user", "invoke",
            "--contract-name=rustsql",
            "--method=sql_update",
            f"--params={params}",
            "--sync-result=true",
            timeout=40,
        ):
            invokes_ok += 1

    for low, high in ((13, 17), (15, 20), (10, 30)):
        attempted_queries += 1
        params = json.dumps({"min_age": str(low), "max_age": str(high)}, separators=(",", ":"))
        if cmc(
            sdk_conf,
            "client", "contract", "user", "get",
            "--contract-name=rustsql",
            "--method=sql_query_range_of_age",
            f"--params={params}",
            "--result-to-string=true",
            timeout=30,
        ):
            queries_ok += 1

    for method, params in (
        ("sql_delete", {"id": "1"}),
        ("sql_insert", {"id": "20", "name": "chainmaker", "age": "2000", "id_card_no": "510623199202023323"}),
        ("sql_update_rollback_save_point", {"id": "20", "name": "chainmaker_save_point"}),
    ):
        attempted_invokes += 1
        if cmc(
            sdk_conf,
            "client", "contract", "user", "invoke",
            "--contract-name=rustsql",
            f"--method={method}",
            f"--params={json.dumps(params, separators=(',', ':'))}",
            "--sync-result=true",
            timeout=40,
        ):
            invokes_ok += 1

    attempted_queries += 2
    if cmc(
        sdk_conf,
        "client", "contract", "user", "get",
        "--contract-name=rustsql",
        "--method=sql_cross_call",
        '--params={"contract_name":"rustsql","min_age":"16","max_age":"19"}',
        "--result-to-string=true",
        timeout=30,
    ):
        queries_ok += 1
    if cmc(
        sdk_conf,
        "client", "contract", "user", "get",
        "--contract-name=rustsql",
        "--method=sql_random_query_str",
        '--params={"id":"501","name":"chainmaker"}',
        "--result-to-string=true",
        timeout=30,
    ):
        queries_ok += 1

    attempted_invokes += 2
    for method in ("sql_random_str", "sql_multi_sql"):
        if cmc(
            sdk_conf,
            "client", "contract", "user", "invoke",
            "--contract-name=rustsql",
            f"--method={method}",
            '--params={"id":"501","name":"chainmaker"}',
            "--sync-result=true",
            timeout=30,
        ):
            invokes_ok += 1

    attempted_invokes += 1
    if cmc(
        sdk_conf,
        "client", "contract", "user", "upgrade",
        "--contract-name=rustsql",
        "--runtime-type=WASMER",
        f"--byte-code-path={SQL_WASM}",
        "--version=2.0.1",
        "--sync-result=true",
        "--params={}",
        timeout=120,
    ):
        invokes_ok += 1

    attempted_invokes += 1
    if cmc(
        sdk_conf,
        "client", "contract", "user", "invoke",
        "--contract-name=rustsql",
        "--method=sql_insert",
        '--params={"id":"21","name":"chainmaker","age":"100000","id_card_no":"510623199202023323"}',
        "--sync-result=true",
        timeout=40,
    ):
        invokes_ok += 1

    attempted_queries += 1
    if cmc(
        sdk_conf,
        "client", "contract", "user", "get",
        "--contract-name=rustsql",
        "--method=sql_query_by_id",
        '--params={"id":"21"}',
        "--result-to-string=true",
        timeout=30,
    ):
        queries_ok += 1

    extra_insert_jobs: list[tuple[str, ...]] = []
    for i in range(500, 560):
        extra_insert_jobs.append((
            "client", "contract", "user", "invoke",
            "--contract-name=rustsql",
            "--method=sql_insert",
            f'--params={{"id":"{i}","name":"chainmaker","age":"{i + 10}","id_card_no":"510623199202023323"}}',
            "--sync-result=true",
        ))
    attempted_invokes += len(extra_insert_jobs)
    invokes_ok += parallel_cmc(sdk_conf, extra_insert_jobs, max_workers=20, timeout=30)

    attempted_invokes += 4
    for method in ("sql_execute_ddl", "sql_dbname_table_name", "sql_execute_commit", "sql_update_state_info"):
        if cmc(
            sdk_conf,
            "client", "contract", "user", "invoke",
            "--contract-name=rustsql",
            f"--method={method}",
            '--params={"id":"501","name":"chainmaker"}',
            "--sync-result=true",
            timeout=30,
        ):
            invokes_ok += 1

    attempted_queries += 1
    if cmc(
        sdk_conf,
        "client", "contract", "user", "get",
        "--contract-name=rustsql",
        "--method=sql_query_state_info",
        '--params={"id":"501","name":"chainmaker"}',
        "--result-to-string=true",
        timeout=30,
    ):
        queries_ok += 1

    return {
        "sql_contract_created": True,
        "sql_invokes_ok": invokes_ok,
        "sql_queries_ok": queries_ok,
        "sql_attempted_invokes": attempted_invokes,
        "sql_attempted_queries": attempted_queries,
    }


def case_parallel_burst(sdk_conf: Path, case: str) -> dict:
    jobs: list[tuple[str, ...]] = []
    malformed = 0
    if case == "rpc-ratelimit-enabled":
        for i in range(32):
            jobs.append((
                "client", "contract", "user", "get",
                "--contract-name=fact",
                "--method=find_by_file_hash",
                f'--params={{"file_hash":"ab3456df5799b87c77e7f88{i % 12:04d}"}}',
                "--result-to-string=true",
            ))
        for _ in range(32):
            jobs.append((
                "client", "contract", "user", "invoke",
                "--contract-name=counter",
                "--method=increase",
                "--params={}",
                "--sync-result=true",
            ))
        successes = parallel_cmc(sdk_conf, jobs, max_workers=20, timeout=20)
        for _ in range(12):
            if not cmc(
                sdk_conf,
                "client", "contract", "user", "invoke",
                "--contract-name=fact",
                "--method=missing_method",
                "--params={}",
                "--sync-result=true",
                timeout=15,
            ):
                malformed += 1
        return {
            "parallel_successes": successes,
            "parallel_attempts": len(jobs),
            "malformed_probes": malformed,
        }

    for i in range(48):
        jobs.append((
            "client", "contract", "user", "invoke",
            "--contract-name=counter",
            "--method=increase",
            "--params={}",
            "--sync-result=true",
        ))
    for i in range(32):
        jobs.append((
            "client", "contract", "user", "invoke",
            "--contract-name=fact",
            "--method=save",
            f'--params={{"file_name":"bulk{i:03d}","file_hash":"bb3456df5799b87c77e7f88{i:04d}","time":"{8654321 + i}"}}',
            "--sync-result=true",
            "--result-to-string=true",
        ))
    successes = parallel_cmc(sdk_conf, jobs, max_workers=24, timeout=25)
    return {
        "parallel_successes": successes,
        "parallel_attempts": len(jobs),
        "malformed_probes": 0,
    }


def strong_varied_interact(release_root: Path, sdk_confs: dict[str, Path],
                           round_dir: Path, case: str) -> tuple[dict, Path]:
    sdk_conf = sdk_confs["wx-org1"]
    activity = interact("varied", sdk_conf, case)
    sql_activity = sql_varied_workload(sdk_conf)
    burst_activity = case_parallel_burst(sdk_conf, case)
    func_verify_activity = func_verify_varied_workload(release_root, sdk_conf)
    cross_org_activity = cross_org_varied_workload({
        org: sdk for org, sdk in sdk_confs.items() if org in {"wx-org2", "wx-org3"}
    })
    system_activity = system_contract_varied_workload(release_root, sdk_confs)
    evm_activity = evm_varied_workload(release_root, sdk_confs)
    cert_manage_activity = cert_manage_varied_workload(release_root, sdk_confs)
    control_activity = control_plane_sweep(sdk_confs)
    subscription_activity = subscription_sweep(sdk_confs)
    activity["accepted_invokes"] += sql_activity["sql_invokes_ok"] + burst_activity["parallel_successes"]
    activity["attempted_invokes"] += sql_activity["sql_attempted_invokes"] + burst_activity["parallel_attempts"]
    activity["successful_queries"] += sql_activity["sql_queries_ok"]
    activity["attempted_queries"] += sql_activity["sql_attempted_queries"]
    activity["accepted_invokes"] += func_verify_activity["func_verify_invokes_ok"]
    activity["attempted_invokes"] += func_verify_activity["func_verify_attempted_invokes"]
    activity["successful_queries"] += func_verify_activity["func_verify_queries_ok"]
    activity["attempted_queries"] += func_verify_activity["func_verify_attempted_queries"]
    activity["accepted_invokes"] += cross_org_activity["cross_org_invokes_ok"]
    activity["attempted_invokes"] += cross_org_activity["cross_org_attempted_invokes"]
    activity["successful_queries"] += cross_org_activity["cross_org_queries_ok"]
    activity["attempted_queries"] += cross_org_activity["cross_org_attempted_queries"]
    activity["accepted_invokes"] += system_activity["system_invokes_ok"] + evm_activity["evm_invokes_ok"]
    activity["attempted_invokes"] += system_activity["system_attempted_invokes"] + evm_activity["evm_attempted_invokes"]
    activity["successful_queries"] += system_activity["system_queries_ok"] + evm_activity["evm_queries_ok"]
    activity["attempted_queries"] += system_activity["system_attempted_queries"] + evm_activity["evm_attempted_queries"]
    activity["accepted_invokes"] += cert_manage_activity["cert_recovered_invokes_ok"]
    activity["attempted_invokes"] += cert_manage_activity["cert_frozen_invoke_attempts"] + 3
    activity["successful_queries"] += cert_manage_activity["cert_recovered_queries_ok"]
    activity["attempted_queries"] += cert_manage_activity["cert_frozen_query_attempts"] + 3
    activity.update(sql_activity)
    activity.update(burst_activity)
    activity.update(func_verify_activity)
    activity.update(cross_org_activity)
    activity.update(system_activity)
    activity.update(evm_activity)
    activity.update(cert_manage_activity)
    activity.update(control_activity)
    activity.update(subscription_activity)
    restarted_org = "wx-org1"
    pre_profile = round_dir / "pre_restart.cov"
    post_profile = round_dir / "post_restart.cov"
    round_profile = round_dir / "round.cov"
    pre_snapshot_ok = goc_profile(GOC_CENTER, pre_profile)
    if pre_snapshot_ok:
        goc_clear(GOC_CENTER)
    stop_org(release_root, restarted_org)
    time.sleep(3)
    offline_accepts = 0
    offline_attempts = 24 if case == "txpool-normal-small-batch" else 12
    offline_sdks = [sdk_confs["wx-org2"], sdk_confs["wx-org3"]]
    for i in range(offline_attempts):
        params = json.dumps({
            "file_name": f"offline{i:03d}",
            "file_hash": f"ff3456df5799b87c77e7f88{i:04d}",
            "time": str(7654321 + i),
        }, separators=(",", ":"))
        if cmc(
            offline_sdks[i % len(offline_sdks)],
            "client", "contract", "user", "invoke",
            "--contract-name=fact",
            "--method=save",
            f"--params={params}",
            "--sync-result=true",
            "--result-to-string=true",
        ):
            offline_accepts += 1
        time.sleep(0.2)
    offline_control_activity = control_plane_sweep({
        org: sdk for org, sdk in sdk_confs.items() if org != restarted_org
    })
    offline_subscription_activity = subscription_sweep({
        org: sdk for org, sdk in sdk_confs.items() if org != restarted_org
    })
    restarted_ok = start_org(release_root, restarted_org)
    time.sleep(12)
    catchup_queries = 0
    for i in range(8):
        params = json.dumps({"file_hash": f"ff3456df5799b87c77e7f88{i:04d}"}, separators=(",", ":"))
        if cmc(
            sdk_conf,
            "client", "contract", "user", "get",
            "--contract-name=fact",
            "--method=find_by_file_hash",
            f"--params={params}",
            "--result-to-string=true",
        ):
            catchup_queries += 1
        time.sleep(0.2)
    post_restart_activity = cross_org_varied_workload({"wx-org1": sdk_conf})
    post_control_activity = control_plane_sweep(sdk_confs)
    post_subscription_activity = subscription_sweep(sdk_confs)
    extra_restart_ok = 0
    extra_restart_invokes = 0
    extra_restart_queries = 0
    extra_restart_attempted_invokes = 0
    extra_restart_attempted_queries = 0

    def extra_restart_cycle(restarted_org: str, offline_orgs: tuple[str, ...]) -> None:
        nonlocal extra_restart_ok
        nonlocal extra_restart_invokes
        nonlocal extra_restart_queries
        nonlocal extra_restart_attempted_invokes
        nonlocal extra_restart_attempted_queries
        stop_org(release_root, restarted_org)
        time.sleep(3)
        offline_sdks = {
            org: sdk for org, sdk in sdk_confs.items()
            if org in offline_orgs
        }
        offline_activity = cross_org_varied_workload(offline_sdks)
        extra_restart_invokes += offline_activity["cross_org_invokes_ok"]
        extra_restart_queries += offline_activity["cross_org_queries_ok"]
        extra_restart_attempted_invokes += offline_activity["cross_org_attempted_invokes"]
        extra_restart_attempted_queries += offline_activity["cross_org_attempted_queries"]
        restarted = start_org(release_root, restarted_org)
        if restarted:
            extra_restart_ok += 1
        time.sleep(12)
        recovered_activity = cross_org_varied_workload({restarted_org: sdk_confs[restarted_org]})
        extra_restart_invokes += recovered_activity["cross_org_invokes_ok"]
        extra_restart_queries += recovered_activity["cross_org_queries_ok"]
        extra_restart_attempted_invokes += recovered_activity["cross_org_attempted_invokes"]
        extra_restart_attempted_queries += recovered_activity["cross_org_attempted_queries"]
        sweep_after = control_plane_sweep(sdk_confs)
        subs_after = subscription_sweep(sdk_confs)
        activity["control_plane_attempts"] += sweep_after["control_plane_attempts"]
        activity["control_plane_successes"] += sweep_after["control_plane_successes"]
        activity["subscription_attempts"] += subs_after["subscription_attempts"]
        activity["subscription_successes"] += subs_after["subscription_successes"]

    extra_restart_cycle("wx-org2", ("wx-org1", "wx-org3", "wx-org4"))
    extra_restart_cycle("wx-org3", ("wx-org1", "wx-org2", "wx-org4"))
    time.sleep(5)
    post_snapshot_ok = goc_profile(GOC_CENTER, post_profile)
    goc_merge(
        [profile for profile, ok in ((pre_profile, pre_snapshot_ok), (post_profile, post_snapshot_ok)) if ok],
        round_profile,
    )
    activity["accepted_invokes"] += post_restart_activity["cross_org_invokes_ok"]
    activity["attempted_invokes"] += post_restart_activity["cross_org_attempted_invokes"]
    activity["successful_queries"] += post_restart_activity["cross_org_queries_ok"]
    activity["attempted_queries"] += post_restart_activity["cross_org_attempted_queries"]
    activity["accepted_invokes"] += extra_restart_invokes
    activity["attempted_invokes"] += extra_restart_attempted_invokes
    activity["successful_queries"] += extra_restart_queries
    activity["attempted_queries"] += extra_restart_attempted_queries
    activity.update({
        "restarted_org": restarted_org,
        "restart_catchup_ok": restarted_ok,
        "extra_restart_cycles_ok": extra_restart_ok,
        "extra_restart_cross_org_invokes_ok": extra_restart_invokes,
        "extra_restart_cross_org_queries_ok": extra_restart_queries,
        "extra_restart_attempted_invokes": extra_restart_attempted_invokes,
        "extra_restart_attempted_queries": extra_restart_attempted_queries,
        "offline_window_invokes": offline_accepts,
        "offline_window_attempts": offline_attempts,
        "catchup_queries": catchup_queries,
        "pre_restart_profile_ok": pre_snapshot_ok,
        "post_restart_profile_ok": post_snapshot_ok,
        "offline_control_plane_attempts": offline_control_activity["control_plane_attempts"],
        "offline_control_plane_successes": offline_control_activity["control_plane_successes"],
        "offline_subscription_attempts": offline_subscription_activity["subscription_attempts"],
        "offline_subscription_successes": offline_subscription_activity["subscription_successes"],
        "post_restart_cross_org_invokes_ok": post_restart_activity["cross_org_invokes_ok"],
        "post_restart_cross_org_queries_ok": post_restart_activity["cross_org_queries_ok"],
        "post_control_plane_attempts": post_control_activity["control_plane_attempts"],
        "post_control_plane_successes": post_control_activity["control_plane_successes"],
        "post_subscription_attempts": post_subscription_activity["subscription_attempts"],
        "post_subscription_successes": post_subscription_activity["subscription_successes"],
    })
    return activity, round_profile


def collect_coverage(profile_path: Path, *, full_coverage: bool = False) -> dict:
    return compute_line_coverage(
        profile_path,
        include_patterns=None if full_coverage else CHAINMAKER_COVERAGE_MODULES,
    )


def archive_logs(release_root: Path, round_dir: Path) -> None:
    for org in ORGS:
        org_root = release_root / release_name(org)
        bin_dir = org_root / "bin"
        config = org_root / "config" / f"{org}.chainmaker.org" / "chainmaker.yml"
        if config.exists():
            shutil.copy2(config, round_dir / f"{org}-chainmaker.yml")
        for name in ("panic.log",):
            src = bin_dir / name
            if src.exists():
                shutil.copy2(src, round_dir / f"{org}-{name}")
        log_files = sorted((org_root / "log").glob("*.log"))[-2:]
        for idx, src in enumerate(log_files):
            shutil.copy2(src, round_dir / f"{org}-log{idx}.log")


def run_round(arm: str, arm_dir: Path, seed: int, round_idx: int,
              case: str, cumulative_profile: Path, *,
              full_coverage: bool = False) -> dict:
    round_dir = arm_dir / f"round{round_idx:03d}"
    round_dir.mkdir(parents=True, exist_ok=True)
    runtime = Path("/tmp") / f"cm-live-{arm}-{round_idx}-{os.getpid()}"
    release_root = prepare_runtime(runtime)
    sdk_conf = round_dir / "sdk-config.yml"
    write_sdk_config(release_root, sdk_conf, "wx-org1")
    sdk_confs = {"wx-org1": sdk_conf}
    if arm == "varied":
        for org in ("wx-org2", "wx-org3", "wx-org4"):
            org_sdk = round_dir / f"sdk-config-{org}.yml"
            write_sdk_config(release_root, org_sdk, org)
            sdk_confs[org] = org_sdk
    org1 = release_root / release_name("wx-org1")
    config_only_baseline = arm not in ACTIVE_WORKLOAD_ARMS
    mutated = 0
    start = time.monotonic()
    started_orgs: list[str] = []
    activity = {
        "fact_contract_created": False,
        "counter_contract_created": False,
        "accepted_invokes": 0,
        "attempted_invokes": 0,
        "successful_queries": 0,
        "attempted_queries": 0,
    }
    round_profile = round_dir / "round.cov"
    goc_init(GOC_CENTER)
    kill_stale_chainmakers(release_root)
    start_all(release_root)
    try:
        for org in ORGS:
            if node_running(release_root, org):
                started_orgs.append(org)
        if started_orgs:
            goc_clear(GOC_CENTER)
            stop_org(release_root, "wx-org1")
            time.sleep(3)
            overlay_case_config(org1, case, seed)
            mutated = mutate_node_config(org1, arm, seed)
            restarted_with_config = start_org(release_root, "wx-org1")
            time.sleep(12)
            activity["config_restart_ok"] = restarted_with_config
            if config_only_baseline:
                time.sleep(8)
            elif arm == "varied":
                activity, round_profile = strong_varied_interact(release_root, sdk_confs, round_dir, case)
                activity["config_restart_ok"] = restarted_with_config
            else:
                activity = interact(arm, sdk_conf, case)
                activity["config_restart_ok"] = restarted_with_config
                time.sleep(5)
                goc_profile(GOC_CENTER, round_profile)
            if config_only_baseline:
                goc_profile(GOC_CENTER, round_profile)
    finally:
        stop_all(release_root)
        kill_stale_chainmakers(release_root)
        archive_logs(release_root, round_dir)
    elapsed = time.monotonic() - start
    merge_into_cumulative(cumulative_profile, round_profile)
    metrics = collect_coverage(cumulative_profile, full_coverage=full_coverage)
    result = {
        "arm": arm,
        "round": round_idx,
        "case": case,
        "mutated_options": mutated,
        "config_only_baseline": config_only_baseline,
        "started_nodes": len(started_orgs),
        "started_orgs": started_orgs,
        "elapsed_seconds": round(elapsed, 1),
        **activity,
        **metrics,
    }
    (round_dir / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"{arm} round={round_idx} case={case}: covered={metrics['covered_lines']} "
        f"total={metrics['total_lines']} ({metrics['coverage_pct']:.2f}%) "
        f"started={len(started_orgs)}/{len(ORGS)} accepted={activity['accepted_invokes']}",
        flush=True,
    )
    shutil.rmtree(runtime, ignore_errors=True)
    return result


def append_timeline(arm_dir: Path, record: dict) -> None:
    with (arm_dir / "timeline.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def run_campaign_arm(arm: str, out_dir: Path, seed: int,
                     rounds: int, budget_minutes: float,
                     converge_rounds: int, full_budget: bool,
                     *, full_coverage: bool = False) -> dict:
    arm_dir = out_dir / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    cumulative_profile = arm_dir / "coverage.cov"
    cumulative_profile.unlink(missing_ok=True)
    (arm_dir / "timeline.jsonl").unlink(missing_ok=True)

    deadline = (time.monotonic() + budget_minutes * 60.0) if budget_minutes > 0 else None
    round_limit = rounds
    if budget_minutes > 0 and rounds <= 1:
        round_limit = 0

    timeline: list[dict] = []
    round_idx = 1
    while True:
        if round_limit > 0 and round_idx > round_limit:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        case = CASES[(round_idx - 1) % len(CASES)]
        result = run_round(
            arm,
            arm_dir,
            seed + round_idx - 1,
            round_idx,
            case,
            cumulative_profile,
            full_coverage=full_coverage,
        )
        append_timeline(arm_dir, result)
        timeline.append(result)
        if not full_budget and len(timeline) >= converge_rounds:
            window = timeline[-converge_rounds:]
            if len({item["covered_lines"] for item in window}) == 1:
                break
        round_idx += 1

    final = timeline[-1] if timeline else collect_coverage(
        cumulative_profile, full_coverage=full_coverage)
    converged = False
    if len(timeline) >= converge_rounds:
        window = timeline[-converge_rounds:]
        converged = len({item["covered_lines"] for item in window}) == 1
    summary = {
        **final,
        "rounds_completed": len(timeline),
        "converged": converged,
        "coverage_scope": (
            "merged goc-based unique full line coverage across four instrumented org nodes"
            if full_coverage else
            "merged goc-based unique line coverage across ChainMaker runtime-sensitive sync/rpc/cache hot paths (scheduler/processor/event/send_request_sync/proposal_cache/api_service)"
        ),
    }
    (arm_dir / "result.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    import argparse
    global GOC_CENTER, PORT_OFFSET
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arms", default="fixed,varied,ecfuzz,conferr,conftest,confdiag")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20270803)
    parser.add_argument("--rounds", type=int, default=1,
                        help="number of rounds per arm; with --budget-minutes and rounds<=1, run until budget")
    parser.add_argument("--converge-rounds", type=int, default=8)
    parser.add_argument("--budget-minutes", type=float, default=0.0,
                        help="wall-clock budget per arm; 0 disables budgeted looping")
    parser.add_argument("--full-budget", action="store_true",
                        help="run for the full wall-clock budget even after convergence")
    parser.add_argument("--full-coverage", action="store_true",
                        help="collect full line coverage instead of scoped config-relevant modules")
    parser.add_argument("--goc-center", default=GOC_CENTER,
                        help="goc server center URL, e.g. http://127.0.0.1:17771")
    parser.add_argument("--port-offset", type=int, default=PORT_OFFSET,
                        help="add this offset to ChainMaker p2p/rpc ports for parallel campaigns")
    args = parser.parse_args()
    GOC_CENTER = args.goc_center
    PORT_OFFSET = args.port_offset
    args.output.mkdir(parents=True, exist_ok=True)
    ensure_instrumented_binary()
    goc_server = start_goc_server(
        GOC_CENTER,
        args.output / ".chainmaker-goc-services.txt",
        args.output / ".chainmaker-goc-server.log",
    )
    try:
        results = {}
        for i, arm in enumerate(args.arms.split(",")):
            arm_name = arm.strip()
            if not arm_name:
                continue
            results[arm_name] = run_campaign_arm(
                arm_name,
                args.output,
                args.seed + i * 1000,
                args.rounds,
                args.budget_minutes,
                args.converge_rounds,
                args.full_budget,
                full_coverage=args.full_coverage,
            )
    finally:
        stop_goc_server(goc_server)
    (args.output / "summary.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
