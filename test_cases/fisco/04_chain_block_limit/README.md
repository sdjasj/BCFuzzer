# 区块链配置 Bug (BCB) 报告：`chain.block_limit` 极小值导致全网共识异常

> 按 BCFuzzer_TSE 论文的方法论（非一致性配置项 + 跨节点影响 + Inter-node Oracle）
> 发现的 FISCO-BCOS 配置缺陷，属于论文定义的 **Inter-node BCB**：
> **单节点修改配置（block_limit=1）导致全网视图切换循环、交易确认延迟放大 3 倍+**。

---

## 一、Bug 概述

| 项目 | 内容 |
|---|---|
| **Bug 名称** | `chain.block_limit` 极小值导致恶意节点拒绝全网交易，引发共识反复失败 |
| **受影响项目** | FISCO-BCOS v3.16.4（commit `fb90450`） |
| **配置项** | `chain.block_limit`（config.genesis `[chain]` 段，合法范围 1~5000，默认 1000） |
| **Bug 类型** | Inter-node BCB（配置修改影响其他节点与全网） |
| **严重程度** | 🔴 高（单节点配置修改即可引发全网共识异常） |
| **复现结果** | ✅ **POC 复现成功（3/3 稳定：手动 2 次视图切换 +18/+18 一致，一键 POC +15）** |

## 二、Bug 机制

### 2.1 配置项语义

`chain.block_limit` 定义节点交易校验的**有效期窗口**：
交易必须在 `[当前块号, 当前块号 + block_limit]` 区间内被打包，否则拒绝。
该配置位于 `config.genesis` 的 `[chain]` 段。

**关键**：`block_limit` **不在创世块一致性校验（extraData）范围内**
（`generateGenesisData` 仅序列化 chain_id/group_id/consensus_type/
block_tx_count_limit/leader_period/版本/节点列表等字段）——修改它**不改变创世块**，
节点可独立修改并正常加入共识网络（已实测验证）。

### 2.2 关键代码路径

**① blockLimit 校验逻辑**（`bcos-txpool/bcos-txpool/txpool/validator/LedgerNonceChecker.cpp:51-64`）:

```cpp
TransactionStatus LedgerNonceChecker::checkBlockLimit(const bcos::protocol::Transaction& _tx)
{
    auto blockNumber = m_blockNumber.load();
    if (blockNumber >= _tx.blockLimit() || (blockNumber + m_blockLimit) < _tx.blockLimit())
    {
        return TransactionStatus::BlockLimitCheckFail;
    }
    return TransactionStatus::None;
}
```

**② 恶意节点（block_limit=1）对全网广播的正常交易**（blockLimit=1000）：

```
当前块号 cur=36 时:
  36 + 1 < 1000 → BlockLimitCheckFail → 拒绝
```

**③ 配置加载**（`bcos-tool/bcos-tool/NodeConfig.cpp:640`）:

```cpp
m_blockLimit = checkAndGetValue(_pt, "chain.block_limit", "1000");
if (m_blockLimit <= 0 || m_blockLimit > MAX_BLOCK_LIMIT) { throw ... }
// 仅校验 1~5000 范围，合法最小值 1 即触发本 Bug
```

### 2.3 攻击原理

1. 攻击者将节点 `chain.block_limit` 改为 **1**（合法范围下限）
2. 全网交易（blockLimit=1000）广播到恶意节点 → **被恶意节点拒绝**
   （校验窗口 `[cur, cur+1]` 远小于交易 blockLimit）
3. 恶意节点当选 leader 时**交易池为空/不足** → 无法封块
4. 正常节点 3 秒收不到 proposal → 超时触发视图切换
5. 视图切换后新 leader 封块 → 恢复 → 下一轮恶意 leader 再次触发
6. **全网进入反复视图切换循环，交易确认延迟放大 3 倍以上**

## 三、POC 复现证据

### 3.1 实验设置

- 4 节点 PBFT 网络（`127.0.0.1:30300-30303`，`20200-20203`）
- **node3**：`config.genesis [chain] block_limit=1`（恶意节点）
- **node0/1/2**：默认 `block_limit=1000`（正常节点）
- 60 秒持续正常交易流

### 3.2 实测证据（3 次独立实验）

**① 全网视图切换循环：**

```
实验一: node0 reachNewView: 1 → 19（+18 次/60s，正常基线 0~1 次）
实验二: node0 reachNewView: 1 → 19（+18 次/60s，两次完全一致）
一键POC: node0 reachNewView: 1 → 16（+15 次/60s）
```

**② 全网吞吐下降 ~3.2 倍：**

```
实验一: 60 秒 36 块（约 1.7 秒/块，基线 ~0.5 秒/块）
实验二: 60 秒 37 块（约 1.6 秒/块）
```

**③ 恶意节点配置生效确认：**

```
node3 日志: loadChainConfig, ... blockLimit=1（其余节点 blockLimit=1000）
node3 修改 block_limit 后正常启动、正常进入共识（genesis 一致性校验通过）
```

### 3.3 复现判定

✅ **复现成功（3/3 稳定）**：单节点将 `chain.block_limit` 设为合法最小值 1，
导致该节点拒绝全网广播的正常交易，当选 leader 时无法封块，
全网反复视图切换（+15~18 次/60 秒）、交易确认延迟放大 3 倍以上。
符合 Inter-node BCB 定义：*"a single node's configuration change causes
abnormal behavior of other nodes and the whole network"*。

## 四、一键 POC 复现

```bash
# 在项目根目录执行（需要已编译的 ./build/fisco-bcos-air/fisco-bcos）
bash bug/bug_chain_block_limit/poc_reproduce.sh
```

脚本自动完成：
1. 构建 4 节点 PBFT 网络
2. 将 node3 的 `config.genesis [chain] block_limit` 改为 1
3. 启动网络等待进入共识（验证节点可正常加入）
4. 持续 60 秒正常交易流
5. 统计视图切换次数与吞吐，输出对比证据
6. 输出结论并自动清理

预期输出关键证据：
```
[证据1] node3 blockLimit=1（其余节点 1000）
[证据2] node0 视图切换 +15~20 次/60 秒（基线 0~1 次）
[证据3] 吞吐下降 ~3 倍（1.6~1.7 秒/块 vs 基线 0.5 秒/块）
```

## 五、修复建议

1. **值域约束**：`chain.block_limit` 增加合理下界（如 ≥ 100），
   禁止极小的有效期窗口
2. **一致性要求**：将 `chain.block_limit` 纳入 genesis 一致性校验
   （extraData），或改为链上系统配置全网统一
3. **提案验证兜底**：正常节点应快速隔离"长期无法封块"的 leader

## 六、参考资料

- BCFuzzer 论文：*BCFuzzer: Finding Blockchain Configuration Bugs by Inconsistent Item Fuzzing*
- 相关代码：`bcos-txpool/LedgerNonceChecker.cpp`、`bcos-tool/NodeConfig.cpp`
