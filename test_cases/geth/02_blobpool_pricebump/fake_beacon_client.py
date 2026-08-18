#!/usr/bin/env python3
"""最小化假共识客户端 (fake beacon client) — 支持 V1(pre-Shanghai) 与 V3(Cancun) 模式

通过 Engine API 驱动 PoS geth 出块（用于无真实 CL 的本地复现环境）。

用法:
  V1 (pre-Shanghai): python3 fake_beacon_client.py v1 payload <authrpc_port> <jwtsecret> <起始head> <轮数> <周期秒>
  V1 同步:          python3 fake_beacon_client.py v1 update  <authrpc_port> <jwtsecret> <目标head> <轮数> <周期秒>
  V3 (Cancun):      python3 fake_beacon_client.py v3 payload <authrpc_port> <jwtsecret> <起始head> <轮数> <周期秒>
  V3 同步:          python3 fake_beacon_client.py v3 update  <authrpc_port> <jwtsecret> <目标head> <轮数> <周期秒>
  V5 (Osaka):       python3 fake_beacon_client.py v5 payload <authrpc_port> <jwtsecret> <起始head> <轮数> <周期秒>
  V5 同步:          python3 fake_beacon_client.py v5 update  <authrpc_port> <jwtsecret> <目标head> <轮数> <周期秒>
"""
import sys
import json
import time
import hashlib
import hmac
import base64
import rlp
import requests

API_VER = sys.argv[1]
MODE = sys.argv[2]
AUTH_PORT = sys.argv[3]
JWT_FILE = sys.argv[4]
GENESIS_HASH = sys.argv[5]
ROUNDS = int(sys.argv[6]) if len(sys.argv) > 6 else 3
PERIOD = int(sys.argv[7]) if len(sys.argv) > 7 else 5

secret_raw = open(JWT_FILE, 'rb').read().strip()
if secret_raw.startswith(b'0x'):
    secret = bytes.fromhex(secret_raw[2:].decode())
else:
    secret = bytes.fromhex(secret_raw.decode())
assert len(secret) == 32, f"JWT secret 长度错误: {len(secret)}"
url = f"http://127.0.0.1:{AUTH_PORT}"


def make_token(secret):
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b'=')
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": int(time.time())}).encode()).rstrip(b'=')
    signing_input = header + b'.' + payload
    sig = base64.urlsafe_b64encode(
        hmac.new(secret, signing_input, hashlib.sha256).digest()).rstrip(b'=')
    return (signing_input + b'.' + sig).decode()


def rpc(method, params):
    global token
    token = make_token(secret)  # 每轮刷新，避免 JWT 60s 过期
    resp = requests.post(url,
                         json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"{method} error: {data['error']}")
    return data["result"]


def extract_blob_hashes(transactions):
    """从区块交易中提取 blob versioned hashes（兼容 hex 字符串与 bytes）"""
    hashes = []
    for raw in transactions:
        if isinstance(raw, str):
            data = bytes.fromhex(raw[2:]) if raw.startswith('0x') else bytes.fromhex(raw)
        else:
            data = bytes(raw)
        if not data or data[0] != 0x03:
            continue
        try:
            body = rlp.decode(data[1:])
            # 本 fork blob tx: [chainId, nonce, tip, fee, gas, to, value, data,
            #                   accessList, blobFeeCap, blobVersionedHashes, v, r, s]
            inner = body[0] if isinstance(body[0], list) else body
            vhs = inner[10]
            for h in vhs:
                hashes.append("0x" + h.hex())
        except Exception:
            continue
    return hashes


print(f"[fakeCL:{API_VER}:{MODE}] genesis/head = {GENESIS_HASH}", file=sys.stderr)

head = GENESIS_HASH
for i in range(ROUNDS):
    attrs = None
    if MODE == "payload":
        ts = int(time.time()) + 1
        if API_VER in ("v3", "v5"):
            attrs = {
                "timestamp": hex(ts),
                "prevRandao": "0x" + "00" * 31 + f"{i:02x}",
                "suggestedFeeRecipient": "0x0000000000000000000000000000000000000001",
                "withdrawals": [],
                "parentBeaconBlockRoot": "0x" + "11" * 32,
            }
        else:
            attrs = {
                "timestamp": hex(ts),
                "prevRandao": "0x" + "00" * 31 + f"{i:02x}",
                "suggestedFeeRecipient": "0x0000000000000000000000000000000000000001",
            }
    try:
        fcu_method = "engine_forkchoiceUpdatedV3" if API_VER in ("v3", "v5") else "engine_forkchoiceUpdatedV1"
        fcu = rpc(fcu_method,
                  [{"headBlockHash": head,
                    "safeBlockHash": head,
                    "finalizedBlockHash": GENESIS_HASH}, attrs])
        if MODE == "payload":
            pid = fcu["payloadId"]
            time.sleep(1.0)

            get_method = "engine_getPayloadV5" if API_VER == "v5" else ("engine_getPayloadV3" if API_VER == "v3" else "engine_getPayloadV1")
            env = rpc(get_method, [pid])
            if API_VER in ("v3", "v5"):
                exec_payload = env["executionPayload"]
            else:
                exec_payload = env
            block_hash = exec_payload["blockHash"]
            n_txs = len(exec_payload["transactions"])
            vhashes = extract_blob_hashes(exec_payload["transactions"])

            if API_VER == "v5":
                new_method = "engine_newPayloadV4"
                new_params = [exec_payload, vhashes, "0x" + "11" * 32, []]
            elif API_VER == "v3":
                new_method = "engine_newPayloadV3"
                new_params = [exec_payload, vhashes, "0x" + "11" * 32]
            else:
                new_method = "engine_newPayloadV1"
                new_params = [exec_payload]
            status = rpc(new_method, new_params)
            rpc(fcu_method,
                [{"headBlockHash": block_hash,
                  "safeBlockHash": block_hash,
                  "finalizedBlockHash": GENESIS_HASH}, None])
            head = block_hash
            print(f"[fakeCL] round {i}: block={block_hash} txs={n_txs} blobs={len(vhashes)} status={status['status']} head={head}", file=sys.stderr)
        else:
            print(f"[fakeCL] update round {i}: fcu status={fcu['payloadStatus']['status']} head={head}", file=sys.stderr)
    except Exception as e:
        # 瞬态错误（如同步中 payloadId 为空导致 getPayload 报错）时重试整轮
        if MODE == "payload" and "Unsupported fork" in str(e):
            for attempt in range(3):
                time.sleep(2.0)
                try:
                    fcu = rpc(fcu_method,
                              [{"headBlockHash": head,
                                "safeBlockHash": head,
                                "finalizedBlockHash": GENESIS_HASH}, attrs])
                    pid = fcu["payloadId"]
                    if pid is None:
                        continue
                    env = rpc(get_method, [pid])
                    exec_payload = env["executionPayload"] if API_VER in ("v3", "v5") else env
                    block_hash = exec_payload["blockHash"]
                    n_txs = len(exec_payload["transactions"])
                    vhashes = extract_blob_hashes(exec_payload["transactions"])
                    if API_VER == "v5":
                        status = rpc("engine_newPayloadV4", [exec_payload, vhashes, "0x" + "11" * 32, []])
                    elif API_VER == "v3":
                        status = rpc("engine_newPayloadV3", [exec_payload, vhashes, "0x" + "11" * 32])
                    else:
                        status = rpc("engine_newPayloadV1", [exec_payload])
                    rpc(fcu_method,
                        [{"headBlockHash": block_hash,
                          "safeBlockHash": block_hash,
                          "finalizedBlockHash": GENESIS_HASH}, None])
                    head = block_hash
                    print(f"[fakeCL] round {i} (retry{attempt}): block={block_hash} txs={n_txs} blobs={len(vhashes)} status={status['status']}", file=sys.stderr)
                    break
                except Exception as e2:
                    print(f"[fakeCL] round {i} retry{attempt} failed: {e2}", file=sys.stderr)
        else:
            print(f"[fakeCL] round {i} failed: {e}", file=sys.stderr)
    time.sleep(PERIOD)
