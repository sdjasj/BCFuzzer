# 区块链配置 Bug (BCB) 报告：`experimental.check_transaction_signature`

> 按 BCFuzzer_TSE 论文的方法论（非一致性配置项 + 跨节点影响 + Inter-node Oracle）
> 发现的 FISCO-BCOS 配置缺陷，属于论文定义的 **Inter-node BCB**。

---

## 一、Bug 概述

| 项目 | 内容 |
|---|---|
| **Bug 名称** | 关闭交易签名校验的节点可向全网注入坏签名交易，引发共识反复失败 |
| **受影响项目** | FISCO-BCOS v3.16.4（commit `fb90450`） |
| **配置项** | `experimental.check_transaction_signature`（默认 true） |
| **Bug 类型** | Inter-node BCB（区块链配置 Bug，影响其他节点） |
| **严重程度** | 🔴 高（单节点配置即可向全网注入非法交易，导致共识异常） |
| **复现结果** | ✅ **POC 复现成功** |

## 二、Bug 机制

### 2.1 配置项语义

`experimental.check_transaction_signature` 控制节点在接受交易时**是否验证交易签名**。
该配置属于**非一致性配置项**——各节点可独立设置，节点仍能加入共识网络。

### 2.2 关键代码路径

**① 恶意节点跳过签名验证**

`bcos-txpool/bcos-txpool/txpool/storage/MemoryStorage.cpp:326-329`:

```cpp
// Step 2: Verify transaction signature (if enabled)
return m_config->checkTransactionSignature() ?
           m_config->txValidator()->verify(*transaction) :
           TransactionStatus::None;
```

**② 正常节点在交易同步时验证签名**

`bcos-txpool/bcos-txpool/sync/TransactionSync.cpp:426-443`:

```cpp
if (m_checkTransactionSignature)
{
    try
    {
        // force sender to empty for the txs verification
        tx->forceSender({});
        // verify failed, it will throw exception
        tx->verify(*m_hashImpl, *m_signatureImpl);
    }
    catch (std::exception const& e)
    {
        tx->setInvalid(true);
        SYNC_LOG(WARNING) << LOG_DESC("verify sender for tx failed") ...
        verifySuccess = false;
    }
}
```

**③ 正常节点验证 leader 提案（proposal）中的交易**

`bcos-txpool/bcos-txpool/TxPool.cpp:239` → `batchVerifyProposal`：
块中的交易若不在本地交易池（missedTxs），需从 leader 拉取；
拉取时签名验证失败 → `verifySuccess=false` → **提案验证失败 → 视图切换**。

### 2.3 攻击原理

1. 攻击者将节点 `experimental.check_transaction_signature` 设为 `false`
2. 通过该节点的 RPC 提交**坏签名交易**（签名全零/随机）——恶意节点接受并存交易池
3. 恶意节点当选 leader 时，封块包含坏签名交易并广播提案
4. 正常节点（校验签名=true）验证提案：坏签名交易不在本地交易池 → 向恶意节点拉取
5. 拉取到的交易签名验证失败 → **提案验证失败 → 视图切换**
6. 视图切换后新 leader 重新封块（不含坏交易）→ 恢复 → 下一轮恶意 leader 再次触发
7. **全网进入反复视图切换循环，交易确认延迟放大数倍**

## 三、POC 复现证据

### 3.1 实验设置

- 4 节点 PBFT 网络（`127.0.0.1:30300-30303`，`20200-20203`）
- **node3**：`experimental.check_transaction_signature=false`（恶意节点）
- **node0/1/2**：默认 `true`（正常节点）
- 向 node3 的 RPC（20203）提交 2 笔签名全零的坏交易
- 随后 60 秒持续正常交易流

### 3.2 实测证据

**① 恶意节点接受坏签名交易（RPC 提交挂起）：**

```
向 node3 提交坏签名交易: 连接超时（交易被 node3 接受存入交易池，
但永远无法被全网共识打包 → RPC 同步等待永不返回）
```

**② 正常节点反复验证失败：**

```
node0 日志（60 秒内 80 次）:
[SYNC]verify sender for tx failed,reason=...Secp256k1Crypto.cpp(195):
Throw ... recoverAddress failed   ← 坏签名交易签名恢复失败
```

**③ 全网视图切换疯狂循环：**

```
node0 reachNewView 次数: 6 → 20（60 秒内 +14 次，正常基线 0~1 次）
```

**④ 全网吞吐下降 3 倍：**

```
基线: ~0.5 秒/块（60 秒 ~120 块）
恶意配置: 60 秒仅 38 块（约 1.6 秒/块），与视图切换循环吻合
```

### 3.3 复现判定

✅ **复现成功**：单节点关闭交易签名校验（合法配置修改），
即可向全网注入坏签名交易，导致正常节点共识验证反复失败、
视图切换循环、交易确认延迟放大 3 倍。符合 Inter-node BCB 定义：
*"a single node's configuration change causes abnormal behavior of other nodes"*。

## 四、一键 POC 复现

```bash
# 在项目根目录执行（需要已编译的 ./build/fisco-bcos-air/fisco-bcos）
bash bug/bug_check_transaction_signature/poc_reproduce.sh
```

脚本自动完成：
1. 构建 4 节点 PBFT 网络
2. 将 node3 的 `experimental.check_transaction_signature` 设为 false
3. 启动网络等待进入共识
4. 向 node3 注入坏签名交易（签名全零）
5. 持续 60 秒正常交易流
6. 统计视图切换次数与吞吐，输出对比证据
7. 输出结论并自动清理

预期输出关键证据：
```
[证据1] 坏签名交易注入 node3 成功（RPC 挂起）
[证据2] node0 签名验证失败事件 80 次（verify sender for tx failed）
[证据3] 视图切换 +14 次/60 秒（基线 0~1 次）
[证据4] 吞吐下降 ~3 倍（1.6 秒/块 vs 基线 0.5 秒/块）
```

## 五、修复建议

1. **禁止关闭交易签名校验**：`check_transaction_signature=false` 仅允许在
   开发/测试模式启用，生产环境强制开启；或将其改为一致性配置项（全网统一）
2. **提案验证兜底**：正常节点验证提案时，若拉取到的交易签名非法，
   应**立即拒绝提案并标记该 leader 为可疑**（当前仅触发视图切换，恶意节点可反复触发）
3. **交易广播验证**：交易同步（TransactionSync）与提交（submitTransaction）应使用
   相同的签名校验策略，杜绝"提交不校验、同步才校验"的不对称

## 六、参考资料

- BCFuzzer 论文：*BCFuzzer: Finding Blockchain Configuration Bugs by Inconsistent Item Fuzzing*
- 相关代码：`bcos-txpool/MemoryStorage.cpp`、`bcos-txpool/TransactionSync.cpp`、`bcos-txpool/TxPool.cpp`
