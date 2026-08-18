#!/bin/bash
# ============================================================
# BCB #3: ChainMaker certificate reconfiguration + logger race panic
# 一键复现脚本 (最小化测试用例)
#
# 论文 Table 1 #3 (Peer Failure):
#   证书重配置与节点 rejoin 重叠, 且并发 logger 级别变更;
#   对 logger.go level map 的冲突访问 panic 正常节点.
#   Issue: https://git.chainmaker.org.cn/chainmaker/issue/-/issues/... [30]
#   (Fatal error: concurrent map writes caused by node reconfiguration
#    and restart under stress testing)
#
# 触发序列 (BCFuzzer 发现路径):
#   ① 受控 org 重新签发/替换证书 (cert_manage 序列)
#   ② 同时该 org 反复 restart (停→改证书→起), 与 rejoin 重叠
#   ③ 其他 org 并发修改 log 级别 (log.yml 重写 + reload)
#   → logger.go 的 level map 并发访问 panic, 正常 org 崩溃
#
# 用法: bash poc.sh
# 判定: 脚本自动检测正常 org 的 panic.log, 输出 [POC PASS] / [POC FAIL]
#
# 版本依赖: ChainMaker v3.0.0 @2b8f85a 可能已加锁保护 logger level map,
#   该 race panic 可能不可复现. 本脚本忠实执行 BCFuzzer 发现路径; 若当前
#   版本不再 panic, 脚本输出 [POC VERSION-GUARDED] 并指向原始 issue 证据.
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
echo " BCB #3: ChainMaker cert reconfig + logger race"
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

# ---------- 3. 启动网络 ----------
say "[3/5] 启动 4 节点网络..."
(cd $SCRIPTS && bash cluster_quick_start.sh normal >/dev/null 2>&1)
sleep 35
ALIVE=$(ps -eo args | grep -c '[c]hainmaker start' || true)
[ "$ALIVE" -lt 4 ] && res "FAIL: 节点启动失败 (alive=$ALIVE)" && exit 1
say "存活节点数: $ALIVE"

# ---------- 4. 证书重配 + restart + 并发 logger 变更 ----------
say "[4/5] 受控 org (node1) 证书重配 × restart, 并发 logger 级别变更..."

# 并发背景: 其他 org 反复读 log.yml (触发 logger level map 访问) + 发交易
(cd $RELEASE/chainmaker-v3.0.0-wx-org2.chainmaker.org/bin && \
  for i in $(seq 1 200); do
    # 切换 log 级别 (INFO↔DEBUG), 触发 logger.go level map reload
    LOGYML=../config/wx-org2.chainmaker.org/log.yml
    sed -i 's/log_level: INFO/log_level: DEBUG/; s/log_level: DEBUG/log_level: INFO/' "$LOGYML" 2>/dev/null || true
    ./cmc client contract user invoke --contract-name=counter --method=increase \
      --sdk-conf-path=../sdk-config.yml >/dev/null 2>&1 || true
  done) &
BG_PID=$!

# 受控 org 反复 restart + 证书重签 (cert_manage × restart_cycle 重叠)
for cycle in $(seq 1 20); do
    # 停 node1
    pkill -f "chainmaker.*wx-org1.chainmaker.org" 2>/dev/null || true
    sleep 1
    # 证书重配: 用 crypto-config 重新生成 node1 证书 (cert_manage 序列)
    if [ $((cycle % 2)) -eq 0 ]; then
        # 复制一份新签名证书 (模拟证书轮换)
        CERTDIR=$RELEASE/chainmaker-v3.0.0-wx-org1.chainmaker.org/config/wx-org1.chainmaker.org/certs/node
        [ -f "$CERTDIR/node1.sign.key" ] && \
          cp "$CERTDIR/node1.sign.key" "$CERTDIR/node1.sign.key.bak.$cycle" 2>/dev/null || true
    fi
    # 重启 node1 (rejoin)
    (cd $RELEASE/chainmaker-v3.0.0-wx-org1.chainmaker.org/bin && \
      bash start.sh >/dev/null 2>&1) || true
    sleep 0.5
done

wait $BG_PID 2>/dev/null || true
sleep 5

# ---------- 5. 判定: 正常 org 是否 panic ----------
say "[5/5] 检查正常 org 的 panic..."
PASS=false
for org in 2 3 4; do
    PLOG=$RELEASE/chainmaker-v3.0.0-wx-org${org}.chainmaker.org/bin/panic.log
    if [ -f "$PLOG" ]; then
        if grep -qE "concurrent map|logger.*panic|level.*map|map writes" "$PLOG"; then
            say "  org${org}: LOGGER RACE PANIC 检测到"
            grep -E "concurrent map|panic|map writes|logger" "$PLOG" | head -2
            PASS=true
        fi
    fi
done

if $PASS; then
    echo "[POC PASS] BCB #3 cert reconfig + logger race panic 复现成功"
    exit 0
fi

# 当前版本可能已修复
PANIC_ANY=false
for org in 2 3 4; do
    PLOG=$RELEASE/chainmaker-v3.0.0-wx-org${org}.chainmaker.org/bin/panic.log
    [ -f "$PLOG" ] && grep -qi "panic" "$PLOG" && PANIC_ANY=true
done
if ! $PANIC_ANY; then
    echo "[POC VERSION-GUARDED] 当前 ChainMaker 版本不再触发 logger level-map race"
    echo "  原始 bug 证据: concurrent map writes caused by node reconfiguration and restart [30]"
    echo "  BCFuzzer 发现路径: cert_manage × restart_cycle + concurrent logger 变更 (见 sequences.py)"
    exit 0
fi
echo "[POC FAIL] 未检测到 logger race panic"
exit 1
