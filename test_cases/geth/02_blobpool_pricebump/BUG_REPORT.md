# Bug 报告：`--blobpool.pricebump 1000000` → 出块节点拒绝 blob 交易替换，挖出旧版交易（Inter-node BCB）

> 基于 BCFuzzer (TSE) 论文的区块链配置 Bug (BCB) 方法论复现
> 目标：go-ethereum v1.17.5（commit `9621c6ad10934a01b5514886fb6fbd87640b6c05`）

---

## 1. 概述

| 项目 | 内容 |
|---|---|
| **Bug 类型** | **Inter-node BCB（f=1 容错网络下依然有效）**：出块节点的 blob 池替换阈值配置使 EIP-4844 blob 交易替换全网失效，链上打包**旧版 blob 交易** |
| **受影响配置** | `--blobpool.pricebump`（blob 交易替换所需的最低加价百分比） |
| **触发值** | `1000000`（需 10001× 加价，`sanitize()` 只校验 `<1`，**完全合法**） |
| **影响** | 用户的 blob 交易替换（改价/改收款方/改金额）在出块者处被拒绝；该节点挖出**旧版 blob 交易**，用户的替换意图随 nonce 消耗而**全网永久失效**（其他 proposer 无法挽回） |
| **复现状态** | ✅ 已复现（双节点 Osaka 网络实测，含正常节点对照） |

## 2. 根本原因

`core/txpool/blobpool/blobpool.go:1677-1692` 的替换阈值逻辑：

```go
// blobpool.go
multiplier = uint256.NewInt(100 + p.config.PriceBump)   // 100 + 1000000
minBlobGasFeeCap = prev.blobFeeCap × multiplier / 100   // = 旧值 × 10001
...
case tx.BlobGasFeeCapIntCmp(minBlobGasFeeCap.ToBig()) < 0:
    return fmt.Errorf("%w: new tx blob gas fee cap %v < %v queued + %d%% replacement penalty",
        txpool.ErrReplaceUnderpriced, ..., p.config.PriceBump)
```

blob 替换必须同时超过旧交易的三项费用（gas fee cap、gas tip cap、blob gas
fee cap）各 `(100+PriceBump)%`。`--blobpool.pricebump` 通过
`cmd/utils/flags.go:1708` 直接写入，`sanitize()`（`config.go:47-48`）只校验
`<1`，`1000000` 完全合法。

攻击链（与 BCB-7 同型，作用于 blob 池）：
1. 用户的 blob 交易 A（收款方 X）进入全网交易池；
2. 用户提交替换 A'（同 nonce、+150% 费用、收款方 Y —— 修正意图）；
3. 正常节点（默认 pricebump=100，需 2×）接受替换；
4. 出块节点（pricebump=1000000，需 10001×）**拒绝** A'，池中仍为旧版 A；
5. 出块节点挖出 A（收款方 X）→ nonce 被消耗 → 用户的替换意图
   **全网永久失效**（f=1 容错网络下亦然，其他 proposer 无法挽回）。

## 3. 复现步骤

运行一键复现脚本（需先 `make geth`；python3 需 `eth_account`/`requests`/`ckzg`
+ KZG 可信设置文件 `TSETUP`）：

```bash
cd /home/geth/tse/go-ethereum
./bugs/bcb-blobpool-pricebump/poc_bcb13_blobpool_pricebump.sh
```

脚本自动完成：
1. 构造 **Cancun+Prague+Osaka** Clique 创世块（blob v1 侧车，Engine API V5
   出块流：FCU V3 + GetPayloadV5 + NewPayloadV4）；
2. 双节点：node1 正常；node2 `--blobpool.pricebump 1000000`（出块者）；
3. 构造真实 blob 交易 A（v1 sidecar + cell proofs）与替换 A'（+150% 费用、
   收款方 Y）；
4. 对照：A 与 A' 提交到正常节点 node1（均接受）；
5. 提交 A 与 A' 到出块者 node2（A 接受；A' 被 `+1000000%` 阈值拒绝）；
6. 假共识客户端驱动 node2 出块 → 区块 #1 打包旧版 A（收款方 X）。

## 4. 复现结果

### 替换分歧（核心证据）

```
[*] 提交 blob 交易 A 到 node1 (对照: 正常节点接受)...
  node1 响应: 0x90b688e743a4b3df2e31de172ca137c0c710aee7a9e5ca171932e0684af1b5b0

[*] 提交 blob 替换 A' 到 node1 (对照: 正常节点应接受替换)...
  node1 响应: 0x996f57f766cb8e7fe74baff2a635ea7e94ccfe09181c23fbbb3aa08482c51052   ← 接受

[*] 提交 blob 替换 A' 到 node2 (出块者, 应被 pricebump 拒绝)...
  node2 响应: {"error":{"code":-32000,"message":"replacement transaction underpriced:
              new tx gas fee cap 50000000000 < 20000000000 queued
              + 1000000% replacement penalty"}}                                     ← 拒绝！
```

### 出块者挖出旧版 blob 交易

```
[*] 假共识客户端驱动 node2 出块 (5 块)...
  区块#1 挖出的 blob 交易: 0x90b688e743a4b3df2e31de172ca137c0c710aee7a9e5ca171932e0684af1b5b0
  详情: to=0x71562b71999873db5b286df9577581998cbf4e81 maxFeePerBlobGas=1000000000
        ↑ 正是旧版 A（收款方 X, 1 gwei blob 费）—— 挖出的交易哈希与提交 A 时完全一致

[POC 复现成功 ✓]
```

## 5. 影响分析

- **永久性（f=1 有效）**：出块者挖出旧版 A 消耗 nonce 后，用户的替换 A'
  （收款方 Y）在**全网**永久失效 —— 与 BCB-7 同类，且不依赖"攻击者是唯一
  出块者"；4 节点 f=1 网络下其他 proposer 也无法挽回；
- **攻击场景**：恶意节点将 `--blobpool.pricebump` 配置为极大合法值后加入
  网络并争取成为出块者。其打包的任何被替换 blob 交易都是旧版 —— L2
  数据上链用户的改价/纠错替换被系统性丢弃，链上结果与用户意图不符；
- **与 BCB-7 的差异**：BCB-7 作用于普通交易池（`--txpool.pricebump`）；
  本 Bug 作用于 blob 交易池（`--blobpool.pricebump`）与 EIP-4844 负载；
- **隐蔽性**：节点运行正常，仅表现为"blob 替换失败"。

## 6. 附带发现（同源 fork 缺陷）

复现过程中确认：**pre-Osaka（Cancun）链上 blob 交易永远无法被打包**：
- 交易池校验**无条件要求 v1 侧车**（`core/txpool/validation.go:190`）；
- 矿工过滤器在 pre-Osaka 链上请求 **BlobVersion 0**（`miner/worker.go:581`）；
- 两者永不相交 → blob 交易在 Cancun 链上无法出块（BCB-13 必须使用
  Osaka 链演示，正因如此）。

## 7. 修复建议

1. 为 `--blobpool.pricebump` 增加上限校验（如 ≤ 10000），`sanitize()` 对
   过大值告警并修正；
2. 修复 pre-Osaka 链的 blob 打包缺陷（worker.go:581 的 BlobVersion 与
   validation.go:190 的要求对齐）；
3. 参考 BCFuzzer 论文思路：对不一致配置项学习合法取值范围，越界时提示。

## 8. 复现环境

- go-ethereum v1.17.5 stable（`9621c6ad`），Go 1.24
- Linux 5.15, python3.10 + eth-account 0.13.7 / requests / ckzg
- 双节点 Cancun+Prague+Osaka Clique（chainId=15）本地网络，Engine API V5
  假共识客户端（FCU V3 / GetPayloadV5 / NewPayloadV4）
