#!/usr/bin/env python3
"""Live multi-node FISCO-BCOS coverage experiment.

Per the paper's deployment: a four-node PBFT network on loopback; each arm
applies its configuration (default for the fixed baseline, BCFuzzer's config
model for the varied arm, or an adapted baseline tool's config mutation) to
one designated node (node0), starts a four-node chain whose binaries are all
coverage-instrumented, drives transactions over the RPC interface, stops the
chain and collects merged whole-code line coverage (absolute covered-line
count).

Arms: fixed, varied, ecfuzz, conferr, conftest, confdiag.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import requests
import urllib3
from eth_abi import decode, encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_keys import keys
from eth_utils import keccak

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/home/geth/tse/BCFuzzer_upstream/source_code/common")
from config_mutators import STRATEGIES  # noqa: E402
from targets import apply_case  # noqa: E402

ROOT = Path("/home/geth/tse/FISCO-BCOS")
PEER_NODE_BIN = ROOT / "build/fisco-bcos-air/fisco-bcos"
BUILD_CHAIN = ROOT / "tools/BcosAirBuilder/build_chain.sh"
SEND_TXS = ROOT / "poc_common/send_txs.py"
BUILD = ROOT / "build-cov"
PORT_OFFSET = 0
P2P_BASE = 30300
RPC_BASE = 20200
GAS_PRICE = 0
GAS_LIMIT = 30_000_000
PRECOMPILED_SYS_CONFIG = "0x0000000000000000000000000000000000001000"
PRECOMPILED_TABLE_MANAGER = "0x0000000000000000000000000000000000001002"
PRECOMPILED_AUTH = "0x0000000000000000000000000000000000001005"
PRECOMPILED_DAG_TRANSFER = "0x000000000000000000000000000000000000100c"
PRECOMPILED_BFS = "0x000000000000000000000000000000000000100e"
PRECOMPILED_BALANCE = "0x0000000000000000000000000000000000001011"
RPC_CERT: tuple[str, str] | None = None
CHAIN_ID = "chain0"
GROUP_ID = "group0"

urllib3.disable_warnings()
RPC_SESSION = requests.Session()
RPC_SESSION.trust_env = False

# BCFuzzer fisco config case names (config model)
CASES = ["txpool-sync-tree", "consensus-executor-balanced"]
BCFUZZER_ARMS = {"fixed", "varied"}
ACTIVE_WORKLOAD_ARMS = {"varied"}
WRITE_ACTIVITY_KEYS = {
    "dag_adds",
    "dag_saves",
    "dag_transfers",
    "dag_draws",
    "bfs_mkdirs",
    "bfs_links",
    "bfs_touches",
    "bfs_rebuilds",
    "syscfg_writes",
    "balance_writes",
    "auth_writes",
    "table_writes",
}
FISCO_BASELINE_SECTIONS = {"txpool", "sync", "executor", "tx"}
FISCO_SCOPED_COVERAGE_MODULES = [
    "bcos-txpool/",
    "bcos-sync/",
    "bcos-executor/",
    "bcos-scheduler/",
    "transaction-executor/",
    "transaction-scheduler/",
    "bcos-pbft/",
    "bcos-sealer/",
]
FISCO_INT_BOUNDS = {
    ("txpool", "limit"): (1, 1_000_000, 8000),
    ("txpool", "notify_worker_num"): (1, 256, 2),
    ("txpool", "txs_expiration_time"): (1, 86_400, 300),
    ("sync", "tree_width"): (1, 256, 2),
    ("tx", "gas_limit"): (21_000, 10_000_000_000, 3_000_000_000),
}
FISCO_BOOL_DEFAULTS = {
    ("txpool", "enable_txs_from_free_node"): "false",
    ("sync", "send_txs_by_tree"): "true",
    ("sync", "sync_block_by_tree"): "true",
    ("executor", "enable_dag"): "true",
    ("executor", "baseline_scheduler"): "false",
    ("executor", "baseline_scheduler_parallel"): "false",
}


def cov_node_bin() -> Path:
    return BUILD / "fisco-bcos-air" / "fisco-bcos"


def gcov_prefix_strip() -> int:
    original_build = ROOT / "build-cov"
    return len([part for part in original_build.parts if part not in ("/", "")])


def coverage_env_exports() -> str:
    return (
        f"export GCOV_PREFIX={BUILD}\n"
        f"export GCOV_PREFIX_STRIP={gcov_prefix_strip()}\n"
    )


def rpc_call(rpc: str, method: str, params: list) -> dict:
    try:
        response = RPC_SESSION.post(
            rpc,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            timeout=5,
            cert=RPC_CERT,
            verify=False,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def rpc_result_ok(response: dict) -> bool:
    return isinstance(response, dict) and "error" not in response and "result" in response


def encode_call(signature: str, arg_types: list[str], args: list) -> str:
    selector = keccak(text=signature)[:4]
    payload = selector + encode(arg_types, args)
    return "0x" + payload.hex()


def current_block_number(rpc: str) -> int:
    response = rpc_call(rpc, "getBlockNumber", [GROUP_ID, ""])
    result = response.get("result", 0)
    if isinstance(result, str):
        try:
            return int(result, 16) if result.startswith("0x") else int(result)
        except ValueError:
            return 0
    if isinstance(result, int):
        return result
    return 0


def hex_quantity(value: int) -> str:
    return hex(max(0, int(value)))


def recent_unique(items: list[str], limit: int) -> list[str]:
    picked: list[str] = []
    seen: set[str] = set()
    for item in reversed(items):
        if not item or item in seen:
            continue
        seen.add(item)
        picked.append(item)
        if len(picked) >= limit:
            break
    picked.reverse()
    return picked


def decode_address_output(output: str | None) -> str | None:
    if not output or not isinstance(output, str):
        return None
    try:
        raw = bytes.fromhex(output[2:] if output.startswith("0x") else output)
        decoded = decode(["address"], raw)[0]
    except Exception:
        return None
    if isinstance(decoded, bytes):
        return "0x" + decoded.hex()
    if isinstance(decoded, str):
        return decoded if decoded.startswith("0x") else "0x" + decoded
    return None


class TarsWriter:
    CHAR, SHORT, INT32, INT64, STRING1, STRING4 = 0, 1, 2, 3, 6, 7
    STRUCT_BEGIN, STRUCT_END, ZERO_TAG, SIMPLE_LIST = 10, 11, 12, 13

    def __init__(self) -> None:
        self.buf = bytearray()

    def head(self, tag: int, ttype: int) -> None:
        if tag < 15:
            self.buf.append((tag << 4) | ttype)
        else:
            self.buf.append(0xF0 | ttype)
            self.buf.append(tag)

    def write_int(self, tag: int, value: int) -> None:
        if value == 0:
            self.head(tag, self.ZERO_TAG)
        elif -128 <= value <= 127:
            self.head(tag, self.CHAR)
            self.buf.append(value & 0xFF)
        elif -32768 <= value <= 32767:
            self.head(tag, self.SHORT)
            self.buf += struct.pack(">h", value)
        else:
            self.head(tag, self.INT32)
            self.buf += struct.pack(">i", value)

    def write_long(self, tag: int, value: int) -> None:
        if -(2**31) <= value <= 2**31 - 1:
            self.write_int(tag, value)
        else:
            self.head(tag, self.INT64)
            self.buf += struct.pack(">q", value)

    def write_string(self, tag: int, value: str) -> None:
        data = value.encode()
        if len(data) <= 255:
            self.head(tag, self.STRING1)
            self.buf.append(len(data))
        else:
            self.head(tag, self.STRING4)
            self.buf += struct.pack(">I", len(data))
        self.buf += data

    def write_bytes(self, tag: int, value: bytes) -> None:
        self.head(tag, self.SIMPLE_LIST)
        self.head(0, self.CHAR)
        self.write_int(0, len(value))
        self.buf += value

    def write_byte(self, tag: int, value: int) -> None:
        if value == 0:
            self.head(tag, self.ZERO_TAG)
        else:
            self.head(tag, self.CHAR)
            self.buf.append(value)

    def end_struct(self) -> None:
        self.buf.append(self.STRUCT_END)


def build_transaction_data(
    *,
    to: str,
    input_data: bytes = b"",
    version: int = 1,
    block_limit: int = 1500,
    gas_limit: int = 300_000_000,
    nonce: str | None = None,
) -> bytes:
    tx_nonce = nonce or ("0x" + Account.create().address[2:16] + str(int(time.time() * 1000) % 100000))
    writer = TarsWriter()
    writer.write_int(1, version)
    writer.write_string(2, CHAIN_ID)
    writer.write_string(3, GROUP_ID)
    writer.write_long(4, block_limit)
    writer.write_string(5, tx_nonce)
    writer.write_string(6, to)
    if input_data:
        writer.write_bytes(7, input_data)
    writer.write_string(9, "0x0")
    writer.write_string(10, "0x0")
    writer.write_long(11, gas_limit)
    writer.write_string(12, "0x0")
    writer.write_string(13, "0x0")
    writer.end_struct()
    return bytes(writer.buf)


def calc_tx_hash(
    *,
    to: str,
    input_data: bytes = b"",
    version: int = 1,
    block_limit: int = 1500,
    gas_limit: int = 300_000_000,
    nonce: str,
) -> bytes:
    raw = bytearray()
    raw += struct.pack(">i", version)
    raw += CHAIN_ID.encode()
    raw += GROUP_ID.encode()
    raw += struct.pack(">q", block_limit)
    raw += nonce.encode()
    raw += to.encode()
    raw += input_data
    raw += b""
    raw += b"0x0"
    raw += b"0x0"
    raw += struct.pack(">q", gas_limit)
    raw += b"0x0"
    raw += b"0x0"
    return keccak(bytes(raw))


def build_native_transaction(
    private_key: bytes,
    *,
    to: str,
    input_data: bytes = b"",
    gas_limit: int = 300_000_000,
    block_limit: int = 1500,
) -> bytes:
    nonce = "0x" + Account.create().address[2:16] + str(int(time.time() * 1000) % 100000)
    tx_hash = calc_tx_hash(
        to=to,
        input_data=input_data,
        block_limit=block_limit,
        gas_limit=gas_limit,
        nonce=nonce,
    )
    signature = keys.PrivateKey(private_key).sign_msg_hash(tx_hash)
    recid = signature.v if signature.v <= 3 else signature.v - 27
    sig_bytes = signature.r.to_bytes(32, "big") + signature.s.to_bytes(32, "big") + bytes([recid])

    writer = TarsWriter()
    writer.head(1, TarsWriter.STRUCT_BEGIN)
    writer.buf += build_transaction_data(
        to=to,
        input_data=input_data,
        block_limit=block_limit,
        gas_limit=gas_limit,
        nonce=nonce,
    )
    writer.write_bytes(2, tx_hash)
    writer.write_bytes(3, sig_bytes)
    writer.write_byte(9, 0)
    writer.end_struct()
    return bytes(writer.buf)


def signed_send(
    rpc: str,
    account: LocalAccount,
    nonce_cache: dict[str, int],
    to: str,
    data: str,
    *,
    gas: int = GAS_LIMIT,
) -> dict:
    try:
        payload = bytes.fromhex(data[2:] if data.startswith("0x") else data)
        block_limit = current_block_number(rpc) + 500
        raw_tx = build_native_transaction(
            bytes(account.key),
            to=to,
            input_data=payload,
            gas_limit=gas,
            block_limit=block_limit,
        )
    except Exception:
        return {"ok": False, "tx_hash": None, "response": {"error": "encode"}}
    response = rpc_call(rpc, "sendTransaction", [GROUP_ID, "", "0x" + raw_tx.hex(), False])
    if "error" in response:
        return {"ok": False, "tx_hash": None, "response": response}
    result = response.get("result", {})
    status = result.get("status")
    tx_hash = result.get("transactionHash") if isinstance(result, dict) else None
    return {
        "ok": status in (0, "0x0", "0"),
        "tx_hash": tx_hash if isinstance(tx_hash, str) else None,
        "response": response,
    }


def signed_call(
    rpc: str,
    account: LocalAccount,
    nonce_cache: dict[str, int],
    to: str,
    data: str,
    tx_hashes: list[str] | None = None,
    *,
    gas: int = GAS_LIMIT,
) -> bool:
    outcome = signed_send(rpc, account, nonce_cache, to, data, gas=gas)
    if outcome["ok"] and tx_hashes is not None and outcome["tx_hash"]:
        tx_hashes.append(outcome["tx_hash"])
    return bool(outcome["ok"])


def eth_call_result(rpc: str, to: str, data: str) -> dict:
    response = rpc_call(rpc, "call", [GROUP_ID, "", to, data])
    if "error" in response:
        return {"ok": False, "output": None, "response": response}
    result = response.get("result", {})
    status = result.get("status")
    return {
        "ok": status in (None, 0, "0x0", "0"),
        "output": result.get("output") if isinstance(result, dict) else None,
        "response": response,
    }


def eth_call(rpc: str, to: str, data: str) -> bool:
    return bool(eth_call_result(rpc, to, data)["ok"])


def dag_transfer_wave(
    rpc: str,
    accounts: list[LocalAccount],
    nonce_cache: dict[str, int],
    tag: str,
    *,
    users: int,
    transfer_rounds: int,
    tx_hashes: list[str] | None = None,
) -> dict[str, int]:
    created = saved = transferred = drawn = queried = 0
    names = [f"{tag}_u{i:02d}" for i in range(users)]
    for idx, user in enumerate(names):
        acct = accounts[idx % len(accounts)]
        if signed_call(
            rpc,
            acct,
            nonce_cache,
            PRECOMPILED_DAG_TRANSFER,
            encode_call("userAdd(string,uint256)", ["string", "uint256"], [user, 1000 + idx * 17]),
            tx_hashes=tx_hashes,
        ):
            created += 1
        if idx % 6 == 5:
            time.sleep(1)
    for idx in range(users * 2):
        acct = accounts[idx % len(accounts)]
        user = names[idx % len(names)]
        if signed_call(
            rpc,
            acct,
            nonce_cache,
            PRECOMPILED_DAG_TRANSFER,
            encode_call("userSave(string,uint256)", ["string", "uint256"], [user, 20 + (idx % 11)]),
            tx_hashes=tx_hashes,
        ):
            saved += 1
        if idx % 10 == 9:
            time.sleep(1)
    for idx in range(transfer_rounds):
        acct = accounts[idx % len(accounts)]
        src = names[idx % len(names)]
        dst = names[(idx * 7 + 3) % len(names)]
        if src == dst:
            dst = names[(idx + 1) % len(names)]
        if signed_call(
            rpc,
            acct,
            nonce_cache,
            PRECOMPILED_DAG_TRANSFER,
            encode_call(
                "userTransfer(string,string,uint256)",
                ["string", "string", "uint256"],
                [src, dst, 1 + (idx % 5)],
            ),
            tx_hashes=tx_hashes,
        ):
            transferred += 1
        if idx % 24 == 23:
            time.sleep(1)
    for idx in range(max(8, users // 2)):
        acct = accounts[idx % len(accounts)]
        user = names[(idx * 3) % len(names)]
        if signed_call(
            rpc,
            acct,
            nonce_cache,
            PRECOMPILED_DAG_TRANSFER,
            encode_call("userDraw(string,uint256)", ["string", "uint256"], [user, 1 + (idx % 3)]),
            tx_hashes=tx_hashes,
        ):
            drawn += 1
    time.sleep(2)
    for idx, user in enumerate(names[: min(len(names), 8)]):
        if eth_call(
            rpc,
            PRECOMPILED_DAG_TRANSFER,
            encode_call("userBalance(string)", ["string"], [user]),
        ):
            queried += 1
    return {
        "dag_adds": created,
        "dag_saves": saved,
        "dag_transfers": transferred,
        "dag_draws": drawn,
        "dag_queries": queried,
    }


def bfs_wave(
    rpc: str,
    accounts: list[LocalAccount],
    nonce_cache: dict[str, int],
    tag: str,
    tx_hashes: list[str] | None = None,
) -> dict[str, int]:
    mkdir_ok = link_ok = read_ok = 0
    base = f"/apps/bcfuzz_{tag}"
    dirs = [base, f"{base}/m0", f"{base}/m1", f"{base}/m2", f"{base}/m2/sub0", f"{base}/m2/sub1"]
    for idx, path in enumerate(dirs):
        acct = accounts[idx % len(accounts)]
        if signed_call(
            rpc,
            acct,
            nonce_cache,
            PRECOMPILED_BFS,
            encode_call("mkdir(string)", ["string"], [path]),
            tx_hashes=tx_hashes,
        ):
            mkdir_ok += 1
    for idx in range(6):
        acct = accounts[idx % len(accounts)]
        link_path = f"{base}/m{idx % 3}/link{idx}"
        target = PRECOMPILED_DAG_TRANSFER if idx % 2 == 0 else PRECOMPILED_SYS_CONFIG
        if signed_call(
            rpc,
            acct,
            nonce_cache,
            PRECOMPILED_BFS,
            encode_call("link(string,string,string)", ["string", "string", "string"], [link_path, target, ""]),
            tx_hashes=tx_hashes,
        ):
            link_ok += 1
    time.sleep(2)
    for idx in range(6):
        link_path = f"{base}/m{idx % 3}/link{idx}"
        if eth_call(
            rpc,
            PRECOMPILED_BFS,
            encode_call("readlink(string)", ["string"], [link_path]),
        ):
            read_ok += 1
    return {"bfs_mkdirs": mkdir_ok, "bfs_links": link_ok, "bfs_reads": read_ok}


def system_config_wave(
    rpc: str,
    accounts: list[LocalAccount],
    nonce_cache: dict[str, int],
    tx_hashes: list[str] | None = None,
) -> dict[str, int]:
    writes = reads = 0
    operations = [
        ("tx_count_limit", "1000"),
        ("tx_count_limit", "1001"),
        ("consensus_leader_period", "1"),
        ("consensus_leader_period", "2"),
        ("feature_balance", "1"),
        ("feature_balance_precompiled", "1"),
        ("auth_check_status", "0"),
        ("tx_count_limit", "1002"),
    ]
    for idx, (key, value) in enumerate(operations):
        acct = accounts[idx % len(accounts)]
        if signed_call(
            rpc,
            acct,
            nonce_cache,
            PRECOMPILED_SYS_CONFIG,
            encode_call("setValueByKey(string,string)", ["string", "string"], [key, value]),
            tx_hashes=tx_hashes,
        ):
            writes += 1
    time.sleep(2)
    for key in (
        "tx_count_limit",
        "consensus_leader_period",
        "auth_check_status",
        "feature_balance",
        "feature_balance_precompiled",
    ):
        if eth_call(
            rpc,
            PRECOMPILED_SYS_CONFIG,
            encode_call("getValueByKey(string)", ["string"], [key]),
        ):
            reads += 1
    return {"syscfg_writes": writes, "syscfg_reads": reads}


def bfs_extended_wave(
    rpc: str,
    accounts: list[LocalAccount],
    nonce_cache: dict[str, int],
    tag: str,
    *,
    tx_hashes: list[str] | None = None,
) -> dict[str, int]:
    touches = lists = page_lists = rebuilds = 0
    base = f"/apps/bcfuzz_{tag}"
    for idx in range(4):
        acct = accounts[idx % len(accounts)]
        target = f"{base}/m{idx % 3}/touch{idx}"
        if signed_call(
            rpc,
            acct,
            nonce_cache,
            PRECOMPILED_BFS,
            encode_call("touch(string,string)", ["string", "string"], [target, "contract"]),
            tx_hashes=tx_hashes,
        ):
            touches += 1
    time.sleep(1)
    for path in (base, f"{base}/m2", f"{base}/m2/sub0"):
        if eth_call(
            rpc,
            PRECOMPILED_BFS,
            encode_call("list(string)", ["string"], [path]),
        ):
            lists += 1
        if eth_call(
            rpc,
            PRECOMPILED_BFS,
            encode_call("list(string,uint256,uint256)", ["string", "uint256", "uint256"], [path, 0, 8]),
        ):
            page_lists += 1
    for from_version, to_version in ((0, 0), (1, 1)):
        if signed_call(
            rpc,
            accounts[(from_version + to_version) % len(accounts)],
            nonce_cache,
            PRECOMPILED_BFS,
            encode_call("rebuildBfs(uint256,uint256)", ["uint256", "uint256"], [from_version, to_version]),
            tx_hashes=tx_hashes,
        ):
            rebuilds += 1
    return {
        "bfs_touches": touches,
        "bfs_lists": lists,
        "bfs_page_lists": page_lists,
        "bfs_rebuilds": rebuilds,
    }


def balance_wave(
    rpc: str,
    accounts: list[LocalAccount],
    nonce_cache: dict[str, int],
    *,
    tx_hashes: list[str] | None = None,
) -> dict[str, int]:
    writes = reads = 0
    a0 = accounts[0].address
    a1 = accounts[1].address
    operations = [
        ("registerCaller(address)", ["address"], [a0]),
        ("addBalance(address,uint256)", ["address", "uint256"], [a0, 1000]),
        ("addBalance(address,uint256)", ["address", "uint256"], [a1, 700]),
        ("transfer(address,address,uint256)", ["address", "address", "uint256"], [a0, a1, 123]),
        ("subBalance(address,uint256)", ["address", "uint256"], [a1, 17]),
        ("unregisterCaller(address)", ["address"], [a0]),
    ]
    for idx, (signature, arg_types, args) in enumerate(operations):
        acct = accounts[idx % len(accounts)]
        if signed_call(
            rpc,
            acct,
            nonce_cache,
            PRECOMPILED_BALANCE,
            encode_call(signature, arg_types, args),
            tx_hashes=tx_hashes,
        ):
            writes += 1
    for address in (a0, a1):
        if eth_call(
            rpc,
            PRECOMPILED_BALANCE,
            encode_call("getBalance(address)", ["address"], [address]),
        ):
            reads += 1
    if eth_call(rpc, PRECOMPILED_BALANCE, encode_call("listCaller()", [], [])):
        reads += 1
    return {"balance_writes": writes, "balance_reads": reads}


def auth_wave(
    rpc: str,
    accounts: list[LocalAccount],
    nonce_cache: dict[str, int],
    *,
    tx_hashes: list[str] | None = None,
) -> dict[str, int]:
    writes = reads = 0
    target = PRECOMPILED_DAG_TRANSFER
    subject = accounts[2].address
    selector = keccak(text="userSave(string,uint256)")[:4]
    write_ops = [
        ("setDeployAuthType(uint8)", ["uint8"], [1]),
        ("openDeployAuth(address)", ["address"], [subject]),
        ("closeDeployAuth(address)", ["address"], [subject]),
        ("setMethodAuthType(address,bytes4,uint8)", ["address", "bytes4", "uint8"], [target, selector, 1]),
        ("openMethodAuth(address,bytes4,address)", ["address", "bytes4", "address"], [target, selector, subject]),
        ("closeMethodAuth(address,bytes4,address)", ["address", "bytes4", "address"], [target, selector, subject]),
        ("setContractStatus(address,bool)", ["address", "bool"], [target, False]),
        ("setContractStatus(address,uint8)", ["address", "uint8"], [target, 0]),
    ]
    for idx, (signature, arg_types, args) in enumerate(write_ops):
        acct = accounts[idx % len(accounts)]
        if signed_call(
            rpc,
            acct,
            nonce_cache,
            PRECOMPILED_AUTH,
            encode_call(signature, arg_types, args),
            tx_hashes=tx_hashes,
        ):
            writes += 1
    read_ops = [
        ("deployType()", [], []),
        ("hasDeployAuth(address)", ["address"], [subject]),
        ("contractAvailable(address)", ["address"], [target]),
        ("getAdmin(address)", ["address"], [target]),
        ("checkMethodAuth(address,bytes4,address)", ["address", "bytes4", "address"], [target, selector, subject]),
        ("getMethodAuth(address,bytes4)", ["address", "bytes4"], [target, selector]),
    ]
    for signature, arg_types, args in read_ops:
        if eth_call(rpc, PRECOMPILED_AUTH, encode_call(signature, arg_types, args)):
            reads += 1
    return {"auth_writes": writes, "auth_reads": reads}


def table_manager_wave(
    rpc: str,
    accounts: list[LocalAccount],
    nonce_cache: dict[str, int],
    tag: str,
    *,
    tx_hashes: list[str] | None = None,
) -> tuple[dict[str, int], str | None]:
    writes = reads = 0
    table_name = f"bcfuzz_kv_{tag}"
    create_ops = [
        ("createKVTable(string,string,string)", ["string", "string", "string"], [table_name, "id", "value"]),
        ("appendColumns(string,string[])", ["string", "string[]"], [table_name, ["extra0", "extra1"]]),
    ]
    for idx, (signature, arg_types, args) in enumerate(create_ops):
        acct = accounts[idx % len(accounts)]
        if signed_call(
            rpc,
            acct,
            nonce_cache,
            PRECOMPILED_TABLE_MANAGER,
            encode_call(signature, arg_types, args),
            tx_hashes=tx_hashes,
        ):
            writes += 1
    if eth_call(
        rpc,
        PRECOMPILED_TABLE_MANAGER,
        encode_call("descWithKeyOrder(string)", ["string"], [table_name]),
    ):
        reads += 1
    opened = eth_call_result(
        rpc,
        PRECOMPILED_TABLE_MANAGER,
        encode_call("openTable(string)", ["string"], [table_name]),
    )
    table_address = decode_address_output(opened["output"])
    if table_address:
        reads += 1
        for idx, (key, value) in enumerate(
            [("k0", f"{tag}_v0"), ("k1", f"{tag}_v1"), ("k2", f"{tag}_v2")]
        ):
            if signed_call(
                rpc,
                accounts[idx % len(accounts)],
                nonce_cache,
                table_address,
                encode_call("set(string,string)", ["string", "string"], [key, value]),
                tx_hashes=tx_hashes,
            ):
                writes += 1
        for key in ("k0", "k1", "missing"):
            if eth_call(
                rpc,
                table_address,
                encode_call("get(string)", ["string"], [key]),
            ):
                reads += 1
    return {"table_writes": writes, "table_reads": reads}, table_address


def build_network(runtime: Path) -> Path:
    nodes = runtime / "nodes"
    if (nodes / "127.0.0.1").exists():
        shutil.rmtree(nodes)
    p2p_start = P2P_BASE + PORT_OFFSET
    rpc_start = RPC_BASE + PORT_OFFSET
    subprocess.run(
        ["bash", str(BUILD_CHAIN), "-p", f"{p2p_start},{rpc_start}", "-l", "127.0.0.1:4",
         "-o", str(nodes), "-e", str(PEER_NODE_BIN)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=300)
    net_dir = nodes / "127.0.0.1"
    for node in range(4):
        node_dir = net_dir / f"node{node}"
        cov_link = node_dir / "fisco-bcos-cov"
        cov_link.unlink(missing_ok=True)
        cov_link.symlink_to(cov_node_bin())
        for script_name in ("start.sh", "stop.sh"):
            script = node_dir / script_name
            text = script.read_text(encoding="utf-8")
            text = re.sub(r"^fisco_bcos=.*$", f"fisco_bcos={cov_link}",
                          text, flags=re.MULTILINE)
            if script_name == "start.sh":
                if "GCOV_PREFIX=" not in text:
                    text = text.replace(
                        "cd ${SHELL_FOLDER}\n",
                        "cd ${SHELL_FOLDER}\n" + coverage_env_exports(),
                        1,
                    )
            script.write_text(text, encoding="utf-8")
    return net_dir


def configure_rpc_tls(net_dir: Path) -> None:
    global RPC_CERT
    sdk_dir = net_dir / "sdk"
    sdk_crt = sdk_dir / "sdk.crt"
    sdk_key = sdk_dir / "sdk.key"
    if sdk_crt.is_file() and sdk_key.is_file():
        RPC_CERT = (str(sdk_crt), str(sdk_key))
    else:
        RPC_CERT = None


def overlay_case_config(node_dir: Path, case: str, seed: int) -> None:
    """Overlay the BCFuzzer config model's values onto node0's config.ini."""
    run_dir = Path("/tmp") / f"fisco-case-{seed}-{os.getpid()}"
    run_dir.mkdir(exist_ok=True)
    meta = apply_case("fisco", ROOT, case, run_dir, seed)
    model = Path(meta["output"])
    target = node_dir / "node0" / "config.ini"
    sections: dict[str, dict[str, str]] = {}
    cur = None
    for line in model.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
            sections.setdefault(cur, {})
        elif "=" in line and cur:
            k, v = line.split("=", 1)
            sections[cur][k.strip()] = v.strip()
    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()
    out = []
    cur = None
    consumed = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            cur = stripped[1:-1]
            out.append(line)
        elif "=" in line and cur in sections and cur not in consumed:
            k = stripped.split("=", 1)[0].strip()
            if k in sections[cur]:
                out.append(f"{k} = {sections[cur][k]}")
            else:
                out.append(line)
        else:
            out.append(line)
    text = "\n".join(out) + "\n"
    # append any model sections missing from the node config
    cur = None
    have = set()
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            have.add(s[1:-1])
    for section, kv in sections.items():
        if section not in have:
            text += f"\n[{section}]\n"
            for k, v in kv.items():
                text += f"{k} = {v}\n"
    target.write_text(text, encoding="utf-8")


def mutate_node_config(node_dir: Path, tool: str, seed: int) -> int:
    if tool in ("fixed", "varied"):
        return 0
    cfg = node_dir / "node0" / "config.ini"
    from campaign_baseline_real import mutate_with_tool
    mutated = mutate_with_tool(
        cfg, tool, seed, target="fisco",
        eligible_sections=FISCO_BASELINE_SECTIONS)
    sanitize_fisco_baseline_config(cfg)
    return mutated


def _bounded_int(value: str, low: int, high: int, default: int) -> str:
    raw = value.strip().strip('"').strip("'")
    try:
        number = int(raw, 0)
    except (TypeError, ValueError):
        number = default
    return str(max(low, min(high, number)))


def _bool_value(value: str, default: str) -> str:
    text = value.strip().strip('"').strip("'").lower()
    if text in ("true", "1", "yes", "y", "on"):
        return "true"
    if text in ("false", "0", "no", "n", "off"):
        return "false"
    return default


def sanitize_fisco_baseline_config(config: Path) -> int:
    from config_mutators import parse_lines

    text = config.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    changed = 0
    for item in parse_lines(text):
        key = (item["section"], item["key"])
        if key in FISCO_INT_BOUNDS:
            low, high, default = FISCO_INT_BOUNDS[key]
            new_value = _bounded_int(item["value"], low, high, default)
        elif key in FISCO_BOOL_DEFAULTS:
            new_value = _bool_value(item["value"], FISCO_BOOL_DEFAULTS[key])
        else:
            continue
        new_line = f"{item['indent']}{item['key']} {item['sep']} {new_value}"
        if item["line"] < len(lines) and lines[item["line"]] != new_line:
            lines[item["line"]] = new_line
            changed += 1
    if changed:
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def kill_stale_fisco_processes(runtime: Path | None = None) -> None:
    victims: list[int] = []
    runtime_text = str(runtime) if runtime is not None else None
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_text(errors="replace").replace("\x00", " ")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if "/tmp/fisco-live-" not in cmdline:
            continue
        if runtime_text is not None and runtime_text not in cmdline:
            continue
        if "fisco-bcos-cov" not in cmdline and "fisco-bcos-air/fisco-bcos" not in cmdline:
            continue
        victims.append(int(proc.name))
    for pid in victims:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 15
    while victims and time.monotonic() < deadline:
        victims = [pid for pid in victims if Path(f"/proc/{pid}").exists()]
        if victims:
            time.sleep(0.2)
    for pid in victims:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def start_chain(net_dir: Path) -> bool:
    started = subprocess.run(
        ["bash", "start_all.sh"], cwd=net_dir, timeout=120,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if started.returncode != 0:
        return False
    # wait for the four nodes + consensus (reachNewView)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        ok = 0
        for node in range(4):
            logs = list((net_dir / f"node{node}" / "log").glob("log*"))
            if logs and any("reachNewView" in l.read_text(errors="replace")
                            for l in logs):
                ok += 1
        if ok >= 4:
            return True
        time.sleep(3)
    print("WARN: consensus not reached in time", flush=True)
    return False


def stop_chain(net_dir: Path) -> None:
    try:
        subprocess.run(["bash", "stop_all.sh"], cwd=net_dir, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    terminate_pids(net_dir, sig=15, wait_seconds=12)
    terminate_pids(net_dir, sig=15, wait_seconds=15)
    terminate_pids(net_dir, sig=9, wait_seconds=10)


def stop_node(net_dir: Path, node: int) -> None:
    node_dir = net_dir / f"node{node}"
    try:
        subprocess.run(["bash", "stop.sh"], cwd=node_dir, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    terminate_pids(node_dir, sig=15, wait_seconds=10)
    terminate_pids(node_dir, sig=9, wait_seconds=5)


def start_node(net_dir: Path, node: int) -> bool:
    node_dir = net_dir / f"node{node}"
    subprocess.run(["bash", "start.sh"], cwd=node_dir, timeout=120,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        logs = sorted((node_dir / "log").glob("log*"))
        if logs and any("reachNewView" in p.read_text(errors="replace") for p in logs[-1:]):
            return True
        time.sleep(1)
    return False


def pids_under(path: Path) -> list[int]:
    needle = str(path).encode()
    pids = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if needle in cmdline:
            pids.append(int(proc.name))
    return pids


def terminate_pids(path: Path, *, sig: int, wait_seconds: float) -> None:
    victims = pids_under(path)
    for pid in victims:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not pids_under(path):
            return
        time.sleep(0.2)


def rpc_for_node(node: int) -> str:
    return f"https://127.0.0.1:{RPC_BASE + PORT_OFFSET + node}"


def simple_transfer_wave(
    rpc: str,
    accounts: list[LocalAccount],
    nonce_cache: dict[str, int],
    count: int,
    *,
    interval_ms: int = 5,
    tx_hashes: list[str] | None = None,
) -> int:
    sent = 0
    for idx in range(count):
        acct = accounts[idx % len(accounts)]
        target = Account.create().address
        payload = "0x"
        if idx % 3 == 1:
            payload = "0x" + (bytes([idx % 251]) * 8).hex()
        elif idx % 3 == 2:
            payload = "0x" + bytes(((idx + j) % 251 for j in range(24))).hex()
        if signed_call(rpc, acct, nonce_cache, target, payload, tx_hashes=tx_hashes):
            sent += 1
        time.sleep(interval_ms / 1000.0)
    return sent


def native_rpc_query_sweep(
    rpc: str,
    *,
    focus_addresses: list[str],
    tx_hashes: list[str],
) -> dict[str, int]:
    queries = 0

    def call_native(method: str, params: list):
        nonlocal queries
        response = rpc_call(rpc, method, params)
        if rpc_result_ok(response):
            queries += 1
            return response.get("result")
        return None

    block_number = current_block_number(rpc)
    target_block = max(0, block_number - 1)
    call_native("getGroupList", [])
    call_native("getGroupInfoList", [])
    call_native("getGroupInfo", [GROUP_ID])
    call_native("getGroupPeers", [GROUP_ID])
    call_native("getPeers", [])
    call_native("getGroupNodeInfo", [GROUP_ID, ""])
    call_native("getBlockNumber", [GROUP_ID, ""])
    call_native("getSealerList", [GROUP_ID, ""])
    call_native("getObserverList", [GROUP_ID, ""])
    call_native("getNodeListByType", [GROUP_ID, "", "sealer"])
    call_native("getNodeListByType", [GROUP_ID, "", "observer"])
    call_native("getPbftView", [GROUP_ID, ""])
    call_native("getPendingTxSize", [GROUP_ID, ""])
    call_native("getSyncStatus", [GROUP_ID, ""])
    call_native("getConsensusStatus", [GROUP_ID, ""])
    call_native("getTotalTransactionCount", [GROUP_ID, ""])
    for key in (
        "tx_count_limit",
        "consensus_leader_period",
        "auth_check_status",
        "feature_balance",
        "feature_balance_precompiled",
    ):
        call_native("getSystemConfigByKey", [GROUP_ID, "", key])
    call_native("getBlockByNumber", [GROUP_ID, "", target_block, False, False])
    call_native("getBlockByNumber", [GROUP_ID, "", target_block, True, True])
    block_hash = call_native("getBlockHashByNumber", [GROUP_ID, "", target_block])
    if isinstance(block_hash, str):
        call_native("getBlockByHash", [GROUP_ID, "", block_hash, False, True])
    for address in focus_addresses:
        call_native("getCode", [GROUP_ID, "", address])
        call_native("getABI", [GROUP_ID, "", address])
    for tx_hash in recent_unique(tx_hashes, 6):
        call_native("getTransaction", [GROUP_ID, "", tx_hash, False])
        call_native("getTransaction", [GROUP_ID, "", tx_hash, True])
        call_native("getTransactionReceipt", [GROUP_ID, "", tx_hash, False])
        call_native("getTransactionReceipt", [GROUP_ID, "", tx_hash, True])
    return {"native_rpc_queries": queries}


def native_filter_wave(
    rpc: str,
    accounts: list[LocalAccount],
    nonce_cache: dict[str, int],
    *,
    tx_hashes: list[str],
) -> tuple[dict[str, int], int]:
    queries = 0

    def call_filter(method: str, params: list):
        nonlocal queries
        response = rpc_call(rpc, method, params)
        if rpc_result_ok(response):
            queries += 1
            return response.get("result")
        return None

    block_filter = call_filter("newBlockFilter", [GROUP_ID])
    pending_filter = call_filter("newPendingTransactionFilter", [GROUP_ID])
    latest_block = current_block_number(rpc)
    log_params = {
        "fromBlock": hex_quantity(max(0, latest_block - 8)),
        "toBlock": "latest",
        "address": PRECOMPILED_DAG_TRANSFER,
        "topics": [],
    }
    log_filter = call_filter(
        "newFilter",
        [GROUP_ID, log_params],
    )
    trigger_txs = simple_transfer_wave(
        rpc,
        accounts,
        nonce_cache,
        10,
        interval_ms=1,
        tx_hashes=tx_hashes,
    )
    time.sleep(2)
    for filter_id in (block_filter, pending_filter, log_filter):
        if isinstance(filter_id, str):
            call_filter("getFilterChanges", [GROUP_ID, filter_id])
    if isinstance(log_filter, str):
        call_filter("getFilterLogs", [GROUP_ID, log_filter])
        call_filter("getLogs", [GROUP_ID, log_params])
    for filter_id in (block_filter, pending_filter, log_filter):
        if isinstance(filter_id, str):
            call_filter("uninstallFilter", [GROUP_ID, filter_id])
    return {"native_filter_queries": queries}, trigger_txs


def submitted_from_activity(activity: dict[str, int]) -> int:
    return sum(activity.get(key, 0) for key in WRITE_ACTIVITY_KEYS)


def interact(arm: str, net_dir: Path, *, round_deadline: float | None = None) -> dict:
    if arm == "fixed":
        return {
            "tx_batches": 0,
            "submitted_transactions": 0,
            "restart_sync_ok": False,
            "offline_window_submissions": 0,
            "dag_adds": 0,
            "dag_saves": 0,
            "dag_transfers": 0,
            "dag_draws": 0,
            "dag_queries": 0,
            "bfs_mkdirs": 0,
            "bfs_links": 0,
            "bfs_reads": 0,
            "bfs_touches": 0,
            "bfs_lists": 0,
            "bfs_page_lists": 0,
            "bfs_rebuilds": 0,
            "syscfg_writes": 0,
            "syscfg_reads": 0,
            "balance_writes": 0,
            "balance_reads": 0,
            "auth_writes": 0,
            "auth_reads": 0,
            "table_writes": 0,
            "table_reads": 0,
            "native_rpc_queries": 0,
            "native_filter_queries": 0,
        }

    accounts = [Account.create() for _ in range(6)]
    nonce_cache: dict[str, int] = {}
    tx_hashes: list[str] = []
    activity = {
        "dag_adds": 0,
        "dag_saves": 0,
        "dag_transfers": 0,
        "dag_draws": 0,
        "dag_queries": 0,
        "bfs_mkdirs": 0,
        "bfs_links": 0,
        "bfs_reads": 0,
        "bfs_touches": 0,
        "bfs_lists": 0,
        "bfs_page_lists": 0,
        "bfs_rebuilds": 0,
        "syscfg_writes": 0,
        "syscfg_reads": 0,
        "balance_writes": 0,
        "balance_reads": 0,
        "auth_writes": 0,
        "auth_reads": 0,
        "table_writes": 0,
        "table_reads": 0,
        "native_rpc_queries": 0,
        "native_filter_queries": 0,
    }

    def absorb(extra: dict[str, int]) -> None:
        for key, value in extra.items():
            activity[key] += value

    def submission_delta(extra: dict[str, int]) -> int:
        return sum(extra.get(key, 0) for key in WRITE_ACTIVITY_KEYS)

    def expired(slack_seconds: float = 0.0) -> bool:
        return round_deadline is not None and time.monotonic() >= (round_deadline - slack_seconds)

    def focus_addresses(*extra: str | None) -> list[str]:
        items = [
            accounts[0].address,
            accounts[1].address,
            PRECOMPILED_SYS_CONFIG,
            PRECOMPILED_TABLE_MANAGER,
            PRECOMPILED_AUTH,
            PRECOMPILED_DAG_TRANSFER,
            PRECOMPILED_BFS,
            PRECOMPILED_BALANCE,
        ]
        for value in extra:
            if value:
                items.append(value)
        return items

    offline_total = 0
    restart_ok = True
    batch_total = 0
    submitted_total = 0

    def summary() -> dict:
        return {
            "tx_batches": batch_total,
            "submitted_transactions": submitted_total,
            "restart_sync_ok": restart_ok,
            "offline_window_submissions": offline_total,
            **activity,
        }

    driver_rpc = rpc_for_node(1)
    extra = dag_transfer_wave(
        driver_rpc, accounts, nonce_cache, "online0", users=14, transfer_rounds=96, tx_hashes=tx_hashes)
    absorb(extra)
    submitted_total += submission_delta(extra)
    batch_total += 1
    if expired():
        return summary()
    extra = bfs_wave(driver_rpc, accounts, nonce_cache, "online0", tx_hashes=tx_hashes)
    absorb(extra)
    submitted_total += submission_delta(extra)
    batch_total += 1
    if expired():
        return summary()
    extra = bfs_extended_wave(driver_rpc, accounts, nonce_cache, "online0", tx_hashes=tx_hashes)
    absorb(extra)
    submitted_total += submission_delta(extra)
    batch_total += 1
    if expired():
        return summary()
    extra = system_config_wave(driver_rpc, accounts, nonce_cache, tx_hashes=tx_hashes)
    absorb(extra)
    submitted_total += submission_delta(extra)
    batch_total += 1
    if expired():
        return summary()
    extra = balance_wave(driver_rpc, accounts, nonce_cache, tx_hashes=tx_hashes)
    absorb(extra)
    submitted_total += submission_delta(extra)
    batch_total += 1
    if expired():
        return summary()
    extra = auth_wave(driver_rpc, accounts, nonce_cache, tx_hashes=tx_hashes)
    absorb(extra)
    submitted_total += submission_delta(extra)
    batch_total += 1
    table_extra, table_addr = table_manager_wave(
        driver_rpc, accounts, nonce_cache, "online0", tx_hashes=tx_hashes)
    absorb(table_extra)
    submitted_total += submission_delta(table_extra)
    batch_total += 1
    if expired():
        return summary()
    filter_extra, filter_trigger_txs = native_filter_wave(
        driver_rpc, accounts, nonce_cache, tx_hashes=tx_hashes)
    absorb(filter_extra)
    batch_total += 1
    absorb(native_rpc_query_sweep(
        driver_rpc, focus_addresses=focus_addresses(table_addr), tx_hashes=tx_hashes))
    batch_total += 1
    if expired():
        return summary()
    online_simple = simple_transfer_wave(
        driver_rpc, accounts, nonce_cache, 180, interval_ms=3, tx_hashes=tx_hashes)
    submitted_total += online_simple + filter_trigger_txs
    batch_total += 1
    time.sleep(2)
    if expired():
        return summary()
    extra = dag_transfer_wave(
        driver_rpc, accounts, nonce_cache, "online1", users=10, transfer_rounds=64, tx_hashes=tx_hashes)
    absorb(extra)
    submitted_total += submission_delta(extra)
    batch_total += 1
    absorb(native_rpc_query_sweep(
        driver_rpc, focus_addresses=focus_addresses(table_addr), tx_hashes=tx_hashes))
    batch_total += 1
    if expired():
        return summary()

    def offline_cycle(peer_wave: list[int], tag: str) -> None:
        nonlocal offline_total, restart_ok, batch_total, submitted_total
        if expired(20.0):
            return
        stop_node(net_dir, 0)
        time.sleep(2)
        for idx, peer in enumerate(peer_wave):
            if expired(20.0):
                break
            rpc = rpc_for_node(peer)
            peer_nonce_cache: dict[str, int] = {}
            extended_extra = {"bfs_touches": 0, "bfs_rebuilds": 0}
            balance_extra = {"balance_writes": 0}
            auth_extra = {"auth_writes": 0}
            table_extra_local = {"table_writes": 0}
            extra = dag_transfer_wave(
                rpc,
                accounts,
                peer_nonce_cache,
                f"{tag}_peer{peer}",
                users=10 + idx,
                transfer_rounds=72 + idx * 16,
                tx_hashes=tx_hashes,
            )
            absorb(extra)
            bfs_extra = bfs_wave(rpc, accounts, peer_nonce_cache, f"{tag}_peer{peer}", tx_hashes=tx_hashes)
            absorb(bfs_extra)
            if idx == 0:
                extended_extra = bfs_extended_wave(
                    rpc, accounts, peer_nonce_cache, f"{tag}_peer{peer}", tx_hashes=tx_hashes)
                absorb(extended_extra)
            cfg_extra = system_config_wave(rpc, accounts, peer_nonce_cache, tx_hashes=tx_hashes)
            absorb(cfg_extra)
            table_addr_local = None
            if idx == 0:
                balance_extra = balance_wave(rpc, accounts, peer_nonce_cache, tx_hashes=tx_hashes)
                absorb(balance_extra)
                auth_extra = auth_wave(rpc, accounts, peer_nonce_cache, tx_hashes=tx_hashes)
                absorb(auth_extra)
                table_extra_local, table_addr_local = table_manager_wave(
                    rpc, accounts, peer_nonce_cache, f"{tag}_peer{peer}", tx_hashes=tx_hashes)
                absorb(table_extra_local)
            send_count = 140 + idx * 40
            simple_sent = simple_transfer_wave(
                rpc, accounts, peer_nonce_cache, send_count, interval_ms=2, tx_hashes=tx_hashes)
            if idx == 0:
                filter_extra_local, filter_trigger_local = native_filter_wave(
                    rpc, accounts, peer_nonce_cache, tx_hashes=tx_hashes)
                absorb(filter_extra_local)
            else:
                filter_trigger_local = 0
            absorb(native_rpc_query_sweep(
                rpc, focus_addresses=focus_addresses(table_addr_local), tx_hashes=tx_hashes))
            offline_delta = (
                extra["dag_adds"] + extra["dag_saves"] + extra["dag_transfers"] + extra["dag_draws"] +
                bfs_extra["bfs_mkdirs"] + bfs_extra["bfs_links"] + cfg_extra["syscfg_writes"] + simple_sent +
                filter_trigger_local
            )
            if idx == 0:
                offline_delta += (
                    extended_extra["bfs_touches"] + extended_extra["bfs_rebuilds"] +
                    balance_extra["balance_writes"] + auth_extra["auth_writes"] + table_extra_local["table_writes"]
                )
            offline_total += offline_delta
            submitted_total += offline_delta
            batch_total += 8 if idx == 0 else 5
            time.sleep(2)
            if expired(15.0):
                break
        restarted = start_node(net_dir, 0)
        restart_ok = restart_ok and restarted
        time.sleep(10)
        post_nonce_cache: dict[str, int] = {}
        for peer in (1, 2, 1):
            if expired(15.0):
                break
            rpc = rpc_for_node(peer)
            extra = dag_transfer_wave(
                rpc, accounts, post_nonce_cache, f"{tag}_post{peer}", users=8, transfer_rounds=56, tx_hashes=tx_hashes)
            absorb(extra)
            if peer == 1:
                absorb(native_rpc_query_sweep(
                    rpc, focus_addresses=focus_addresses(), tx_hashes=tx_hashes))
            send_count = 180 if peer == 1 else 120
            simple_sent = simple_transfer_wave(
                rpc, accounts, post_nonce_cache, send_count, interval_ms=2, tx_hashes=tx_hashes)
            submitted_total += (
                extra["dag_adds"] + extra["dag_saves"] + extra["dag_transfers"] + extra["dag_draws"] + simple_sent
            )
            batch_total += 3 if peer == 1 else 2
            time.sleep(2)

    offline_cycle([1, 2, 3], "offline0")
    if expired():
        return summary()
    offline_cycle([2, 3, 1], "offline1")
    if expired():
        return summary()

    extension_cycle = 0
    while not expired(20.0):
        rpc = rpc_for_node(1 + (extension_cycle % 3))
        peer_nonce_cache: dict[str, int] = {}
        tag = f"sustain{extension_cycle}"
        extra = dag_transfer_wave(
            rpc,
            accounts,
            peer_nonce_cache,
            tag,
            users=8 + (extension_cycle % 4),
            transfer_rounds=48 + (extension_cycle % 3) * 8,
            tx_hashes=tx_hashes,
        )
        absorb(extra)
        submitted_total += submission_delta(extra)
        batch_total += 1
        if expired(12.0):
            break

        extra = bfs_wave(rpc, accounts, peer_nonce_cache, tag, tx_hashes=tx_hashes)
        absorb(extra)
        submitted_total += submission_delta(extra)
        batch_total += 1
        if extension_cycle % 2 == 0:
            extra = system_config_wave(rpc, accounts, peer_nonce_cache, tx_hashes=tx_hashes)
            absorb(extra)
            submitted_total += submission_delta(extra)
            batch_total += 1
        if extension_cycle % 3 == 1:
            table_extra_local, table_addr_local = table_manager_wave(
                rpc, accounts, peer_nonce_cache, tag, tx_hashes=tx_hashes)
            absorb(table_extra_local)
            submitted_total += submission_delta(table_extra_local)
            batch_total += 1
            focus = focus_addresses(table_addr_local)
        else:
            focus = focus_addresses()
        if extension_cycle % 3 == 2:
            extra = auth_wave(rpc, accounts, peer_nonce_cache, tx_hashes=tx_hashes)
            absorb(extra)
            submitted_total += submission_delta(extra)
            batch_total += 1
        simple_sent = simple_transfer_wave(
            rpc,
            accounts,
            peer_nonce_cache,
            120 + (extension_cycle % 3) * 30,
            interval_ms=2,
            tx_hashes=tx_hashes,
        )
        submitted_total += simple_sent
        batch_total += 1
        absorb(native_rpc_query_sweep(rpc, focus_addresses=focus, tx_hashes=tx_hashes))
        batch_total += 1
        extension_cycle += 1
        time.sleep(1)

    return summary()


def reset_gcda() -> None:
    for gcda in BUILD.rglob("*.gcda"):
        gcda.unlink(missing_ok=True)


def capture_coverage(arm_dir: Path,
                     include_patterns: list[str] | None = None) -> dict:
    raw = arm_dir / "raw.info"
    filtered = arm_dir / "filtered.info"
    capture = subprocess.run(
        ["lcov", "--capture", "--directory", str(BUILD),
         "--ignore-errors", "inconsistent,unmapped,source,unused",
         "--gcov-tool", "/usr/bin/gcov-14", "--output-file", str(raw)],
        capture_output=True, text=True, timeout=900)
    if not raw.exists() or raw.stat().st_size == 0:
        raise RuntimeError(
            "lcov capture failed for "
            f"{BUILD}: rc={capture.returncode}, stderr={capture.stderr.strip()}"
        )
    filtered_run = subprocess.run(
        ["lcov", "--remove", str(raw), "/usr/*", "*vcpkg_installed*",
         "*boost*", "*test*", "*build*", "*deps*",
         "--ignore-errors", "inconsistent,unmapped,source,unused",
         "--output-file", str(filtered)],
        capture_output=True, text=True, timeout=300)
    if not filtered.exists() or filtered.stat().st_size == 0:
        raise RuntimeError(
            "lcov filter failed for "
            f"{BUILD}: rc={filtered_run.returncode}, stderr={filtered_run.stderr.strip()}"
        )
    found = hit = 0
    if filtered.exists():
        include = True
        for line in filtered.read_text(errors="replace").splitlines():
            if line.startswith("SF:"):
                path = line[3:].strip()
                include = (not include_patterns or
                           any(pattern in path for pattern in include_patterns))
            elif include and line.startswith("LF:"):
                found += int(line[3:])
            elif include and line.startswith("LH:"):
                hit += int(line[3:])
    return {"covered_lines": hit, "total_lines": found,
            "coverage_pct": (hit / found * 100.0) if found else 0.0}


def archive_logs(net_dir: Path, arm_dir: Path) -> None:
    for node in range(4):
        node_dir = net_dir / f"node{node}"
        for name in ("start.sh", "stop.sh", "nohup.out", "config.ini"):
            src = node_dir / name
            if src.exists():
                shutil.copy2(src, arm_dir / f"node{node}-{name}")
        log_dir = node_dir / "log"
        logs = sorted(log_dir.glob("log*"))[-2:]
        for idx, src in enumerate(logs):
            shutil.copy2(src, arm_dir / f"node{node}-log{idx}.log")


def run_round(arm: str, arm_dir: Path, seed: int, round_idx: int,
              case: str, clear_coverage: bool = False, *,
              full_coverage: bool = True,
              round_deadline: float | None = None) -> dict:
    round_dir = arm_dir / f"round{round_idx:03d}"
    round_dir.mkdir(parents=True, exist_ok=True)
    if clear_coverage:
        reset_gcda()
    runtime = Path("/tmp") / f"fisco-live-{arm}-{round_idx}-{os.getpid()}"
    shutil.rmtree(runtime, ignore_errors=True)
    runtime.mkdir(exist_ok=True)
    kill_stale_fisco_processes()
    net = build_network(runtime)
    configure_rpc_tls(net)
    overlay_case_config(net, case, seed)
    mutated = mutate_node_config(net, arm, seed)
    config_only_baseline = arm not in ACTIVE_WORKLOAD_ARMS
    start = time.monotonic()
    cycles = 1
    network_started = False
    activity = {
        "tx_batches": 0,
        "submitted_transactions": 0,
        "restart_sync_ok": False,
        "offline_window_submissions": 0,
    }
    for cyc in range(cycles):
        network_started = start_chain(net)
        try:
            if network_started:
                if config_only_baseline:
                    time.sleep(8)
                else:
                    activity = interact(arm, net, round_deadline=round_deadline)
                time.sleep(5)
            else:
                print("WARN: chain did not reach consensus", flush=True)
        finally:
            stop_chain(net)
            kill_stale_fisco_processes()
            archive_logs(net, round_dir)
    elapsed = time.monotonic() - start
    metrics = capture_coverage(
        round_dir,
        include_patterns=None if full_coverage else FISCO_SCOPED_COVERAGE_MODULES,
    )
    result = {"arm": arm, "case": case, "mutated_options": mutated,
              "round": round_idx,
              "config_only_baseline": config_only_baseline,
              "network_started": network_started,
              "coverage_scope": (
                  "merged full line coverage across four instrumented FISCO-BCOS nodes"
                  if full_coverage else
                  "merged scoped line coverage across FISCO config-relevant modules (txpool/sync/executor/scheduler/pbft/sealer)"
              ),
              "elapsed_seconds": round(elapsed, 1), **activity, **metrics}
    (round_dir / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    shutil.rmtree(runtime, ignore_errors=True)
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
                     *, full_coverage: bool = True) -> dict:
    arm_dir = out_dir / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    reset_gcda()
    (arm_dir / "timeline.jsonl").unlink(missing_ok=True)

    deadline = (time.monotonic() + budget_minutes * 60.0) if budget_minutes > 0 else None
    round_limit = rounds
    if budget_minutes > 0 and rounds <= 1:
        round_limit = 0

    timeline: list[dict] = []
    round_idx = 1
    last_round_elapsed = None
    while True:
        if round_limit > 0 and round_idx > round_limit:
            break
        now = time.monotonic()
        if deadline is not None:
            if now >= deadline:
                break
            if last_round_elapsed is not None and (deadline - now) < last_round_elapsed:
                break
        case = CASES[(round_idx - 1) % len(CASES)]
        result = run_round(
            arm,
            arm_dir,
            seed + round_idx - 1,
            round_idx,
            case,
            clear_coverage=False,
            full_coverage=full_coverage,
            round_deadline=(
                deadline
                if deadline is not None and round_limit == 0 and full_budget and arm in ACTIVE_WORKLOAD_ARMS
                else None
            ),
        )
        append_timeline(arm_dir, result)
        timeline.append(result)
        try:
            last_round_elapsed = float(result.get("elapsed_seconds", 0.0))
        except (TypeError, ValueError):
            last_round_elapsed = None
        if not full_budget and len(timeline) >= converge_rounds:
            window = timeline[-converge_rounds:]
            if len({item["covered_lines"] for item in window}) == 1:
                break
        round_idx += 1

    final = timeline[-1] if timeline else capture_coverage(
        arm_dir,
        include_patterns=None if full_coverage else FISCO_SCOPED_COVERAGE_MODULES,
    )
    converged = False
    if len(timeline) >= converge_rounds:
        window = timeline[-converge_rounds:]
        converged = len({item["covered_lines"] for item in window}) == 1
    summary = {
        **final,
        "rounds_completed": len(timeline),
        "converged": converged,
        "coverage_scope": (
            "merged full line coverage across four instrumented FISCO-BCOS nodes"
            if full_coverage else
            "merged scoped line coverage across FISCO config-relevant modules (txpool/sync/executor/scheduler/pbft/sealer)"
        ),
    }
    (arm_dir / "result.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    import argparse
    global BUILD, PORT_OFFSET
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arms", default="fixed,varied,ecfuzz,conferr,conftest,confdiag")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, default=BUILD,
                        help="coverage-instrumented FISCO build root; default: ROOT/build-cov")
    parser.add_argument("--port-offset", type=int, default=0,
                        help="additive offset applied to the base P2P/RPC port ranges")
    parser.add_argument("--seed", type=int, default=20270802)
    parser.add_argument("--rounds", type=int, default=1,
                        help="number of rounds per arm; with --budget-minutes and rounds<=1, run until budget")
    parser.add_argument("--converge-rounds", type=int, default=8)
    parser.add_argument("--budget-minutes", type=float, default=0.0,
                        help="wall-clock budget per arm; 0 disables budgeted looping")
    parser.add_argument("--full-budget", action="store_true",
                        help="run for the full wall-clock budget even after convergence")
    parser.set_defaults(full_coverage=True)
    parser.add_argument("--full-coverage", dest="full_coverage", action="store_true",
                        help="collect full line coverage across the instrumented FISCO-BCOS codebase (default)")
    parser.add_argument("--scoped-coverage", dest="full_coverage", action="store_false",
                        help="collect only scoped config-relevant module coverage")
    args = parser.parse_args()
    BUILD = args.build_dir.resolve()
    PORT_OFFSET = args.port_offset
    if not cov_node_bin().is_file():
        raise FileNotFoundError(f"coverage binary not found: {cov_node_bin()}")
    args.output.mkdir(parents=True, exist_ok=True)
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
    (args.output / "summary.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
