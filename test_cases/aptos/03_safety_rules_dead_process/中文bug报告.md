# Bug 报告: `safety_rules.service` 指向不可达的 process 服务 → 节点存活但无法签名,全网共识永久停滞

## 1. 漏洞概述

| 项目 | 内容 |
|---|---|
| 漏洞类型 | 配置驱动的共识活性 (liveness) 永久失效 —— **严格 inter-node 类** |
| 影响节点 | **全网**:所有节点存活,但 ledger 永久冻结,交易永远得不到回执 |
| 论文对应 | 与 FISCO BCOS min_seal_time BCB 同款场景:单节点合法配置使全网共识停滞 |
| 测试基线 | aptos-core @ `7f99ad42` (tag `aptos-node-v1.48.5-hotfix`) |
| 复现结果 | ✅ 一键 POC 复现成功(脚本:`./poc.sh`) |

## 2. 漏洞位置与根因

**配置消费点**:`config/src/config/safety_rules_config.rs:206`(`SafetyRulesService::Process`)

```rust
pub enum SafetyRulesService {
    Local,
    Process(RemoteService),   // 生产部署模式:独立安全规则进程
    Serializer,
    Thread,
}
```

把 `consensus.safety_rules.service` 设为 `{ type: process, server_address: "/ip4/127.0.0.1/tcp/9999" }`
(合法的 yaml,指向一个没有进程监听的地址):

- 节点**正常启动、通过身份验证、加入网络、完全不崩溃**(配置无任何启动期校验,
  地址只在运行时使用);
- 安全规则客户端在 `secure/net` 层对每一条 RPC(投票签名、提案签名、超时签名)
  **无限重试连接失败**(实测日志:每 ~100ms 一条 `ConnectionRefused`);
- 节点无法签名任何投票/提案/超时 → **永不投票** → 2 验证者网络中任何区块凑不齐
  QC → **ledger 永久冻结**。

与 bugs 18/19(round_initial_timeout_ms=0、sync_only=true)同为"配置节点存活但
全网停滞"类,是同一防护缺失(网络不检测"已入网验证者不投票")的第三个实例;
区别在于本配置是**生产部署模式**(process 型 safety rules)下的常见误配置
(地址写错/进程未启动),攻击面更现实。

## 3. 触发条件

1. 节点 A 配置 `consensus.safety_rules.service.type = process`,`server_address` 指向
   无服务监听的地址(合法 yaml,无校验);
2. A 正常启动、加入网络(不崩溃,持续重试连接);
3. 网络规模 2~3 验证者(quorum 需要 A 的提案票;4 验证者中 3/4 quorum 可绕过,
   但 2 个节点同样配置即可停滞全网,与 bug 18 的攻击扩展一致)。

## 4. 复现步骤

```bash
cd bugs/safety_rules_dead_process
./poc.sh            # 可传 aptos-core 仓库路径作为参数
```

脚本自动完成:启动 2 验证者本地网络 → 等网络出块 → 杀掉 forge → SIGKILL 节点 0 →
将 `safety_rules.service` 改为 `process + 死地址` → 用原二进制重启 → 验证
两节点存活 + ledger 冻结 + 安全规则连接失败 → 清理。

## 5. 实测证据(一键脚本输出)

```
==> [6/6] 验证:两节点均存活 + ledger 冻结 + 轮次无限推进
node0 ledger_version: 35 -> 35
node1 ledger_version: 35 -> 35
>>> 节点0 进程存活(配置节点未崩溃)
>>> node0 安全规则连接失败次数: 548
>>> 复现成功: 两节点均存活但 ledger 冻结,配置节点无法签名(安全规则不可达),全网共识永久停滞 (inter-node 影响确认)
==> 结果: PASS
```

手动验证记录:节点 0 日志持续输出
`secure/net/src/lib.rs:244 {"error":"NetworkError(Os { code: 111, kind: ConnectionRefused, ...`(~每 100ms 一条);
正常节点 1 的 ledger 冻结在 45(45 秒+ 无增长)。

## 6. Inter-node 影响分析

- **全网交易永久无法确认**;配置节点与正常节点均不崩溃、无显著错误日志
  (ConnectionRefused 仅为 WARN 级),运维难以定位;
- 攻击者可用此配置合法地瘫痪 2~3 验证者网络(或与 bug 18 相同,4 验证者网络
  中 2 个节点同时配置);
- 该配置是**生产标准部署形态**(独立 safety-rules 进程)下的误配置,`server_address`
  无任何启动期校验 —— 一个地址笔误即可造成全网停滞。

## 7. 修复建议

1. **启动期校验**:`SafetyRulesService::Process` 的 `server_address` 应在节点启动时
   做连接预检(超时失败则拒绝启动并给出明确错误),而不是运行时无限重试;
2. 网络侧检测:验证者若在连续 N 轮既不投票也不提案,其他节点应能生成其
   "缺席证明"并最终将其移出验证者集合(与 bugs 18/19 同一修复方向);
3. 对 safety-rules 客户端增加熔断:连续失败超过阈值后主动退出节点进程
   (fail-fast),而不是静默拖垮全网。
