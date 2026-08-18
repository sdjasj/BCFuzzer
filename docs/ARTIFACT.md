# Artifact evaluation guide

This guide walks through validating the BCFuzzer artifact: the engine,
the 12 minimized BCB test cases, and the calibration evidence.

## 0. Prerequisites

- Python 3.10+, `pip install -r requirements.txt`
- The four blockchain source trees checked out per `docs/ENVIRONMENT.md`
- Toolchains: Go 1.24 + goc, Rust 1.94 + llvm-cov, gcc-14 (for full
  live reproduction; unit tests and calibrate mode need only Python)

Run `./setup.sh` to verify the workspace layout and bind `test_cases/`.

## 1. Unit tests (no networks)

```
python3 tests/test_bcfuzzer_mutator.py       # mutation rules, rollback, exemption
python3 tests/test_bcfuzzer_mei_scheduler.py  # MEI classification + two-level scheduling
```

Both should print `all ... tests passed`.

## 2. Engine import sanity

```
python3 -c "from bcfuzzer.oracle import BcbOracle; from bcfuzzer.scheduler import TwoLevelScheduler; print('OK')"
python3 full_bcfuzzer.py --list
```

The second command lists the 20 BUG_SPECS (12 Table-1 bugs + extra
ChainMaker variants).

## 3. Minimized PoC test cases (Table 1)

Pick a bug whose target source tree is built, e.g. FISCO BCB #4:

```
cd test_cases/fisco/01_min_seal_time
bash poc_reproduce.sh
```

Expected: `[POC 复现成功]` (or `结果: PASS`) printed, exit 0.

The two ChainMaker race bugs (#2, #3) may print
`[POC VERSION-GUARDED]` if the pinned ChainMaker v3.0.0 release no
longer triggers the race (net-libp2p RWMutex hardening). This is the
documented version-dependence; the discovery path is still recorded.

To run a target's full regression set:

```
python3 bcfuzzer_campaign.py --target fisco --mode regress \
    --bugs fs-04,fs-05,fs-06,fs-07 --output /tmp/regress-fisco
```

## 4. Calibration (oracle fires on the bug set)

```
python3 bcfuzzer_campaign.py --target aptos --mode calibrate \
    --bugs ap-10,ap-11,ap-12 --output /tmp/calib-aptos
```

Each `calibration/<bug>.json` records the expected vs fired oracle
signals and the raw observations (before/after ledger heights, victim
nodes, panic text).

## 5. Fuzz campaign (24-hour, all four targets)

A single 6-hour leg:

```
python3 bcfuzzer_campaign.py --target geth --output /tmp/bcfz-geth \
    --nodes 13 --controlled 4 --budget-minutes 360 --seed 42
```

All four targets serially (legs must not overlap — the network factories
boot unreliably under concurrent 13-node loads):

```
./run_campaign.sh
```

Each leg writes `result.json` (deduped BugReports, MEI summary, pool
size), `timeline.jsonl` (per-round placement, verdicts, mutations, seed
results, sequences, failures), and `state/{mei,scheduler,oracle}.json`.

## 6. Expected outcomes

- **Unit tests**: all pass.
- **PoC test cases**: 10 of 12 reproduce on the pinned versions; BCB #2
  and #3 may report `[POC VERSION-GUARDED]` (documented).
- **Calibration**: the oracle fires the expected signal for each bug
  (per `calibration.py`'s `oracle_signals`).
- **Fuzz campaign**: MEI unexplored→0, scheduler pool grows, the BCB
  Oracle reports the Table-1 manifestations on the targets where the
  trigger path is exercised within the budget.

See `docs/notes/stage-g-campaign-analysis.md` for the 24-hour campaign
analysis (per-leg findings, defect/fix log).
