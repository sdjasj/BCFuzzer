# Bug 09: 单节点配置 batch 池 + 链上 turbo/gas → 该节点裁剪块 TxCount 虚高 → 其他节点全部崩溃（纯配置 Inter-node Crash BCB）

> 复现状态：**✅ 复现成功**（2026-08-01，chainmaker-go v3.0.0，commit 2b8f85a）
> 对应论文：BCFuzzer (TSE) 中 **Inter-node Oracle 的「normal nodes crash」类别**
> **纯配置触发（零代码修改）：仅一个节点修改自己的 `txpool.pool_type`，即可使其他全部节点崩溃**

## 一、漏洞概述

| 项目 | 内容 |
|------|------|
| 漏洞类型 | Blockchain Configuration Bug (BCB)，Transaction 类，**Crash 类** |
| 影响范围 | **Inter-node**：一个节点修改本地配置 `pool_type: "batch"` 后，**其他全部节点进程崩溃** |
| 严重程度 | **严重**（其他节点直接 crash，全网共识瘫痪；攻击者无需修改代码） |
| 受影响配置项 | ① 节点本地 `chainmaker.yml` → `txpool.pool_type: "batch"`；② 链配置 `consensus_message_turbo: true` + `enable_gas: true` + `enable_optimize_charge_gas: true` |
| 受影响版本 | chainmaker-go v3.0.0（tbft-engine v1.0.2） |

## 二、漏洞根因

`GetTurboBlock`（block_helper.go:1414-1431）在** batch 池 + coinbase 开启**时：

```go
if TxPoolType == batch.TxPoolType {
    if coinbasemgr.CheckCoinbaseEnable(chainConf) {
        turboBlock.Txs = []*commonPb.Transaction{block.Txs[block.Header.TxCount-1]}
        //         ↑ 裁剪块只保留 coinbase 一笔交易 (len(Txs) = 1)
    }
    return turboBlock
    // ↑ 但 Header 仍是原块头: TxCount = 普通交易数 + 1 (含 coinbase)
}
```

**广播的裁剪块：`Header.TxCount = N+1`（N 笔普通交易 + 1 笔 coinbase），而 `Txs` 只含 1 笔 coinbase → `TxCount > len(Txs)`！**

其他节点（默认 normal 池）验证时（`recoverBlock` turbo 路径 → coinbase 开启 → `recoverBlockWithCoinBaseTx`）：

```go
// block_helper.go:1679
if !coinbasemgr.IsCoinBaseTx(block.Txs[block.Header.TxCount-1]) {   // ← block.Txs[N] 越界!
```

`block.Txs[TxCount-1]` 越界（len(Txs)=1）→ **panic → 进程崩溃**。

**该崩溃在共识消息处理 goroutine（`syncmode.(*CoreEngine).OnMessage`）中，无 recover → 节点直接崩溃。**

## 三、攻击场景（纯配置，无需改代码）

1. 链上开启 turbo + gas 计费（创世或治理配置，合法可选项）；
2. **攻击者节点仅修改自己的 `txpool.pool_type: "batch"`**（合法本地配置，yaml 注释明示可选项）；
3. 攻击者节点作为 proposer **正常打包**（无任何恶意构造）→ `GetTurboBlock` 自动产生 `TxCount > len(Txs)` 的裁剪块；
4. **所有其他节点（默认配置）验证该块 → 越界 panic → 进程全部崩溃**（实测 node2/3/4 同时崩溃）；
5. 全网仅剩攻击者节点 → 共识瘫痪、交易永久挂起。

## 四、复现现象（关键日志）

### 1. 崩溃栈（node2/3/4 完全一致）

```
panic: runtime error: index out of range [1] with length 1
goroutine [running]:
	module/core/common/block_helper.go:1679 +0x5d5   ← IsCoinBaseTx(block.Txs[TxCount-1])
	module/core/common/block_helper.go:1633 +0x439   ← recoverBlockWithCoinBaseTx
	module/core/common/block_helper.go:1480 +0xdd    ← RecoverBlock
	module/core/syncmode/verifier/block_verifier_impl.go:222  ← 共识验证路径
```

### 2. 全网状态：仅恶意节点（batch 池）存活

```
ps -eo pid,args | grep '[c]hainmaker start'
# 仅 node1（配置了 batch 池）存活, node2/3/4（默认配置）进程消失
```

## 五、修复建议

1. `GetTurboBlock` batch 分支（block_helper.go:1422-1431）裁剪后**同步修正块头 `TxCount`**：
   ```go
   turboBlock.Header.TxCount = uint32(len(turboBlock.Txs))
   ```
2. 或 `recoverBlockWithCoinBaseTx`（block_helper.go:1679）增加越界防御：
   ```go
   if block.Header.TxCount == 0 || uint32(len(block.Txs)) < block.Header.TxCount {
       return nil, nil, fmt.Errorf("invalid tx count: %d, txs: %d", ...)
   }
   ```
3. 同时在共识消息处理 goroutine 外层增加 recover 兜底。

## 六、POC 判定标准（poc.sh 一键复现，纯配置无源码修改）

```
[1] node1 本地配置 pool_type=batch（其余节点默认）; 链配置 turbo+gas
[2] 发交易后, node2/3/4 panic.log 出现 "index out of range"（block_helper.go:1679）→ 进程崩溃
[3] 全网仅 node1 存活
[1]&[2]&[3] 满足 → BUG 复现成功
```
