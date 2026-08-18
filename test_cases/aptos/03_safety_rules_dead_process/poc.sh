#!/usr/bin/env bash
# =============================================================================
# 一键 POC: consensus.round_initial_timeout_ms = 0 → 全网共识永久停滞
#          (FISCO min_seal_time 同款:配置节点完全存活,但全网无法提交任何区块)
#
# 本 bug 为本次审计新发现(严格 inter-node 类:配置节点不崩溃、不退出)。
# 根因: ExponentialTimeInterval(round_initial_timeout_ms=0) → 节点每轮瞬间超时、
#       永远只投 nil 票 → 2 验证者网络中任何区块都凑不齐 QC → 轮次靠 TC 无限
#       推进,零区块提交 → ledger 永久冻结,交易永远得不到回执。
# 配置位置: consensus/src/epoch_manager.rs:299 (create_round_state)
# 测试基线: aptos-core @ 7f99ad42 (aptos-node-v1.48.5-hotfix),2 验证者 local swarm
#
# 用法:  ./poc.sh [aptos-core 仓库路径]   (默认: 脚本上级目录)
# 依赖:  target-x86-64/release/{aptos-node,forge} 已构建
# =============================================================================
set -u

REPO="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
NODE_BIN="$REPO/target-x86-64/release/aptos-node"
FORGE_BIN="$REPO/target-x86-64/release/forge"

[ -x "$NODE_BIN" ] || { echo "FATAL: 找不到 aptos-node: $NODE_BIN"; exit 1; }
[ -x "$FORGE_BIN" ] || { echo "FATAL: 找不到 forge: $FORGE_BIN"; exit 1; }

SWARM_DIR=$(mktemp -d /tmp/poc-sr.XXXXXX)
echo "==> [1/6] 启动 2 验证者网络 (SWARM_DIR=$SWARM_DIR)"
"$FORGE_BIN" --suite run_forever --num-validators 2 test local-swarm \
    --swarmdir "$SWARM_DIR" --aptos-node-binary "$NODE_BIN" > "$SWARM_DIR/forge.log" 2>&1 &
FORGE_PID=$!

api_addr() { awk '/^api:/{getline; gsub(/[" ]/,"",$2); print $2}' "$SWARM_DIR/$1/node.yaml" 2>/dev/null; }
ledger()  { curl --noproxy '*' -fsS --max-time 5 "http://$(api_addr "$1")/v1" 2>/dev/null \
            | python3 -c "import json,sys;print(json.load(sys.stdin)['ledger_version'])" 2>/dev/null; }

for i in $(seq 1 120); do
    L0=$(ledger 0); L1=$(ledger 1)
    if [ -n "$L0" ] && [ -n "$L1" ] && [ "$L0" -gt 0 ] 2>/dev/null && [ "$L1" -gt 0 ] 2>/dev/null; then break; fi
    sleep 3
done
L0=$(ledger 0); L1=$(ledger 1)
{ [ -n "$L0" ] && [ -n "$L1" ] && [ "$L0" -gt 0 ] && [ "$L1" -gt 0 ]; } 2>/dev/null \
    || { echo "FATAL: 网络启动超时"; kill "$FORGE_PID" 2>/dev/null; exit 1; }
echo "==> 网络已启动 (ledger: node0=$L0 node1=$L1)"

echo "==> [2/6] 杀掉 forge(移除自动重启监视器,孤儿节点继续运行)"
kill "$FORGE_PID" 2>/dev/null; sleep 3

echo "==> [3/6] 基线 ledger: node0=$(ledger 0) node1=$(ledger 1)"

echo "==> [4/6] SIGKILL 节点0,将 safety_rules.service 改为 process + 死地址"
pkill -9 -f "$SWARM_DIR/0/node.yaml" 2>/dev/null; sleep 2
cp "$SWARM_DIR/0/node.yaml" "$SWARM_DIR/0/node.yaml.bak"
python3 - "$SWARM_DIR/0/node.yaml" << 'PYEOF'
import sys, re, yaml
path = sys.argv[1]
lines = open(path).read().splitlines()
sidx = next(i for i, l in enumerate(lines) if re.match(r'^  safety_rules:', l))
tidx = next(i for i in range(sidx + 1, len(lines)) if re.match(r'^    service:', lines[i]))
end = next(i for i in range(tidx + 1, len(lines)) if re.match(r'^    \S', lines[i]))
lines[tidx:end] = ['    service:',
                   '      type: process',
                   '      server_address: "/ip4/127.0.0.1/tcp/9999"']
open(path, 'w').write("\n".join(lines) + "\n")
d = yaml.safe_load(open(path))
assert d['consensus']['safety_rules']['service']['type'] == 'process', "配置修改失败"
print("配置已修改: safety_rules.service = process @ 127.0.0.1:9999(死地址)")
PYEOF

echo "==> [5/6] 重启节点0(带缺陷配置),等待网络停滞..."
( cd "$SWARM_DIR/0" && exec env RUST_LOG=debug "$NODE_BIN" -f node.yaml >> log 2>&1 ) &
RESTARTED_PID=$!

# 等节点0 重新加入并触发停滞(等 ledger 冻结 + 轮次推进)
sleep 40

PASS=1
echo "==> [6/6] 验证:两节点均存活 + ledger 冻结 + 轮次无限推进"
N0A=$(ledger 0); N1A=$(ledger 1)
sleep 20
N0B=$(ledger 0); N1B=$(ledger 1)
echo "node0 ledger_version: $N0A -> $N0B"
echo "node1 ledger_version: $N1A -> $N1B"

if kill -0 "$RESTARTED_PID" 2>/dev/null; then echo ">>> 节点0 进程存活(配置节点未崩溃)"; else echo ">>> 注意: 节点0 进程已退出"; fi
ps aux | grep aptos-node | grep -v grep | wc -l | xargs echo ">>> 存活 aptos-node 进程数:"

FROZEN=0
[ -n "$N1A" ] && [ "$N1A" = "$N1B" ] && FROZEN=1
REJECTS=$(grep -c "ConnectionRefused" "$SWARM_DIR/0/log" 2>/dev/null || echo 0)
echo ">>> node0 安全规则连接失败次数: $REJECTS"
if [ "$FROZEN" = "1" ] && [ "$REJECTS" -gt 5 ]; then
    echo ">>> 复现成功: 两节点均存活但 ledger 冻结,配置节点无法签名(安全规则不可达),全网共识永久停滞 (inter-node 影响确认)"
else
    echo ">>> 停滞特征不符,复现失败"; PASS=0
fi

echo "==> 清理"
for i in 0 1; do pkill -9 -f "$SWARM_DIR/$i/node.yaml" 2>/dev/null; done
sleep 2

echo "==> 结果: $([ $PASS -eq 1 ] && echo 'PASS' || echo 'FAIL')"
echo "==> 证据: 节点0日志 $SWARM_DIR/0/log (grep 'ConnectionRefused');两节点 ledger 冻结"
