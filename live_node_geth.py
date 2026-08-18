#!/usr/bin/env python3
"""Live two-node go-ethereum coverage experiment.

TTD-zero PoS execution-layer private chain on loopback: node0 is the
configured actor, node1 is the unchanged peer.  A fake beacon client drives
normal block production in the fixed arm and stronger payload / transaction
diversity in the varied arm.  Adapted baseline tools remain configuration-only:
they do not receive extra transaction or inter-node-message mutation after the
configured node is restarted.

Arms: fixed, varied, ecfuzz, conferr, conftest, confdiag.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/home/geth/tse/BCFuzzer_upstream/source_code/common")
from goc_utils import (  # noqa: E402
    compute_line_coverage,
    ensure_goc_binary,
    goc_build_env,
    goc_init,
    goc_profile,
    merge_into_cumulative,
    start_goc_server,
    stop_goc_server,
)
from config_mutators import STRATEGIES  # noqa: E402
from targets import apply_case  # noqa: E402

ROOT = Path("/home/geth/tse/go-ethereum")
INSTRUMENTED_GETH = Path("/tmp/geth-goc-cover")
PEER_GETH = ROOT / "build/bin/geth"
FAKE_CL = (Path("/home/geth/tse/inter-node-bugs-final/geth/01_miner_gaslimit_collapse")
           / "fake_beacon_client.py")
BLOB_FAKE_CL = (Path("/home/geth/tse/inter-node-bugs-final/geth/02_blobpool_pricebump")
                / "fake_beacon_client.py")
GETH_GOC_SOURCE = Path("/tmp/geth-goc-src")
GETH_GO_BIN = Path("/home/geth/go/pkg/mod/golang.org/toolchain@v0.0.1-go1.24.0.linux-amd64/bin")
GOC_CENTER = "http://127.0.0.1:17772"
GENESIS = Path("/tmp/geth-genesis.json")
PASSWORD = Path("/tmp/geth-password.txt")
BLOB_TSETUP = Path("/home/geth/go/pkg/mod/github.com/ethereum/c-kzg-4844/v2@v2.1.8/src/trusted_setup.txt")
RPC0 = "http://127.0.0.1:8545"
RPC1 = "http://127.0.0.1:8546"
CASES = ["miner-txpool-balanced", "blobpool-constrained"]
BCFUZZER_ARMS = {"fixed", "varied"}
ACTIVE_WORKLOAD_ARMS = {"varied"}
GETH_BASELINE_SECTIONS = {"Eth.Miner", "Eth.TxPool", "Eth.BlobPool"}
GETH_COVERAGE_MODULES = [
    "/core/txpool/",
    "/eth/catalyst/",
    "/eth/downloader/",
    "/miner/",
    "/p2p/",
]
GETH_NUMERIC_BOUNDS = {
    ("Eth.Miner", "GasCeil"): (21000, 100_000_000, 30_000_000),
    ("Eth.Miner", "GasPrice"): (0, 1_000_000_000_000, 1_000_000),
    ("Eth.Miner", "Recommit"): (100_000_000, 600_000_000_000, 2_000_000_000),
    ("Eth.TxPool", "PriceLimit"): (0, 1_000_000_000, 1),
    ("Eth.TxPool", "PriceBump"): (0, 1_000, 10),
    ("Eth.TxPool", "GlobalSlots"): (1, 1_000_000, 2048),
    ("Eth.TxPool", "GlobalQueue"): (1, 1_000_000, 1024),
    ("Eth.TxPool", "Lifetime"): (1_000_000_000, 604_800_000_000_000, 10_800_000_000_000),
    ("Eth.BlobPool", "Datacap"): (1024, 1_099_511_627_776, 1_073_741_824),
    ("Eth.BlobPool", "PriceBump"): (0, 1_000, 100),
}

for proxy_key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(proxy_key, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

SIGNER = ""
SENDER_KEY = "0x00000000000000000000000000000000000000000000000000000000000000a2"

GENESIS_TMPL = {
    "config": {"chainId": 1337, "homesteadBlock": 0, "eip150Block": 0,
               "eip155Block": 0, "eip158Block": 0, "byzantiumBlock": 0,
               "constantinopleBlock": 0, "petersburgBlock": 0,
               "istanbulBlock": 0, "berlinBlock": 0, "londonBlock": 0,
               "terminalTotalDifficulty": 0, "shanghaiTime": 0,
               "cancunTime": 0, "pragueTime": 0, "osakaTime": 0,
               "blobSchedule": {
                   "cancun": {"target": 3, "max": 6, "baseFeeUpdateFraction": 3338477},
                   "prague": {"target": 6, "max": 9, "baseFeeUpdateFraction": 5007716},
               }},
    "difficulty": "0x0",
    "gasLimit": "0x1c9c380",
    "extraData": "0x00",
    "alloc": {
        "{SIGNER_ADDR}": {"balance": "0x200000000000000000000000000000000000000000000000000000000000000"},
        "{SENDER_ADDR}": {"balance": "0x200000000000000000000000000000000000000000000000000000000000000"},
    },
}

BLOB_RECIPIENTS = (
    "0x71562b71999873db5b286df9577581998cbf4e81",
    "0x71562b71999873db5b286df9577581998cbf4e82",
)
_BLOB_SETTINGS = None


def prepare_geth_goc_source() -> Path:
    shutil.rmtree(GETH_GOC_SOURCE, ignore_errors=True)
    shutil.copytree(ROOT, GETH_GOC_SOURCE, ignore=shutil.ignore_patterns(".git"))
    go_mod = GETH_GOC_SOURCE / "go.mod"
    lines = go_mod.read_text(encoding="utf-8").splitlines()
    cleaned: list[str] = []
    skip_tool = False
    for line in lines:
        if not skip_tool and line.startswith("tool ("):
            skip_tool = True
            continue
        if skip_tool:
            if line.strip() == ")":
                skip_tool = False
            continue
        cleaned.append(line)
    go_mod.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
    return GETH_GOC_SOURCE


def ensure_instrumented_binary() -> Path:
    ensure_goc_binary()
    if INSTRUMENTED_GETH.is_file() and os.access(INSTRUMENTED_GETH, os.X_OK):
        return INSTRUMENTED_GETH
    source_root = prepare_geth_goc_source()
    env = goc_build_env()
    env["PATH"] = f"{GETH_GO_BIN}:{env['PATH']}"
    subprocess.run(
        ["/tmp/goc", "build", f"--center={GOC_CENTER}",
         "--output", str(INSTRUMENTED_GETH), "."],
        cwd=source_root / "cmd/geth",
        check=True,
        timeout=3600,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    return INSTRUMENTED_GETH


def kill_stale_geth_processes() -> None:
    victims: list[int] = []
    markers = (
        "--networkid 1337",
        "--port 30310",
        "--port 30311",
        "--http.port 8545",
        "--http.port 8546",
        "--authrpc.port 8551",
        "--authrpc.port 8552",
        "/tmp/geth-live-",
    )
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_text(errors="replace").replace("\x00", " ")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if "geth" not in cmdline:
            continue
        if any(marker in cmdline for marker in markers):
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


def make_keys(work: Path) -> str:
    """Create the signer account (node0 keystore) and the clique genesis."""
    from eth_account import Account
    d0 = work / "node0"
    d1 = work / "node1"
    d0.mkdir(parents=True, exist_ok=True)
    d1.mkdir(parents=True, exist_ok=True)
    PASSWORD.write_text("password\n")
    r = subprocess.run([str(PEER_GETH), "--datadir", str(d0), "account", "new",
                        "--password", str(PASSWORD)],
                       capture_output=True, text=True, timeout=120)
    m = re.search(r"0x[0-9a-fA-F]{40}", r.stdout)
    signer_addr = m.group(0).lower()
    sender_addr = Account.from_key(SENDER_KEY).address.lower()
    genesis = (json.dumps(GENESIS_TMPL)
               .replace("{SIGNER_ADDR}", signer_addr)
               .replace("{SENDER_ADDR}", sender_addr))
    GENESIS.write_text(genesis)
    return signer_addr


def init_nodes(work: Path) -> None:
    for d in (work / "node0", work / "node1"):
        subprocess.run([str(PEER_GETH), "--datadir", str(d), "init", str(GENESIS)],
                       check=True, capture_output=True, text=True, timeout=120)


def apply_config(work: Path, case: str, seed: int) -> Path:
    """Generate the BCFuzzer geth.toml case config (mutated per tool later)."""
    run_dir = Path("/tmp") / f"geth-case-{seed}-{os.getpid()}"
    run_dir.mkdir(exist_ok=True)
    meta = apply_case("geth", ROOT, case, run_dir, seed)
    return Path(meta["output"])


def _bounded_int(value: str, low: int, high: int, default: int) -> str:
    raw = value.strip().strip('"').strip("'")
    try:
        number = int(raw, 0)
    except (TypeError, ValueError):
        number = default
    number = max(low, min(high, number))
    return str(number)


def sanitize_geth_baseline_config(config: Path) -> int:
    """Keep adapted baseline mutants inside geth's live-admissible domains.

    The raw comparison tools may emit type errors, malformed TOML strings, or
    values far outside geth's uint/time bounds.  The live calibration compares
    deployed configurations, so the common adapter preserves the selected
    option but clamps it to a value geth can parse and run on one node.
    """
    from config_mutators import parse_lines

    text = config.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    changed = 0
    for item in parse_lines(text):
        key = (item["section"], item["key"])
        if key in GETH_NUMERIC_BOUNDS:
            low, high, default = GETH_NUMERIC_BOUNDS[key]
            new_value = _bounded_int(item["value"], low, high, default)
        elif key == ("Eth.BlobPool", "Datadir"):
            raw = item["value"].strip()
            new_value = raw if raw.startswith('"') and raw.endswith('"') else '"blobpool"'
        else:
            continue
        new_line = f"{item['indent']}{item['key']} {item['sep']} {new_value}"
        if item["line"] < len(lines) and lines[item["line"]] != new_line:
            lines[item["line"]] = new_line
            changed += 1
    if changed:
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def start_node(work: Path, node: str, config: Path | None, signer: str,
               mine: bool, peer: str | None, logs: Path) -> subprocess.Popen:
    d = work / node
    binary = ensure_instrumented_binary()
    argv = [str(binary)]
    if config is not None:
        argv += ["--config", str(config)]
    argv += ["--datadir", str(d), "--networkid", "1337",
             "--syncmode", "full", "--port",
             "30310" if node == "node0" else "30311",
             "--http", "--http.port", "8545" if node == "node0" else "8546",
             "--http.addr", "127.0.0.1",
             "--http.api", "eth,net,web3,txpool,admin",
             "--authrpc.addr", "127.0.0.1",
             "--authrpc.port", "8551" if node == "node0" else "8552",
             "--bootnodes", "", "--netrestrict", "127.0.0.0/8"]
    if mine:
        argv += ["--password", str(PASSWORD)]
    if peer:
        argv += ["--bootnodes", peer]
    env = os.environ.copy()
    env["GOC_SERVICE_NAME"] = f"geth-{node}"
    fh = logs.open("w")
    return subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT,
                            env=env)


def wait_http(url: str, timeout: int = 60) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if rpc_call(url, "eth_blockNumber") is not None:
                return True
        except Exception:
            time.sleep(1)
    return False


def rpc_call(url: str, method: str, params: list | None = None,
             timeout: int = 5):
    import urllib.request
    payload = json.dumps({
        "jsonrpc": "2.0", "method": method,
        "params": params or [], "id": 1,
    }).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
    except Exception:
        return None
    if "error" in body:
        return None
    return body.get("result")


def peer_count(url: str) -> int:
    result = rpc_call(url, "net_peerCount")
    return int(result, 16) if isinstance(result, str) else 0


def send_txs(count: int, url: str = RPC0) -> None:
    """Send `count` signed raw transactions from the pre-funded sender."""
    import urllib.request
    from eth_account import Account
    acct = Account.from_key(SENDER_KEY)
    nonce = None
    for i in range(count):
        if nonce is None or i % 100 == 0:
            req = urllib.request.Request(
                url,
                data=json.dumps({"jsonrpc": "2.0", "method": "eth_getTransactionCount",
                                 "params": [acct.address, "pending"], "id": 1}).encode(),
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    nonce = int(json.loads(r.read())["result"], 16)
            except Exception:
                return
        tx = {"to": acct.address,
              "value": 1, "gas": 21000, "gasPrice": 1000000000,
              "nonce": nonce + i, "chainId": 1337}
        signed = acct.sign_transaction(tx)
        req = urllib.request.Request(
            url,
            data=json.dumps({"jsonrpc": "2.0", "method": "eth_sendRawTransaction",
                             "params": ["0x" + signed.raw_transaction.hex().removeprefix("0x")],
                             "id": 1}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                pass
        except Exception:
            pass
        if i % 50 == 0:
            time.sleep(0.3)


def send_replacement_txs(base_nonce: int, variants: int = 12,
                         url: str = RPC0) -> int:
    import urllib.request
    from eth_account import Account

    acct = Account.from_key(SENDER_KEY)
    accepted = 0
    for index in range(variants):
        tx = {
            "to": acct.address,
            "value": 1 + index,
            "gas": 21000,
            "gasPrice": 1_000_000_000 + index * 150_000_000,
            "nonce": base_nonce,
            "chainId": 1337,
        }
        signed = acct.sign_transaction(tx)
        req = urllib.request.Request(
            url,
            data=json.dumps({
                "jsonrpc": "2.0",
                "method": "eth_sendRawTransaction",
                "params": ["0x" + signed.raw_transaction.hex().removeprefix("0x")],
                "id": 1,
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5):
                accepted += 1
        except Exception:
            pass
        time.sleep(0.1)
    return accepted


def send_data_txs(start_nonce: int, count: int, data_size: int,
                  url: str = RPC0) -> int:
    import urllib.request
    from eth_account import Account

    acct = Account.from_key(SENDER_KEY)
    accepted = 0
    payload = "ab" * data_size
    for index in range(count):
        tx = {
            "to": acct.address,
            "value": 1,
            "gas": 400000 + data_size * 16,
            "gasPrice": 1_500_000_000 + index * 1_000,
            "nonce": start_nonce + index,
            "chainId": 1337,
            "data": "0x" + payload,
        }
        signed = acct.sign_transaction(tx)
        req = urllib.request.Request(
            url,
            data=json.dumps({
                "jsonrpc": "2.0",
                "method": "eth_sendRawTransaction",
                "params": ["0x" + signed.raw_transaction.hex().removeprefix("0x")],
                "id": 1,
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5):
                accepted += 1
        except Exception:
            pass
        if index % 4 == 0:
            time.sleep(0.2)
    return accepted


def blob_settings():
    global _BLOB_SETTINGS
    if _BLOB_SETTINGS is not None:
        return _BLOB_SETTINGS
    if not BLOB_TSETUP.is_file():
        return None
    try:
        import ckzg
        _BLOB_SETTINGS = ckzg.load_trusted_setup(str(BLOB_TSETUP), 0)
    except Exception:
        _BLOB_SETTINGS = None
    return _BLOB_SETTINGS


def build_blob_raw_tx(nonce: int, max_fee: int, tip: int, blob_fee: int,
                      recipient: str, blob_byte: int) -> str | None:
    settings = blob_settings()
    if settings is None:
        return None
    try:
        import ckzg
        import rlp
        from eth_account import Account
        from eth_utils import to_checksum_address
        blob = bytes([blob_byte & 0xFF]) * 131072
        tx = {
            "chainId": 1337,
            "nonce": nonce,
            "gas": 50000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": tip,
            "to": to_checksum_address(recipient),
            "value": 10**12,
            "data": b"",
            "type": 3,
            "maxFeePerBlobGas": blob_fee,
        }
        signed = Account.sign_transaction(tx, SENDER_KEY, blobs=[blob])
        payload = rlp.decode(signed.raw_transaction[1:])
        inner, legacy_blobs, commitments = payload[0], payload[1], payload[2]
        _, cell_proofs = ckzg.compute_cells_and_kzg_proofs(blob, settings)
        v1_raw = bytes([0x03]) + rlp.encode([inner, 1, legacy_blobs, commitments, cell_proofs])
        return "0x" + v1_raw.hex()
    except Exception:
        return None


def send_raw_transaction(url: str, raw_hex: str) -> bool:
    import urllib.request
    req = urllib.request.Request(
        url,
        data=json.dumps({
            "jsonrpc": "2.0",
            "method": "eth_sendRawTransaction",
            "params": [raw_hex],
            "id": 1,
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=8).read()
        return True
    except Exception:
        return False


def send_blob_txs(start_nonce: int, count: int, url: str = RPC1) -> int:
    accepted = 0
    for index in range(count):
        raw = build_blob_raw_tx(
            start_nonce + index,
            20_000_000_000 + index * 1_000_000_000,
            20_000_000_000 + index * 1_000_000_000,
            1_000_000_000 + index * 100_000_000,
            BLOB_RECIPIENTS[index % len(BLOB_RECIPIENTS)],
            0x40 + index,
        )
        if raw and send_raw_transaction(url, raw):
            accepted += 1
        time.sleep(0.3)
    return accepted


def send_blob_replacements(base_nonce: int, url: str = RPC1, variants: int = 6) -> int:
    accepted = 0
    for index in range(variants):
        raw = build_blob_raw_tx(
            base_nonce,
            30_000_000_000 + index * 6_000_000_000,
            30_000_000_000 + index * 6_000_000_000,
            2_000_000_000 + index * 600_000_000,
            BLOB_RECIPIENTS[index % len(BLOB_RECIPIENTS)],
            0x80 + index,
        )
        if raw and send_raw_transaction(url, raw):
            accepted += 1
        time.sleep(0.4)
    return accepted


def current_block_hash(url: str) -> str | None:
    block = rpc_call(url, "eth_getBlockByNumber", ["latest", False])
    if isinstance(block, dict):
        return block.get("hash")
    return None


def drive_fake_beacon(authrpc_port: int, jwtsecret: Path, head_hash: str,
                      mode: str, rounds: int, period: int = 1,
                      api_version: str = "v1", script: Path = FAKE_CL) -> bool:
    argv = [
        sys.executable,
        str(script),
        api_version,
        mode,
        str(authrpc_port),
        str(jwtsecret),
        head_hash,
        str(rounds),
        str(period),
    ]
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=max(180, rounds * max(period, 1) * 5),
    )
    return completed.returncode == 0


def synchronize_peer(head_hash: str, work: Path, *, api_version: str = "v1",
                     script: Path = FAKE_CL, rounds: int = 3) -> bool:
    jwt1 = work / "node1" / "geth" / "jwtsecret"
    if not jwt1.is_file():
        return False
    return drive_fake_beacon(
        8552, jwt1, head_hash, "update",
        rounds=rounds, period=1,
        api_version=api_version, script=script)


def simple_fixed_workload(work: Path) -> dict:
    return {
        "sent_simple_txs": 0,
        "replacement_txs": 0,
        "data_txs": 0,
        "fake_payload_rounds": 0,
        "peer_sync_updates": 0,
    }


def strong_varied_workload(work: Path, case: str) -> dict:
    import urllib.request
    from eth_account import Account

    acct = Account.from_key(SENDER_KEY)
    nonce_hex = rpc_call(RPC1, "eth_getTransactionCount", [acct.address, "pending"])
    base_nonce = int(nonce_hex, 16) if isinstance(nonce_hex, str) else 0

    sent_simple = 0
    replacement = 0
    data_txs = 0
    blob_txs = 0
    blob_replacements = 0
    if case == "blobpool-constrained":
        for batch in (48, 64):
            send_txs(batch, url=RPC1)
            sent_simple += batch
            time.sleep(1)
        blob_txs = send_blob_txs(base_nonce + sent_simple + 5, count=8, url=RPC1)
        blob_replacements = send_blob_replacements(base_nonce + sent_simple + 25, url=RPC1, variants=6)
        data_txs = send_data_txs(base_nonce + sent_simple + 40, count=6, data_size=1024, url=RPC1)
    else:
        for batch in (64, 96, 128):
            send_txs(batch, url=RPC1)
            sent_simple += batch
            time.sleep(1)
        replacement = send_replacement_txs(base_nonce + sent_simple + 5, variants=16, url=RPC1)
        data_txs = send_data_txs(base_nonce + sent_simple + 25, count=12, data_size=2048, url=RPC1)

    malformed_calls = 0
    for bad in ("0x", "0xdeadbeef", "0x03ff"):
        req = urllib.request.Request(
            RPC1,
            data=json.dumps({
                "jsonrpc": "2.0",
                "method": "eth_sendRawTransaction",
                "params": [bad],
                "id": 1,
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            malformed_calls += 1

    genesis = rpc_call(RPC0, "eth_getBlockByNumber", ["0x0", False]) or {}
    genesis_hash = genesis.get("hash") if isinstance(genesis, dict) else None
    jwt0 = work / "node0" / "geth" / "jwtsecret"
    payload_ok = False
    synced_ok = False
    if genesis_hash and jwt0.is_file():
        if case == "blobpool-constrained":
            payload_ok = drive_fake_beacon(
                8551, jwt0, genesis_hash, "payload",
                rounds=16, period=1, api_version="v5", script=BLOB_FAKE_CL)
        else:
            payload_ok = drive_fake_beacon(
                8551, jwt0, genesis_hash, "payload",
                rounds=12, period=1, api_version="v1", script=FAKE_CL)
        head_hash = current_block_hash(RPC0)
        if head_hash:
            if case == "blobpool-constrained":
                synced_ok = synchronize_peer(
                    head_hash, work, api_version="v5",
                    script=BLOB_FAKE_CL, rounds=5)
            else:
                synced_ok = synchronize_peer(head_hash, work)

    _ = rpc_call(RPC0, "txpool_status")
    _ = rpc_call(RPC0, "txpool_content")
    _ = rpc_call(RPC1, "txpool_status")
    return {
        "sent_simple_txs": sent_simple,
        "replacement_txs": replacement,
        "data_txs": data_txs,
        "blob_txs": blob_txs,
        "blob_replacements": blob_replacements,
        "malformed_rpc_calls": malformed_calls,
        "fake_payload_rounds": (16 if case == "blobpool-constrained" else 12) if payload_ok else 0,
        "peer_sync_updates": (5 if case == "blobpool-constrained" else 3) if synced_ok else 0,
    }


def interact(arm: str, work: Path, case: str) -> dict:
    if arm == "fixed":
        return simple_fixed_workload(work)
    if arm == "varied":
        return strong_varied_workload(work, case)
    return {
        "sent_simple_txs": 0,
        "replacement_txs": 0,
        "data_txs": 0,
        "blob_txs": 0,
        "blob_replacements": 0,
        "malformed_rpc_calls": 0,
        "fake_payload_rounds": 0,
        "peer_sync_updates": 0,
    }


def collect_coverage(profile_path: Path, *, full_coverage: bool = False) -> dict:
    return compute_line_coverage(
        profile_path,
        include_patterns=None if full_coverage else GETH_COVERAGE_MODULES,
    )


def run_round(arm: str, arm_dir: Path, seed: int, round_idx: int,
              case: str, cumulative_profile: Path, *,
              full_coverage: bool = False) -> dict:
    round_dir = arm_dir / f"round{round_idx:03d}"
    round_dir.mkdir(parents=True, exist_ok=True)
    work = Path("/tmp") / f"geth-live-{arm}-{round_idx}-{os.getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    global SIGNER
    signer = make_keys(work)
    SIGNER = signer
    init_nodes(work)
    cfg = apply_config(work, case, seed)
    shutil.copy2(cfg, round_dir / "node0.toml")
    from campaign_baseline_real import mutate_with_tool
    adapted_options = 0
    config_only_baseline = arm not in ACTIVE_WORKLOAD_ARMS
    if config_only_baseline:
        mutated = mutate_with_tool(
            cfg, arm, seed, target="geth",
            eligible_sections=GETH_BASELINE_SECTIONS)
        adapted_options = sanitize_geth_baseline_config(cfg)
    else:
        mutated = 0
    start = time.monotonic()
    round_profile = round_dir / "round.cov"
    goc_init(GOC_CENTER)
    kill_stale_geth_processes()
    p0 = start_node(work, "node0", cfg, signer, mine=True, peer=None,
                    logs=round_dir / "node0.log")
    p1 = start_node(work, "node1", None, signer, mine=False, peer=None,
                    logs=round_dir / "node1.log")
    ok = wait_http(RPC0) and wait_http(RPC1)
    if ok:
        enode = rpc_call(RPC0, "admin_nodeInfo") or {}
        if isinstance(enode, dict) and enode.get("enode"):
            rpc_call(RPC1, "admin_addPeer", [enode["enode"]])
            time.sleep(5)
    peers0 = peer_count(RPC0) if ok else 0
    peers1 = peer_count(RPC1) if ok else 0
    activity = {
        "sent_simple_txs": 0,
        "replacement_txs": 0,
        "data_txs": 0,
        "blob_txs": 0,
        "blob_replacements": 0,
        "malformed_rpc_calls": 0,
        "fake_payload_rounds": 0,
        "peer_sync_updates": 0,
    }
    if ok:
        if config_only_baseline:
            time.sleep(8)
        else:
            activity = interact(arm, work, case)
        time.sleep(5)
        goc_profile(GOC_CENTER, round_profile)
    else:
        print("WARN: node0 RPC unreachable", flush=True)
    for p in (p0, p1):
        if p:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
    kill_stale_geth_processes()
    elapsed = time.monotonic() - start
    merge_into_cumulative(cumulative_profile, round_profile)
    metrics = collect_coverage(cumulative_profile, full_coverage=full_coverage)
    result = {"arm": arm, "case": case, "mutated_options": mutated,
              "round": round_idx,
              "adapted_options": adapted_options,
              "config_only_baseline": config_only_baseline,
              "node0_peers": peers0, "node1_peers": peers1,
              "coverage_scope": "merged goc-based unique line coverage across both instrumented geth nodes",
              "elapsed_seconds": round(elapsed, 1), **activity, **metrics}
    (round_dir / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    print(f"{arm} round={round_idx} case={case}: covered={metrics['covered_lines']} "
          f"total={metrics['total_lines']} ({metrics['coverage_pct']:.2f}%) "
          f"elapsed={elapsed:.0f}s mutated={mutated}", flush=True)
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
            "merged goc-based unique full line coverage across both instrumented geth nodes"
            if full_coverage else
            "merged goc-based unique line coverage across geth config-relevant modules (txpool/catalyst/downloader/miner/p2p)"
        ),
    }
    (arm_dir / "result.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    import argparse
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
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    ensure_instrumented_binary()
    goc_server = start_goc_server(
        GOC_CENTER,
        args.output / ".geth-goc-services.txt",
        args.output / ".geth-goc-server.log",
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
