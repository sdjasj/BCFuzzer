# Bug 报告：`--miner.gaslimit 5000` → 全网 gas limit 塌缩 98%，交易吞吐被永久压垮（Inter-node BCB）

> 基于 BCFuzzer (TSE) 论文的区块链配置 Bug (BCB) 方法论复现
> 目标：go-ethereum v1.17.5（commit `9621c6ad10934a01b5514886fb6fbd87640b6c05`）

---

## 1. 概述

| 项目 | 内容 |
|---|---|
| **Bug 类型** | **Inter-node BCB（共识级）**：单个出块节点的配置通过**共识合法的区块**永久压垮全网交易吞吐 |
| **受影响配置** | `--miner.gaslimit`（矿工目标 gas limit） |
| **触发值** | `5000`（= 协议下限 `MinGasLimit`，**完全合法**，无任何校验） |
| **触发方式** | 该节点担任出块者连续出块（其区块全部满足共识规则，其他节点必须接受） |
| **影响** | 全网链 gas limit 从 8,000,000 塌缩 98% 至 ~162,000（交易容量 380 tx/块 → 7 tx/块）；攻击者停手后恢复速率仅 1/1024 每块，**全网交易吞吐被压垮数小时**，与攻击者是否继续出块无关 |
| **复现状态** | ✅ 已复现（双节点实测：4000 块塌缩 + 正常节点接管后的缓慢回升） |

## 2. 根本原因

`miner/worker.go:273-281` 中，出块者的区块 gas limit 由其配置决定：

```go
// miner/worker.go
gasCeil := miner.config.GasCeil          // ← --miner.gaslimit
...
header.GasLimit = core.CalcGasLimit(parent.GasLimit, gasCeil)
```

`core/block_validator.go:228-249` 的 `CalcGasLimit` 以 **1/1024 每块**的速率
向 `gasCeil` 逼近；`--miner.gaslimit 5000`（= `params.MinGasLimit`，协议允许
的最小值）通过 `cmd/utils/flags.go:1721` 直接写入，**无任何上下限校验**。

攻击链：
1. 攻击者节点（`--miner.gaslimit 5000`）作为出块者连续出块；每个区块的
   gas limit 比父块降低 1/1024 —— **完全符合共识规则**
   （`VerifyGaslimit`：`|diff| ≤ parent/1024` 且 `≥ MinGasLimit`，所有节点
   必须接受）；
2. 约 4000 块后，链 gas limit 从 8,000,000 塌缩到 ~162,000（98%）；
3. **粘性**：即使攻击者停手，任何正常出块者（gasCeil=60M）也只能以
   1/1024 每块的速度回升 —— 恢复到 8M 需再出 ~4000 块（真实网络数小时）；
4. 期间全网每个区块只能容纳 ~7 笔交易（162K/21000），全网交易处理
   系统性瘫痪 —— **与 BCFuzzer 论文 FISCO BCOS `min seal time` 案例同类的
   单节点配置 → 全网共识级影响**。

## 3. 复现步骤

运行一键复现脚本（需先 `make geth`，python3 需安装 `eth_account`/`requests`，
约 13 分钟；可用 `ROUNDS_A=200 ROUNDS_B=50` 快速验证流程）：

```bash
cd /home/geth/tse/go-ethereum
./bugs/bcb-miner-gaslimit/poc_bcb10_gaslimit_collapse.sh
```

脚本自动完成：
1. **阶段1（攻击）**：nodeA（`--miner.gaslimit 5000`，唯一出块者）由快速
   假共识客户端驱动出 4000 块，记录 gas limit 里程碑；
2. **验证**：正常节点 nodeB 同步 nodeA 的全部区块（证明攻击区块共识合法）；
3. **阶段2（粘性）**：正常节点 nodeB（默认 gasCeil=60M）接管出块 500 块，
   测量 gas limit 的缓慢回升。

## 4. 复现结果

### 阶段1：全网 gas limit 塌缩（4000 块，实测）

```
 阶段1 (攻击): nodeA (gaslimit=5000) 出 4000 块...
 nodeA: milestones={1: 7992189, 1001: 3009403, 2001: 1133771,
                    3001: 427729, 4000: 162123}
 nodeB (正常节点) 同步高度: 4000/4000 —— 攻击者的区块全部被正常节点接受 ✓
  塌缩后链 gas limit: 162,123 (攻击前 8,000,000)
  交易容量: 7 tx/块 (攻击前 380 tx/块)
```

### 阶段2：粘性 —— 正常节点接管后回升极慢（实测）

```
 阶段2 (粘性): 正常节点 nodeB (gasCeil=60M) 接管出块 500 块...
 nodeB: milestones={1: 162280, 126: 183139, 251: 206705, 376: 233332, 500: 263157}
  正常节点接管 500 块后 gas limit: 263,157 (仍塌缩 96.7%)
  恢复速率: 1/1024 每块 —— 恢复到 8M 还需约 3496 块 (真实网络数小时)
```

## 5. 影响分析

- **攻击场景**：恶意节点将 `--miner.gaslimit` 配置为 5000（完全合法，
  无任何校验）后加入网络并争取成为出块者（PoS 验证者/Clique sealer）。
  其出块任期（Clique 5s/块约 5.6 小时）内，全网 gas limit 塌缩 98%；
- **永久性**：与 BCB-3/5/6/7（仅影响攻击者自身任期）不同，**攻击者停手后
  影响仍持续** —— 全网所有出块者只能在 1/1024/块 的约束下缓慢恢复，
  交易吞吐被压垮数小时；
- **共识合法性**：攻击者的每个区块都是共识合法的（`VerifyGaslimit` 通过），
  没有任何节点可以拒绝 —— 防御方无法通过共识规则阻止；
- **交易影响**：全网交易容量 380 tx/块 → 7 tx/块，用户交易确认被系统性
  延迟，可配合拜占庭式攻击使用。

## 6. 修复建议

1. 为 `--miner.gaslimit` 增加合理的上下限校验（如不低于
   `params.MinGasLimit * 100` 或网络平均 gas limit 的某个比例）；
2. 共识层面考虑对 gas limit 的移动速率施加下限保护（如不允许低于
   `MaxGasLimit/1024` 的量级持续下调）—— 需共识升级；
3. 出块前检查：若 `GasCeil` 远低于当前链 gas limit，记录告警；
4. 参考 BCFuzzer 论文思路：对不一致配置项学习合法取值范围，越界时提示。

## 7. 复现环境

- go-ethereum v1.17.5 stable（`9621c6ad`），Go 1.24
- Linux 5.15, python3.10 + eth-account 0.13.7 / requests
- 双节点 Clique（chainId=15）本地网络，快速假共识客户端驱动出块（0.17s/块）
