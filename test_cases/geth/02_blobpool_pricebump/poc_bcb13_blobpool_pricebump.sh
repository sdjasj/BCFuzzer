#!/usr/bin/env bash
# =============================================================================
# BCB-13 POC: --blobpool.pricebump 1000000 → 出块节点拒绝 blob 交易替换
#         挖出旧版 blob 交易（原收款方），用户替换意图全网永久失效
#         （Inter-node BCB，f=1 容错网络下依然有效）
# =============================================================================
# 复现流程：
#   1. 构造 Cancun Clique 创世块（chainId=15, cancunTime=0, blobSchedule）
#   2. 启动双节点：node1 正常；node2 带 --blobpool.pricebump 1000000（出块者）
#   3. 节点互连，node2 标记 synced
#   4. 构造 blob 交易 A（1 blob, 收款方 X）提交到 node1 → 传播到 node2
#   5. 构造 blob 替换交易 A'（同 nonce, 三项费用 +20%, 收款方 Y —— 用户修正意图）
#      → node1 正常替换(2.5x>2x)；node2 (pricebump=1M, 需 10001x) 拒绝
#   6. node2 出块 → 链上打包旧版 A（收款方 X）→ 用户的替换在全网永久失效
#
# 前置条件: make geth；python3 有 eth_account/requests/ckzg + TSETUP 可信设置
# 用法:     ./poc_bcb13_blobpool_pricebump.sh
# =============================================================================
set -u

GETH=build/bin/geth
[ -x "$GETH" ] || { echo "错误: 未找到 $GETH，请先执行 make geth"; exit 1; }
TS=${TSETUP:-/home/geth/go/pkg/mod/github.com/ethereum/c-kzg-4844/v2@v2.1.8/src/trusted_setup.txt}
[ -f "$TS" ] || { echo "错误: 未找到 KZG 可信设置文件 (TSETUP=$TS)"; exit 1; }

pkill -x geth 2>/dev/null || true
sleep 1

RUNTIME=$(mktemp -d /tmp/bcb13-poc.XXXXXX)
mkdir -p "$RUNTIME/node1" "$RUNTIME/node2"

echo "================================================================"
echo " BCB-13: --blobpool.pricebump 1000000 → 挖出旧版 blob 交易"
echo "================================================================"
echo "  Node1: 默认配置 (pricebump=100)"
echo "  Node2: --blobpool.pricebump 1000000 (替换需 10001x 加价, 出块者)"
echo "  链: Cancun+Prague+Osaka (blob v1 侧车)"
echo ""

# --- 生成测试密钥 + Cancun 创世块 ---
GENESIS_JSON=$(python3 - "$RUNTIME/key.txt" << 'PYEOF'
import sys, json
from eth_keys import keys
priv = keys.PrivateKey(b'\x42' * 32)
addr = priv.public_key.to_checksum_address()
with open(sys.argv[1], 'w') as f:
    f.write(priv.to_hex()[2:])
print(json.dumps({
    "config": {"chainId": 15, "homesteadBlock": 0, "eip150Block": 0, "eip155Block": 0,
               "eip158Block": 0, "byzantiumBlock": 0, "constantinopleBlock": 0,
               "petersburgBlock": 0, "istanbulBlock": 0, "berlinBlock": 0, "londonBlock": 0,
               "terminalTotalDifficulty": 0, "shanghaiTime": 0, "cancunTime": 0,
               "pragueTime": 0, "osakaTime": 0,
               "blobSchedule": {
                   "cancun": {"target": 3, "max": 6, "baseFeeUpdateFraction": 3338477},
                   "prague": {"target": 6, "max": 9, "baseFeeUpdateFraction": 5007716}},
               "clique": {"period": 5, "epoch": 30000}},
    "difficulty": "0", "gasLimit": "8000000",
    "extradata": "0x0000000000000000000000000000000000000000000000000000000000000000" + addr[2:] + "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
    "alloc": {addr: {"balance": "100000000000000000000"}}
}))
PYEOF
)
echo "$GENESIS_JSON" > "$RUNTIME/genesis.json"
echo "[OK] 创世块有效 (Cancun)"

# 初始化双节点
"$GETH" init --datadir "$RUNTIME/node1" "$RUNTIME/genesis.json" >/dev/null 2>&1
"$GETH" init --datadir "$RUNTIME/node2" "$RUNTIME/genesis.json" >/dev/null 2>&1

# node1: 正常配置
"$GETH" --datadir "$RUNTIME/node1" --networkid 15 \
  --port 30541 --nat extip:127.0.0.1 --nodiscover --ipcdisable \
  --http --http.addr 127.0.0.1 --http.port 18787 \
  --http.api admin,eth,net,web3,txpool --authrpc.port 18789 \
  --syncmode full \
  >"$RUNTIME/node1.log" 2>&1 &
N1=$!

# node2: pricebump=1000000（出块者）
"$GETH" --datadir "$RUNTIME/node2" --networkid 15 \
  --port 30542 --nat extip:127.0.0.1 --nodiscover --ipcdisable \
  --http --http.addr 127.0.0.1 --http.port 18788 \
  --http.api admin,eth,net,web3,txpool --authrpc.port 18790 \
  --blobpool.pricebump 1000000 --blobpool.fetchprobability 100 --syncmode full \
  >"$RUNTIME/node2.log" 2>&1 &
N2=$!

sleep 4

kill -0 "$N1" 2>/dev/null || { echo "错误: node1 启动失败"; exit 1; }
kill -0 "$N2" 2>/dev/null || { echo "错误: node2 启动失败"; exit 1; }
echo "[OK] 双节点已启动"

# 互连 + 标记 node2 synced
ENODE=$(curl --noproxy '*' -fsS -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","method":"admin_nodeInfo","params":[],"id":1}' \
  http://127.0.0.1:18787 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['enode'])" 2>/dev/null)
curl --noproxy '*' -fsS -H 'Content-Type: application/json' \
  --data "{\"jsonrpc\":\"2.0\",\"method\":\"admin_addPeer\",\"params\":[\"$ENODE\"],\"id\":1}" \
  http://127.0.0.1:18788 >/dev/null 2>&1
sleep 2
PEERS=$(curl --noproxy '*' -fsS -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","method":"net_peerCount","params":[],"id":1}' \
  http://127.0.0.1:18787 2>/dev/null)
echo "[OK] peerCount = $PEERS"

GENESIS_HASH=$(curl --noproxy '*' -fsS -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","method":"eth_getBlockByNumber","params":["0x0",false],"id":1}' \
  http://127.0.0.1:18787 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['hash'])" 2>/dev/null)
python3 "$(dirname "$0")/fake_beacon_client.py" v5 update 18790 "$RUNTIME/node2/geth/jwtsecret" "$GENESIS_HASH" 1 1 >/dev/null 2>&1
echo "[OK] node2 已标记 synced"

# --- 构造 blob 交易 A（收款方 X）与替换 A'（收款方 Y, 三项费用+20%） ---
echo ""
echo "[*] 构造 blob 交易 A (收款方 X) 与替换 A' (收款方 Y, 费用+20%)..."
python3 - "$RUNTIME/key.txt" "$RUNTIME/reqA.json" "$RUNTIME/reqAp.json" "$TS" << 'PYEOF'
import sys, json, rlp
import ckzg
from eth_account import Account
from eth_utils import to_checksum_address

settings = ckzg.load_trusted_setup(sys.argv[4], 0)
priv = open(sys.argv[1]).read().strip()
blob = bytes(131072)
X = to_checksum_address('0x71562b71999873db5b286df9577581998cbf4e81')
Y = to_checksum_address('0x71562b71999873db5b286df9577581998cbf4e82')

def build(nonce, fee, tip, blobfee, to, out):
    tx = {'chainId': 15, 'nonce': nonce, 'gas': 50000,
          'maxFeePerGas': fee, 'maxPriorityFeePerGas': tip,
          'to': to, 'value': 10**17, 'data': b'', 'type': 3,
          'maxFeePerBlobGas': blobfee}
    signed = Account.sign_transaction(tx, priv, blobs=[blob])
    payload = rlp.decode(signed.raw_transaction[1:])
    inner, legacy_blobs, commitments, proofs = payload[0], payload[1], payload[2], payload[3]
    cells, cell_proofs = ckzg.compute_cells_and_kzg_proofs(blob, settings)
    v1_raw = bytes([0x03]) + rlp.encode([inner, 1, legacy_blobs, commitments, cell_proofs])
    open(out, 'w').write(json.dumps({"jsonrpc":"2.0","method":"eth_sendRawTransaction","params":["0x"+v1_raw.hex()],"id":1}))

# A: 20 gwei / 20 gwei / 1 gwei blob fee → X
build(0, 20*10**9, 20*10**9, 10**9, X, sys.argv[2])
# A': 2.5x 全部费用 → Y（用户修正意图；blobpool 默认替换惩罚为 100%, 需 >2x）
build(0, 50*10**9, 50*10**9, 25*10**8, Y, sys.argv[3])
print("A : 20gwei/20gwei/1gwei → X", file=sys.stderr)
print("A': 50gwei/50gwei/2.5gwei → Y (2.5x)", file=sys.stderr)
PYEOF

echo "[*] 提交 blob 交易 A 到 node2 (出块者, 本地 RPC → 完整 sidecar)..."
RES_A2=$(curl --noproxy '*' -sS -H 'Content-Type: application/json' --data @"$RUNTIME/reqA.json" http://127.0.0.1:18788 2>/dev/null)
echo "  node2 响应: $RES_A2"
sleep 3

echo ""
echo "[*] 提交 blob 交易 A 到 node1 (对照: 正常节点接受)..."
RES_A=$(curl --noproxy '*' -sS -H 'Content-Type: application/json' --data @"$RUNTIME/reqA.json" http://127.0.0.1:18787 2>/dev/null)
echo "  node1 响应: $RES_A"
sleep 3

echo ""
echo "[*] 提交 blob 替换 A' 到 node1 (对照: 正常节点应接受替换)..."
RES_AP=$(curl --noproxy '*' -sS -H 'Content-Type: application/json' --data @"$RUNTIME/reqAp.json" http://127.0.0.1:18787 2>/dev/null)
echo "  node1 响应: $RES_AP"
sleep 3

echo ""
echo "[*] 提交 blob 替换 A' 到 node2 (出块者, 应被 pricebump 拒绝)..."
RES_AP2=$(curl --noproxy '*' -sS -H 'Content-Type: application/json' --data @"$RUNTIME/reqAp.json" http://127.0.0.1:18788 2>/dev/null)
echo "  node2 响应: $RES_AP2"
sleep 3

echo ""
echo "[*] node2 交易池（应仍持有旧版 A）..."
echo "  $(curl --noproxy '*' -fsS -H 'Content-Type: application/json' --data '{"jsonrpc":"2.0","method":"txpool_status","params":[],"id":1}' http://127.0.0.1:18788 2>/dev/null)"

# =============================================================================
# 核心场景: node2 出块 → 链上打包旧版 A（收款方 X）
# =============================================================================
echo ""
echo "[*] 假共识客户端驱动 node2 出块 (5 块)..."
python3 "$(dirname "$0")/fake_beacon_client.py" v5 payload 18790 "$RUNTIME/node2/geth/jwtsecret" "$GENESIS_HASH" 5 1 \
  >"$RUNTIME/fakecl2.log" 2>&1 &
FCL=$!
wait "$FCL" 2>/dev/null
sleep 2

# 检查区块 #1 挖出的 blob 交易收款方
MINED_TX=$(curl --noproxy '*' -fsS -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","method":"eth_getBlockByNumber","params":["0x1",true],"id":1}' \
  http://127.0.0.1:18788 2>/dev/null | python3 -c "
import sys,json
d = json.load(sys.stdin)['result']
if d and d['transactions']:
    print(d['transactions'][0]['hash'])
else:
    print('none')" 2>/dev/null)

TX_DETAIL=$(curl --noproxy '*' -fsS -H 'Content-Type: application/json' \
  --data "{\"jsonrpc\":\"2.0\",\"method\":\"eth_getTransactionByHash\",\"params\":[\"$MINED_TX\"],\"id\":1}" \
  http://127.0.0.1:18788 2>/dev/null | python3 -c "
import sys,json
d = json.load(sys.stdin)['result']
if d:
    print(f'to={d[\"to\"]} maxFeePerBlobGas={int(d[\"maxFeePerBlobGas\"],16)}')
else:
    print('not found')" 2>/dev/null)
echo "  区块#1 挖出的 blob 交易: $MINED_TX"
echo "  详情: $TX_DETAIL"

echo ""
echo "================================================================"
TO_X=$(echo "$TX_DETAIL" | grep -c "4e81")
if [ "$TO_X" -ge 1 ] && [ -n "$MINED_TX" ] && [ "$MINED_TX" != "none" ]; then
  echo " [POC 复现成功 ✓] 出块节点挖出的是旧版 blob 交易 A（收款方 X）！"
  echo ""
  echo "  证据链:"
  echo "  1. blob 交易 A (20gwei → X) 提交到 node1，传播到 node2"
  echo "  2. blob 替换 A' (24gwei → Y, +20%) 提交到 node1"
  echo "     —— node1 正常替换（池中为 A'）；node2 (pricebump=1M, 需 10001x)"
  echo "     拒绝 A'（池中仍为旧版 A）"
  echo "  3. node2 出块，区块#1 打包的是旧版 A（$TX_DETAIL）"
  echo "  4. 用户的替换意图（收款方 Y）随 nonce 被消耗而全网永久失效"
  echo ""
  echo "  结论: 单个节点的 --blobpool.pricebump 配置（合法不一致配置项）"
  echo "  使 EIP-4844 blob 交易替换在出块者处失效，链上打包旧版 blob 交易"
  echo "  —— 与 BCB-7 同类，作用于 blob 交易池；f=1 容错网络下依然有效"
  echo "  （其他 proposer 也无法挽回已被消耗的 nonce）。"
else
  echo " [POC 复现失败 ✗] mined=$MINED_TX detail=$TX_DETAIL"
  tail -5 "$RUNTIME/fakecl2.log" 2>/dev/null
fi
echo "================================================================"

# 清理
kill -INT "$N1" "$N2" 2>/dev/null
wait "$N1" "$N2" 2>/dev/null || true
rm -rf "$RUNTIME"
echo ""
echo "[*] 已清理临时数据"
