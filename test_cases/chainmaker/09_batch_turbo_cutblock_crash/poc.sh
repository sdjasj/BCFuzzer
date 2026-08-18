#!/bin/bash
# ============================================================
# POC-9: 单节点配置 batch 池 + 链上 turbo/gas → 其他节点全部崩溃 (纯配置 Inter-node Crash)
# 一键复现脚本
#
# 用法: bash poc.sh
# 判定: 脚本自动检测, 输出 PASS/FAIL
#
# 纯配置文件修改, 零代码修改:
#   ① node1 本地 chainmaker.yml: txpool.pool_type = "batch" (仅 node1)
#   ② 链配置: consensus_message_turbo + enable_gas + enable_optimize_charge_gas
#   → node1 正常打包时 GetTurboBlock(batch 分支) 产生 TxCount > len(Txs) 的裁剪块
#   → node2/3/4 验证时 block.Txs[TxCount-1] 越界 panic → 崩溃
# ============================================================
set -e

ROOT=/home/geth/tse/chainmaker-go
SCRIPTS=$ROOT/scripts
RELEASE=$ROOT/build/release
CMC=$ROOT/tools/cmc/cmc
BUG_DIR=$(cd "$(dirname "$0")" && pwd)
SDK_CONF=$ROOT/bug/00_common/sdk_config.yml
CONTRACT=rust-counter-2.0.0.wasm

say() { echo "[*] $*"; }
err() { echo "[!] $*"; }

echo "=============================================="
echo " POC-9: 纯配置 batch 池 → 其他节点全部崩溃"
echo "=============================================="

if [ ! -f "$CMC" ]; then
    say "构建 cmc..."
    (cd $ROOT/tools/cmc && go build -o cmc .)
fi

# ---------- 1. 停止旧网络, 清理 ----------
say "[1/6] 停止旧网络并清理 release..."
(cd $SCRIPTS && bash cluster_quick_stop.sh >/dev/null 2>&1) || true
sleep 2
rm -rf $RELEASE/chainmaker-v3.0.0-wx-org*
rm -f  $RELEASE/chainmaker-v3.0.0-wx-org*

# ---------- 2. 生成 release ----------
say "[2/6] 生成 4 节点 TBFT release 包..."
(cd $SCRIPTS && bash prepare.sh 4 1 11301 12301 32351 22351 23351 \
    -c 1 -l INFO -v false -j false --vlog=INFO --jlog=INFO >/dev/null 2>&1)
(cd $SCRIPTS && bash build_release.sh >/dev/null 2>&1)

# ---------- 3. 纯配置修改 ----------
say "[3/6] 纯配置修改: node1 batch 池 + 链配置 turbo/gas..."
for org in 1 2 3 4; do
    T=$(ls $RELEASE/chainmaker-v3.0.0-wx-org${org}*.tar.gz | head -1)
    (cd $RELEASE && tar zxf "$T")
    BC=$RELEASE/chainmaker-v3.0.0-wx-org${org}.chainmaker.org/config/wx-org${org}.chainmaker.org/chainconfig/bc1.yml
    python3 - "$BC" << 'EOF'
import sys
p = sys.argv[1]
s = open(p).read()
old1 = """account_config:
  # the flag to control if subtracting gas from transaction's origin account when sending tx.
  enable_gas: false"""
new1 = """account_config:
  # the flag to control if subtracting gas from transaction's origin account when sending tx.
  enable_gas: true"""
assert old1 in s; s = s.replace(old1, new1)
old2 = """  # Used for dynamic tuning the capacity of tx execution goroutine pool
  enable_conflicts_bit_window: true"""
new2 = """  # Used for dynamic tuning the capacity of tx execution goroutine pool
  enable_conflicts_bit_window: true

  # enable optimized charge gas
  enable_optimize_charge_gas: true"""
assert old2 in s; s = s.replace(old2, new2)
old3 = """  # consensus_turbo_config:
    # If consensus message compression is enabled or not(solo could not use consensus message turbo).
    # consensus_message_turbo: false"""
new3 = """  consensus_turbo_config:
    consensus_message_turbo: true
    retry_time: 500
    retry_interval: 20"""
assert old3 in s; s = s.replace(old3, new3)
open(p, 'w').write(s)
EOF
    # node1 本地配置: batch 池
    if [ $org -eq 1 ]; then
        CM=$RELEASE/chainmaker-v3.0.0-wx-org1.chainmaker.org/config/wx-org1.chainmaker.org/chainmaker.yml
        sed -i 's/pool_type: "normal"/pool_type: "batch"/' "$CM"
        grep -q 'pool_type: "batch"' "$CM" && say "    node1 -> pool_type=batch OK"
    fi
done

# ---------- 4. 启动 + 部署 ----------
say "[4/6] 启动网络并部署合约..."
(cd $SCRIPTS && bash cluster_quick_start.sh normal >/dev/null 2>&1)
sleep 35
ALIVE=$(ps -eo args | grep -c '[c]hainmaker start' || true)
[ "$ALIVE" -lt 4 ] && err "FAIL: 节点启动失败 (alive=$ALIVE)" && exit 1
say "    存活节点数: $ALIVE"

cd $ROOT/bug/00_common
[ -L certs ] || ln -sfn $RELEASE/chainmaker-v3.0.0-wx-org1.chainmaker.org/config/wx-org1.chainmaker.org/certs certs
sleep 10
for i in $(seq 1 30); do
    grep -q 'all necessary peers connected' \
        $RELEASE/chainmaker-v3.0.0-wx-org1.chainmaker.org/log/system.log 2>/dev/null && break
    sleep 2
done
$CMC client contract user create --contract-name=counter --runtime-type=WASMER \
    --byte-code-path=$ROOT/test/wasm/$CONTRACT --version=1.0 \
    --sdk-conf-path="$SDK_CONF" >/dev/null 2>&1 || true

# ---------- 5. 发交易 ----------
say "[5/6] 持续发交易 (等待 node1 batch 池轮值打包)..."
for round in $(seq 1 10); do
    for i in $(seq 1 8); do
        $CMC client contract user invoke --contract-name=counter --method=increase \
            --sdk-conf-path="$SDK_CONF" --params="{}" --gas-limit=10000 >/dev/null 2>&1 || true
    done
    sleep 8
    ALIVE=$(ps -eo args | grep -c '[c]hainmaker start' || true)
    if [ "$ALIVE" -lt 4 ]; then
        say "    第 $round 轮: 节点数降至 $ALIVE (crash 发生!)"
        break
    fi
done

# ---------- 6. 判定 ----------
say "[6/6] 判定..."
CRASH_NODES=0
for i in 2 3 4; do
    N=$RELEASE/chainmaker-v3.0.0-wx-org${i}.chainmaker.org
    if grep -q 'index out of range' $N/bin/panic.log 2>/dev/null; then
        CRASH_NODES=$((CRASH_NODES + 1))
        say "    [√] node${i} panic: index out of range (崩溃确认)"
    fi
done
ALIVE_FINAL=$(ps -eo args | grep -c '[c]hainmaker start' || true)
say "    崩溃节点数: $CRASH_NODES, 存活节点数: $ALIVE_FINAL"

echo ""
echo "=============================================="
if [ "$CRASH_NODES" -ge 2 ]; then
    echo " 结果: PASS — BUG 复现成功"
    echo " 现象: 仅 node1 修改本地配置 pool_type=batch, 链上 turbo+gas,"
    echo "       所有其他节点(默认配置)验证 node1 的裁剪块时越界 panic → 进程崩溃"
elif [ "$CRASH_NODES" -ge 1 ]; then
    echo " 结果: PARTIAL — 仅部分节点崩溃"
else
    echo " 结果: FAIL — 未复现"
fi
echo "=============================================="
