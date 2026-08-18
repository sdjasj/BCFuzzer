# Minimized Test Cases for the 12 BCBs (Table 1)

This directory contains the one-click reproduction scripts for the 12
Blockchain Configuration Bugs (BCBs) reported in Table 1 of the paper.
Each subdirectory holds a `poc.sh` (or `poc_reproduce.sh`) that builds the
isolated target network, applies the triggering configuration, drives the
interaction that exposes the bug, and prints `[POC PASS]` (or
`[POC VERSION-GUARDED]` for findings whose race no longer reproduces on the
pinned target version) on success.

## Table 1 ↔ test case mapping

| # | Test Object | Bug Type | Config trigger | Test case |
|---|-------------|----------|-----------------|-----------|
| 1 | ChainMaker | Peer Failure | `pool_type=batch` + turbo block TxCount | `chainmaker/09_batch_turbo_cutblock_crash/` |
| 2 | ChainMaker | Peer Failure | `net.seeds` updates race peer-info map | `chainmaker/11_net_seeds_peer_map_race/` |
| 3 | ChainMaker | Peer Failure | cert reconfig races logger level-map | `chainmaker/12_cert_reconfig_logger_race/` |
| 4 | FISCO BCOS | Progress Failure | `consensus.min_seal_time=60s` | `fisco/01_min_seal_time/` |
| 5 | FISCO BCOS | Progress Failure | `check_transaction_signature=false` | `fisco/02_check_transaction_signature/` |
| 6 | FISCO BCOS | Progress Failure | `txpool.check_block_limit=false` | `fisco/03_check_block_limit/` |
| 7 | FISCO BCOS | Progress Failure | `chain.block_limit=1` | `fisco/04_chain_block_limit/` |
| 8 | Go-Ethereum | Progress Failure | `miner.gaslimit=5000` | `geth/01_miner_gaslimit_collapse/` |
| 9 | Go-Ethereum | Transaction Failure | `blobpool.pricebump=1000000` | `geth/02_blobpool_pricebump/` |
| 10 | Aptos | Progress Failure | `consensus.round_initial_timeout_ms=0` | `aptos/01_round_initial_timeout_zero/` |
| 11 | Aptos | Progress Failure | `consensus.sync_only=true` | `aptos/02_sync_only_true/` |
| 12 | Aptos | Progress Failure | `consensus.safety_rules.service=process` | `aptos/03_safety_rules_dead_process/` |

## Reproduction environment

The scripts assume the four blockchain source trees are checked out as
siblings under a common workspace (default `/home/geth/tse`); see
`docs/ENVIRONMENT.md` for the pinned commits and `setup.sh` for wiring
non-standard layouts.

| Platform | Version | Commit |
|----------|---------|--------|
| ChainMaker | v3.0.0 | `2b8f85a` |
| FISCO-BCOS | 3.16.4 | `fb90450` |
| go-ethereum | 1.17.5 | (Osaka + fake beacon client) |
| aptos-core | — | `7f99ad42` |

## Running

```
cd test_cases/chainmaker/09_batch_turbo_cutblock_crash
bash poc.sh
```

Each script prints `[POC PASS]` (or `[POC 复现成功]` / `结果: PASS` for the
original-corpus scripts) on success and exits 0.

## Version-dependence note (BCB #2 and #3)

ChainMaker BCB #2 (`net.seeds` peer-map race) and #3 (cert reconfig + logger
level-map race) were originally reported via the ChainMaker issue tracker
(paper references [29], [30]).  The pinned ChainMaker v3.0.0 release ships
net-libp2p v1.3.1, which guards the peer-information maps with `RWMutex` and
may no longer reproduce the concurrent-map-write panic.  The two PoC scripts
faithfully execute the BCFuzzer discovery path (the `net.seeds` /
certificate-reconfiguration mutations overlapped with `restart_cycle` and
`concurrent_workload` sequences — see `bcfuzzer/sequences.py`); if the
current version does not panic, the script prints
`[POC VERSION-GUARDED]` and points to the original issue evidence, preserving
the minimized test case as a historical record of the finding.
