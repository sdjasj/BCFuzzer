# Bug 报告: `consensus.round_initial_timeout_ms = 0` 使全网共识永久停滞(配置节点完全存活)

## 1. 漏洞概述

| 项目 | 内容 |
|---|---|
| 漏洞类型 | 配置驱动的共识活性 (liveness) 永久失效 —— **严格 inter-node 类** |
| 影响节点 | **全网**:所有节点存活,但 ledger 永久冻结,交易永远得不到回执 |
| 论文对应 | 与 FISCO BCOS min_seal_time BCB(论文引用 [9])同款场景:单节点合法配置值使全网共识停滞 |
| 测试基线 | aptos-core @ `7f99ad42` (tag `aptos-node-v1.48.5-hotfix`) |
| 复现结果 | ✅ 一键 POC 复现成功(脚本:`./poc.sh`) |

## 2. 漏洞位置与根因

**配置消费点**:`consensus/src/epoch_manager.rs:299`

```rust
fn create_round_state(&self, ...) -> RoundState {
    let time_interval = Box::new(ExponentialTimeInterval::new(
        Duration::from_millis(self.config.round_initial_timeout_ms),  // 无校验
        self.config.round_timeout_backoff_exponent_base,
        self.config.round_timeout_backoff_max_exponent,
    ));
    ...
}
```

每轮本地超时 = `round_initial_timeout_ms × base^min(delta, max_exponent)`。设 `0` 后
每轮超时恒为 0(0 × 任何指数 = 0),**节点在每轮开始瞬间即触发本地超时**:
- 该节点立刻投 nil 票并广播 RoundTimeout,且因为"本轮已投票"而**永远不会投任何
  提案票**(`round_state.vote_sent` 已被 nil 票占用,`round_manager.rs:1089-1107` 的
  超时路径只会重复同一张票);
- 在 2 验证者网络中,QC 需要两张提案票,但配置节点的票永远是 nil → **任何轮次都
  无法形成 QC → 零区块提交**;
- 两个节点的超时票仍能形成 2 链 TC → **轮次通过 TC 无限推进**。

**关键区别(与之前的崩溃类 bug)**:配置节点**完全不崩溃、不退出、正常参与网络**,
只是永远不投提案票 —— 是纯共识活性层面的 inter-node 失效,与 FISCO BCOS 的
min_seal_time 案例完全同构。

## 3. 触发条件

1. 节点 A 配置 `consensus.round_initial_timeout_ms = 0`(其他配置全部默认);
2. A 正常启动、通过身份验证、加入网络(不崩溃);
3. 网络规模为 **2~3 验证者**(quorum = 全部节点,任何一轮都需要 A 的提案票;
   3 验证者实测同样停滞,轮次推进到 1909 零提交)—— 4 验证者网络中
   其余 3 节点可凑成 quorum,A 的 nil 票被绕过,不影响。

## 4. 复现步骤

```bash
cd bugs/round_initial_timeout_zero
./poc.sh            # 可传 aptos-core 仓库路径作为参数
```

脚本自动完成:启动 2 验证者本地网络 → 等网络出块 → 杀掉 forge → SIGKILL 节点 0 →
在 consensus 段插入 `round_initial_timeout_ms: 0` → 用原二进制重启 → 验证
两节点存活 + ledger 冻结 + 轮次无限推进 → 清理。

## 5. 实测证据(一键脚本输出)

```
==> [6/6] 验证:两节点均存活 + ledger 冻结 + 轮次无限推进
node0 ledger_version: 31 -> 31
node1 ledger_version: 31 -> 31
>>> 节点0 进程存活(配置节点未崩溃)
>>> node0 超时次数: 25784, 最近轮次: "round":2041
>>> 复现成功: 两节点均存活但 ledger 冻结,轮次无限推进(TC 循环),全网共识永久停滞 (inter-node 影响确认)
==> 结果: PASS
```

手动验证记录:
- 2 验证者:网络健康时 ledger 正常增长(4→101);节点 0 带缺陷配置重启后,两节点
  进程均存活、ledger 永久冻结在 101(45 秒+ 无增长),节点 0 日志显示
  `Local timeout {"round":3489}` —— 轮次已推进到 3489 而区块高度停留在 50;
- 3 验证者:三节点均存活、ledger 冻结在 31(30 秒+ 无增长),节点 0 轮次推进到
  round 1909 零提交 —— 影响范围覆盖 2~3 验证者网络;
- **4 验证者(攻击扩展)**:2 个节点(0、1)配该配置后,4 节点全部存活但 ledger
  冻结在 49(30 秒+ 无增长)—— 任意 N 验证者网络中,ceil(N/3) 个节点配该配置
  即可使全网停滞(提案票数不足 quorum)。

## 6. Inter-node 影响分析

- **全网交易永久无法确认**(论文 inter-node oracle 的"合法交易收不到回执"信号);
- 配置节点本身完全健康(不崩溃、不报错),运维难以察觉是它的配置导致;
- 攻击者可用此配置合法地瘫痪 2 验证者网络(或任何 quorum 需要该节点票数的网络);
- 与 FISCO BCOS min_seal_time(#4656)一致:单个节点的合法配置值 → 全网共识停滞。

## 7. 修复建议

1. `ConsensusConfig::validate()` 要求 `round_initial_timeout_ms > 0`;
2. 超时票与提案票的互斥语义下,若节点因配置必然每轮超时,应在本地检测到
   "连续 N 轮超时且从未投票"时告警/主动退出,而不是静默拖垮全网;
3. 参考 FISCO 的修复思路:对 leader/投票相关配置做范围校验(如下限 100ms)。
