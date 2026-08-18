# 区块链配置 Bug (BCB) 报告：`txpool.check_block_limit`

> 按 BCFuzzer_TSE 论文的方法论（非一致性配置项 + 跨节点影响 + Inter-node Oracle）
> 发现的 FISCO-BCOS 配置缺陷，属于论文定义的 **Inter-node BCB**。

---

## 一、Bug 概述

| 项目 | 内容 |
|---|---|
| **Bug 名称** | 关闭 blockLimit 校验的节点可向全网注入过期交易，引发共识反复失败 |
| **受影响项目** | FISCO-BCOS v3.16.4（commit `fb90450`） |
| **配置项** | `txpool.check_block_limit`（默认 true） |
| **Bug 类型** | Inter-node BCB（区块链配置 Bug，影响其他节点） |
| **严重程度** | 🔴 高（单节点配置即可注入全网无法确认的交易，导致共识异常） |
| **复现结果** | ✅ **POC 复现成功**（视图切换 +25 次/60 秒） |

## 二、Bug 机制

### 2.1 配置项语义

`txpool.check_block_limit` 控制节点在接受交易时**是否校验交易的 blockLimit**
（交易必须在 `[当前块号, 当前块号 + block_limit]` 区间内被打包）。
该配置属于**非一致性配置项**——各节点可独立设置，节点仍能加入共识网络。

### 2.2 关键代码路径

**① 恶意节点跳过 blockLimit 校验**

`bcos-txpool/bcos-txpool/txpool/validator/LedgerNonceChecker.cpp:44-49`:

```cpp
if (m_checkBlockLimit && _tx.type() == static_cast<uint8_t>(TransactionType::BCOSTransaction))
{  // check blockLimit
    return checkBlockLimit(_tx);
}
return TransactionStatus::None;
```

**② blockLimit 校验逻辑**（正常节点）

`bcos-txpool/bcos-txpool/txpool/validator/LedgerNonceChecker.cpp:51-64`:

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

**③ 正常节点验证 leader 提案**

正常节点验证提案时，块中的交易若不在本地交易池，需向 leader 拉取；
拉取后经 `batchVerifyAndSubmitTransaction` 走完整校验链（含 blockLimit 检查）→
校验失败 → **提案验证失败 → 视图切换**。

### 2.3 攻击原理

1. 攻击者将节点 `txpool.check_block_limit` 设为 `false`
2. 通过该节点的 RPC 提交 **blockLimit 已过期的合法签名交易**（如 blockLimit=0）——
   恶意节点跳过校验接受并存交易池（该交易全网永远无法共识，RPC 提交同步等待永久挂起）
3. 恶意节点当选 leader 时，封块包含过期交易并广播提案
4. 正常节点（校验 blockLimit=true）验证提案：过期交易不在本地交易池 →
   向恶意节点拉取 → 拉取后 blockLimit 校验失败 → **提案验证失败 → 视图切换**
5. 视图切换后新 leader 重新封块 → 恢复 → 下一轮恶意 leader 再次触发
6. **全网进入反复视图切换循环，交易确认延迟放大数倍**

## 三、POC 复现证据

### 3.1 实验设置

- 4 节点 PBFT 网络（`127.0.0.1:30300-30303`，`20200-20203`）
- **node3**：`txpool.check_block_limit=false`（恶意节点）
- **node0/1/2**：默认 `true`（正常节点）
- **关键步骤：网络运行中重启 node3**（清空其交易池，确保注入的过期交易是交易池中最早的交易，必然被其封入区块）
- 重启后向 node3 的 RPC（20203）注入 2 笔 blockLimit=0 的**合法签名**过期交易
- 随后 60 秒持续正常交易流

### 3.2 实测证据

**① 恶意节点接受过期交易（RPC 提交挂起）：**

```
向 node3 提交 blockLimit=0 交易: 连接超时（node3 已接受存入交易池，
但全网永远无法共识 → RPC 同步等待永不返回）
```

**② 全网视图疯狂切换（60 秒 +25 次）：**

```
node0 reachNewView 次数: 2 → 27（+25 次，正常基线 0~1 次）
node0/node3 的 view 值持续飙升: view=34, 35, 36 ...
```

**③ 全网吞吐下降 ~2 倍，伴随周期性停滞：**

```
基线: ~0.5 秒/块
恶意配置: 60 秒仅 59 块（约 1.0 秒/块），且出现 5 秒级停滞
（t+5s=1, t+10s=1; t+15s=4, t+20s=4 ...）
```

### 3.3 复现判定

✅ **复现成功**：单节点关闭 blockLimit 校验（合法配置修改），
即可向全网注入过期交易，导致正常节点共识验证反复失败、
视图切换循环（60 秒 25 次）、交易确认延迟放大数倍。
符合 Inter-node BCB 定义：*"a single node's configuration change causes
abnormal behavior of other nodes"*。

## 四、一键 POC 复现

```bash
# 在项目根目录执行（需要已编译的 ./build/fisco-bcos-air/fisco-bcos）
bash bug/bug_check_block_limit/poc_reproduce.sh
```

脚本自动完成：
1. 构建 4 节点 PBFT 网络
2. 将 node3 的 `txpool.check_block_limit` 设为 false
3. 启动网络等待进入共识
4. **重启 node3**（关键：清空其交易池）
5. 向 node3 注入 blockLimit=0 的过期交易（合法签名）
6. 持续 60 秒正常交易流
7. 统计视图切换次数与吞吐，输出对比证据
8. 输出结论并自动清理

预期输出关键证据：
```
[证据1] 过期交易注入成功（RPC 同步等待挂起）
[证据2] node0 视图切换 +15~25 次/60 秒（基线 0~1 次）
[证据3] view 值持续飙升（30+），吞吐下降 ~2 倍
```

## 五、修复建议

1. **禁止关闭 blockLimit 校验**：`check_block_limit=false` 仅允许开发/测试模式，
   生产环境强制开启；或将其改为一致性配置项（全网统一）
2. **提案验证兜底**：正常节点验证提案时，若拉取到的交易 blockLimit 非法，
   应立即拒绝提案并标记该 leader 为可疑（当前仅触发视图切换，恶意节点可反复触发）
3. **统一校验策略**：交易同步（TransactionSync）与提交（submitTransaction）
   应使用相同的 blockLimit 校验策略，杜绝"提交不校验、同步才校验"的不对称

## 六、参考资料

- BCFuzzer 论文：*BCFuzzer: Finding Blockchain Configuration Bugs by Inconsistent Item Fuzzing*
- 相关代码：`bcos-txpool/LedgerNonceChecker.cpp`、`bcos-txpool/TxPool.cpp`、`bcos-txpool/TransactionSync.cpp`
