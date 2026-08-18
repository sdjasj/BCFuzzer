# Bug 报告: `consensus.sync_only = true` 使全网共识永久停滞(配置节点存活但不参与投票)

## 1. 漏洞概述

| 项目 | 内容 |
|---|---|
| 漏洞类型 | 配置驱动的共识活性 (liveness) 永久失效 —— **严格 inter-node 类** |
| 影响节点 | **全网**:所有节点存活,但 ledger 永久冻结,交易永远得不到回执 |
| 论文对应 | 与 FISCO BCOS min_seal_time BCB 同款场景:单节点合法配置使全网共识停滞 |
| 测试基线 | aptos-core @ `7f99ad42` (tag `aptos-node-v1.48.5-hotfix`) |
| 复现结果 | ✅ 一键 POC 复现成功(脚本:`./poc.sh`) |

## 2. 漏洞位置与根因

**配置消费点**:`consensus/src/round_manager.rs:1042`

```rust
if self.sync_only() {
    self.network.broadcast_sync_info(self.block_store.sync_info()).await;
    bail!("[RoundManager] sync_only flag is set, broadcasting SyncInfo");
}
```

`sync_only` 设计用于节点维护/升级:节点只广播同步信息,不提案、不投票。该配置**无任何
网络侧防护**:节点可以带着 `sync_only = true` 加入验证者集合并正常通过身份验证。
在 2/3 验证者网络中(quorum = 全部节点),该节点永远不投提案票 → 任何区块都凑不齐
QC → 轮次靠 TC 无限推进、零区块提交 → **全网永久停滞**,而配置节点本身完全健康。

`sync_only` 语义上"本节点不参与",但其对网络的影响与 FISCO min_seal_time 相同:
**其他节点没有任何机制检测到"一个已入网验证者拒绝投票"并保护共识活性**。

## 3. 触发条件

1. 节点 A 配置 `consensus.sync_only = true`(合法配置,无校验);
2. A 正常启动、通过身份验证、加入网络(不崩溃,持续广播 SyncInfo);
3. 网络规模 2~3 验证者(quorum 需要 A 的提案票)。

## 4. 复现步骤

```bash
cd bugs/sync_only_true
./poc.sh            # 可传 aptos-core 仓库路径作为参数
```

脚本自动完成:启动 2 验证者本地网络 → 等网络出块 → 杀掉 forge → SIGKILL 节点 0 →
在 consensus 段插入 `sync_only: true` → 用原二进制重启 → 验证两节点存活 +
ledger 冻结 + 轮次推进 → 清理。

## 5. 实测证据(一键脚本输出)

```
==> [6/6] 验证:两节点均存活 + ledger 冻结 + 轮次无限推进
node0 ledger_version: 37 -> 37
node1 ledger_version: 37 -> 37
>>> 节点0 进程存活(配置节点未崩溃)
>>> node0 超时次数: 112, 最近轮次: "round":19
>>> 复现成功: 两节点均存活但 ledger 冻结,轮次无限推进(TC 循环),全网共识永久停滞 (inter-node 影响确认)
==> 结果: PASS
```

## 6. Inter-node 影响分析

- **全网交易永久无法确认**;配置节点与正常节点均不崩溃、无错误日志,运维难以定位;
- 攻击者可用此配置合法地瘫痪 2~3 验证者网络;
- 与 `round_initial_timeout_ms = 0`(见 `bugs/round_initial_timeout_zero/`)同为
  "配置节点存活但全网停滞"类,是同一防护缺失(网络不检测"已入网验证者不投票")
  的两个实例。

## 7. 修复建议

1. 网络侧检测:验证者若在连续 N 轮既不投票也不提案,其他节点应能生成其
   "缺席证明"并最终将其移出验证者集合(类 Liveness 检测机制);
2. 配置侧:`sync_only` 仅允许在节点启动时一次性使用,禁止已入网节点热切换;
3. 2~3 验证者网络启动时校验:若任何验证者配置了 `sync_only`,拒绝其入网或拒绝
   整个网络启动并提示。
