# BCFuzzer

BCFuzzer is a framework for detecting **Blockchain Configuration Bugs
(BCBs)** — inter-node or network-wide failures caused by node-local
configuration interactions with transactions or inter-node protocol
messages. It combines a mutation-rule model for divergent configuration
items, a two-level multi-node configuration scheduler, transaction and
inter-node-message interaction corpora, and two runtime oracles that
capture security and availability issues. BCFuzzer discovered **12
previously unknown BCBs** across four widely-used blockchain platforms:
Go-Ethereum, FISCO BCOS, ChainMaker, and Aptos.

## Repository structure

```
BCFuzzer/
├── bcfuzzer/                  # the fuzzing engine (design §3)
│   ├── common.py              # ItemSpec / MutationOp / Seed / BugReport data model
│   ├── item_catalog.py        # declarative config-item inventory (4 targets, 12 bug triggers)
│   ├── mutator.py             # type-aware mutation rules (snapshot/rollback, dangerous_legal exemption)
│   ├── mei.py                 # Mutation-Effective Index (consistent/inconsistent/unexplored)
│   ├── scheduler.py           # two-level scheduler (exploration + fuzzing roles, P_unexplored, placement hash)
│   ├── corpus_t.py            # transaction-corpus seeds (T)
│   ├── corpus_m.py           # inter-node-message seeds (M, incl. ChainMaker capability flags)
│   ├── sequences.py           # drive_blocks / rotate_role / restart_cycle / concurrent_workload / submit_pair
│   ├── oracle.py              # BCB Oracle (peer/progress/transaction failure, durable windows)
│   ├── calibration.py        # calibrate mode — prove oracle fires on the 12 bug set
│   ├── regression.py          # regress mode — re-run minimized PoC test cases
│   └── targets/               # per-target network factories + adapters
│       ├── geth_net.py geth_adapter.py
│       ├── fisco_net.py fisco_adapter.py
│       ├── chainmaker_net.py chainmaker_adapter.py
│       └── aptos_net.py aptos_adapter.py
├── bcfuzzer_campaign.py       # main driver: --mode {fuzz, calibrate, regress}
├── full_bcfuzzer.py           # BUG_SPECS registry + run_bug (PoC test-case harness)
├── config_mutators.py         # 4 baseline strategies (ECFuzz / ConfTest / ConfErr / ConfDiagDetector)
├── goc_utils.py               # Go coverage tooling (goc server, profile merge)
├── live_node_*.py             # platform live-node adapters (imported, not modified)
├── targets.py live_profiles.py adapter_cli.py seeded_tests/  # shared adapter layer
├── llvm_profile_flush.c       # LD_PRELOAD helper for aptos coverage
├── test_cases/                # minimized test cases for all 12 BCBs (Table 1)
├── tests/                     # unit tests (mutator, MEI/scheduler, campaign)
├── docs/                      # ENVIRONMENT.md, ARTIFACT.md, notes/
├── setup.sh                   # wire the workspace contract (symlinks)
├── run_campaign.sh            # drive a 4-target fuzz campaign
└── requirements.txt
```

## Requirements

```
pip install -r requirements.txt
```

Python 3.10+. The platform source trees (Go-Ethereum, ChainMaker,
FISCO-BCOS, Aptos) and their toolchains are external — see
`docs/ENVIRONMENT.md` for pinned commits and build steps.

## Workspace contract

The platform adapters (`live_node_*.py`) and PoC scripts assume the four
blockchain source trees and the bug corpus are siblings under a common
workspace root (default `/home/geth/tse`). Run `setup.sh` to verify the
layout and bind the shipped `test_cases/` to the expected path:

```
./setup.sh                       # default workspace /home/geth/tse
./setup.sh /path/to/workspace    # custom workspace root
```

`setup.sh` symlinks `test_cases/` → `$WORKSPACE/inter-node-bugs-final` so
the regression harness resolves without an external corpus checkout.

## Quick start

### 1. Unit tests (no networks needed)

```
python3 tests/test_bcfuzzer_mutator.py
python3 tests/test_bcfuzzer_mei_scheduler.py
```

### 2. Minimized PoC test cases (Table 1)

List the 12 BCBs and their PoC scripts:

```
python3 full_bcfuzzer.py --list
```

Reproduce a single BCB (requires the target source tree):

```
cd test_cases/fisco/01_min_seal_time
bash poc_reproduce.sh
```

Or run the regression harness over a target's bug set:

```
python3 bcfuzzer_campaign.py --target fisco --mode regress --bugs fs-04,fs-05,fs-06,fs-07 --output /tmp/regress-fisco
```

### 3. Calibration (prove the oracle fires on the bug set)

```
python3 bcfuzzer_campaign.py --target aptos --mode calibrate --bugs ap-10,ap-11,ap-12 --output /tmp/calib-aptos
```

### 4. Fuzz campaign (24-hour, 13-node networks)

A single leg (6 hours, seed 42):

```
python3 bcfuzzer_campaign.py --target geth --output /tmp/bcfz-geth \
    --nodes 13 --controlled 4 --budget-minutes 360 --seed 42
```

All four targets serially:

```
./run_campaign.sh
```

## Table 1 — the 12 BCBs

| # | Target | Type | Config trigger | Test case |
|---|--------|------|----------------|-----------|
| 1 | ChainMaker | Peer Failure | `pool_type=batch` + turbo TxCount | `test_cases/chainmaker/09_batch_turbo_cutblock_crash/` |
| 2 | ChainMaker | Peer Failure | `net.seeds` race peer-info map | `test_cases/chainmaker/11_net_seeds_peer_map_race/` |
| 3 | ChainMaker | Peer Failure | cert reconfig + logger race | `test_cases/chainmaker/12_cert_reconfig_logger_race/` |
| 4 | FISCO BCOS | Progress Failure | `consensus.min_seal_time=60s` | `test_cases/fisco/01_min_seal_time/` |
| 5 | FISCO BCOS | Progress Failure | `check_transaction_signature=false` | `test_cases/fisco/02_check_transaction_signature/` |
| 6 | FISCO BCOS | Progress Failure | `txpool.check_block_limit=false` | `test_cases/fisco/03_check_block_limit/` |
| 7 | FISCO BCOS | Progress Failure | `chain.block_limit=1` | `test_cases/fisco/04_chain_block_limit/` |
| 8 | Go-Ethereum | Progress Failure | `miner.gaslimit=5000` | `test_cases/geth/01_miner_gaslimit_collapse/` |
| 9 | Go-Ethereum | Transaction Failure | `blobpool.pricebump=1000000` | `test_cases/geth/02_blobpool_pricebump/` |
| 10 | Aptos | Progress Failure | `round_initial_timeout_ms=0` | `test_cases/aptos/01_round_initial_timeout_zero/` |
| 11 | Aptos | Progress Failure | `sync_only=true` | `test_cases/aptos/02_sync_only_true/` |
| 12 | Aptos | Progress Failure | `safety_rules.service=process` | `test_cases/aptos/03_safety_rules_dead_process/` |

See `test_cases/README.md` for the full mapping and reproduction notes.
BCB #2 and #3 may not reproduce on the pinned ChainMaker v3.0.0 release
(net-libp2p RWMutex hardening); their PoC scripts print
`[POC VERSION-GUARDED]` and point to the original issue evidence.

## Engine modules

- **MEI** (`mei.py`): Mutation-Effective Index — tracks valid/invalid/
  unexplored admission outcomes per (op, item, value); admission is the
  sole feedback source.
- **Two-level scheduler** (`scheduler.py`): assigns exploration / fuzzing /
  normal roles; maintains a config pool; picks the least-tested seed per
  placement hash with `P_unexplored = 1/(|V|+1)`.
- **T/M corpora** (`corpus_t.py`, `corpus_m.py`): transaction seeds
  (normal-node RPC) and inter-node-message seeds (controlled-node direct
  delivery, incl. ChainMaker capability flags).
- **BCB Oracle** (`oracle.py`): peer failure (process death + panic
  signatures), progress failure (durable stall + view-change storm),
  transaction failure (receipt/fork + replacement-rejection); durable
  windows and capacity metrics.
- **Sequences** (`sequences.py`): `drive_blocks`, `rotate_role`,
  `restart_cycle`, `concurrent_workload`, `submit_pair`.
- **Calibration / regression** (`calibration.py`, `regression.py`):
  replay the 12 bug set through fuzzer primitives / PoC scripts.

## Anonymization

This artifact is anonymized for double-blind review. Author identity has
been removed from the git history and source files. Issue-tracker
references in the bug reports point to public ChainMaker issues and are
not author self-identification.
