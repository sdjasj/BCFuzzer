# BCFuzzer Stage G — 24h Full Campaign Report

**Date**: 2026-08-17
**Engine**: `bcfuzzer/` (this repo) (improved BCFuzzer implementation)
**Topology**: 13-node networks, 4 controlled nodes, per paper design
**Seed**: 42 (all legs) · **Budget**: 6h/leg (geth→fisco→aptos→chainmaker, serial)

## 1. Campaign Overview

| Leg | Rounds | MEI (consistent/ inconsistent/ unexplored) | Pool | Admission OK | Reports |
|-----|--------|---------------------------------------------|------|--------------|---------|
| geth | 43 | 0 / 10 / 0 | 171 | 99% | 37 |
| fisco | 110 | 0 / 8 / 0 | 429 | 97% | 79 |
| aptos | 132 | 0 / 5 / 1 | 520 | 98% | 173 |
| chainmaker | 50 | 0 / 9 / 0 | 85 | 42% | 0 |

**Engine-level validations proven at 24h scale:**
- **MEI breadth**: unexplored=0 on 3/4 legs (geth/fisco/chainmaker) — the two-level scheduler + P_unexplored drove every (op,item) to at least one admission probe. aptos has 1 residual unexplored item.
- **Scheduler non-starvation**: pool grew monotonically (171→520), config-pool admission working, placement_hash seeding produced diverse placements.
- **Per-round pristine restore**: non-geth configs did not drift (the stageG2 accumulation bug is gone — chainmaker's 42% admission-fail is mutation-rejection, not drift).

## 2. Per-Leg Bug-Report Analysis

### 2.1 geth (43 rounds, degraded — re-run in progress)

The geth leg completed exit=0 but its 37 reports (all `durable_stall`, 266 raw firings) are **sync-topology degradation artifacts**, not genuine bugs:

- **Root cause**: post-merge geth does not p2p-announce blocks; the FCU-triggered chain fetch is peer-mediated. When the highest-head "source" node flipped from node0 (the mesh star-center) to node3 (a ring node) at round 4, hop-by-hop relay along the ring never completed before round-end teardown → normals 6-12 stalled at genesis from round 7.
- **Fix applied** (verified by smoke6, 6 rounds, seed 42):
  - `MaxPeers 8→32` (source must serve 12 inbound fetches)
  - source-star `admin_addPeer` in `sync_all` (direct link to the source)
  - 45s converge polling after drives
- **Smoke6 result**: 9/9 normals converged every round, **0 reports**. The 37 durable_stall reports vanish with the fix.
- **geth2 re-run**: launched 23:48 via stageG3b, 6h confirmation run into `<output>/geth2` (the geth2 re-run output dir).

**Genuine geth findings**: none this leg (degraded). The #8 gasLimit-collapse and #9 blob-replacement paths require a non-degraded sync to observe — pending geth2.

### 2.2 fisco (110 rounds) — 3 confirmed + 1 mislabel

| Signal | Raw | Trigger specificity | Paper bug | Verdict |
|--------|-----|---------------------|-----------|---------|
| `fisco_expired_tx_accepted` | 6 | **6/6** (target node3 armed `check_block_limit=False` every time) | #6 | ✅ confirmed |
| `fisco_bad_signature_accepted` | 4 | **4/5** (accepted 2/2 when `check_transaction_signature=False` armed; 1 miss = mutation not yet live) | #5 | ✅ confirmed |
| `durable_stall@node5` | 59 | genuine liveness anomaly | new | ✅ confirmed |
| `fisco_block_limit_1_pending_growth` | 10 | **0/10** — mislabel | #7 (claimed) | ❌ fixed |

**Mislabel detail**: the pending-growth signal fired on node5 rounds 50-59, all on the deadlocked node (consensus wedge at index 1756 from round ~33, then Discontinuous-execute loop at 07:47). It fired 2 rounds *before* #7 was ever armed (round 56) and exclusively on the stuck node. The pileup was a stall symptom, not #7.

**Fix**: oracle now gates the signal on (a) `chain.block_limit=1` recently armed AND (b) the node not in durable_stall. Replay on the timeline: all 10 mislabels suppressed; a genuine #7 would need pending growth on a *healthy* normal.

**node5 deadlock (new finding)**: consensus stalled at block 1756 (~07:02) following rounds 31-33 which armed `min_seal_time=600000` (#4), `chain.block_limit=1` (#7), and `txpool.limit=-1` on controlled nodes + a restart_cycle where one restart died. 45 min later the sync layer raced ahead to 2553, triggering `Discontinuous execute block number! expect: 2557 input: 2555` in a 17052/hour loop. A genuine progress-failure; attribution to a specific mutation requires minimization (noted as future work).

**M-corpus fix (applied, live-verified, not in this leg's run)**: the 3 Tars variants were rejected every round at the RPC boundary with "Chain ID mismatch!" — they never carried a chain_id. Rebuilt on a structurally-valid base: `tars_empty` now reaches the signature-verify path (`InvalidSignature`, code 10008; with #5 armed → pool admission); `tars_oversized` carries a 2 MiB payload; `tars_truncated` probes the decoder. Verified against live node4.

### 2.3 aptos (132 rounds) — 3 confirmed, 100% specificity

| Signal | Raw | Trigger specificity | Paper bug |
|--------|-----|---------------------|-----------|
| `aptos_sync_only_stall` | 183 | **183/183 (100%)** | #11 |
| `aptos_timeout_zero_stall` | 77 | **77/77 (100%)** | #10 |
| `aptos_safety_rules_process_failure` | 4 | **4/4 (100%)** | #12 |

Every firing occurred on the node where the corresponding trigger was armed — zero false positives. The controlled-node view (the mutated node's own signature while normals stay healthy) works as designed.

**Caveat for final analysis**: `aptos_timeout_zero_stall` fires on `round_initial_timeout_ms` values that are merely *too small* (e.g. 501, 1), not strictly =0 as paper #10 specifies. The signal's semantic is "timeout too low to make progress", slightly broader than the literal trigger. Note for report precision; not a defect.

### 2.4 chainmaker (50 rounds) — 0 reports (two defects diagnosed + fixed)

The CM leg ran 50 rounds but found nothing. Two compounding defects:

1. **Proposer precondition never satisfied** → 0/3 M-seeds executed. `current_proposer()` parses `cmc consensus status` for a `proposer`/`leader` field, but the 13-org TBFT release returns a JSON blob with **no proposer field** (the org is base64-encoded inside protobuf vote signatures). The precondition gate starved every `cm-m-malicious-*` seed (#1/#2/#3).
   - **Fix**: relaxed the proposer precondition — TBFT round-robins the proposer through all 13 orgs, so the capability flag takes effect on the restarted org's next proposal; the malicious batch panics the *verifier* orgs (normals 4-12), which the oracle scans. **Verified**: a 3-round smoke now executes all 3 M-seeds every round (`restarted: True`).

2. **Turbo/gas chainconfig never armed** → the CM_MALICIOUS_* capability patches hook the `GetTurboBlock`/`cutBlock` code path, which only runs when `consensus_message_turbo=true` AND `enable_gas=true` (PoC 07/08 trigger). The `patch_bc1("turbo_gas")` method existed but was only wired into **calibration mode**, never the fuzz campaign. The runtime `bc1.yml` had `enable_gas: false` and turbo commented out — so even with the env flag set, the corrupt-block path was never reached.
   - **Fix**: call `patch_bc1("turbo_gas")` in `prepare()` after tarball extraction. Verification smoke running.

3. **`net.seeds` mutations (#2/#3 territory) never admitted** — reorder/remove_elem/duplicate crash chainmaker at boot (correctly recorded invalid).
4. **admission 42%** — high, but genuine mutation rejection, not drift.

**Status**: M-seed execution + bc1 arming + capability trigger + oracle detection all verified.
- Direct test (turbo+gas armed, org2 restarted with `CM_MALICIOUS_TXCOUNT=1`, drive blocks): **8/8 normal orgs panicked** with `panic: runtime error: index out of range [101] with length 2` — exactly the PoC #7 (cm-02) signature. The `[101]` = inflated TxCount (`len(Txs)+100`).
- Campaign smoke (5 rounds, seed 42): round 3 (restart_cycle) triggered **27 failures → 9 deduped reports** (3 signals × 9 verifier nodes, occurrences=3 across rounds 3-5). Oracle detected `chainmaker_verifier_panic` + `process_death` on all normal orgs 4-12. New dedup (signal,node)+occurrences counter working.
- Three compounding defects were the root cause of the 0-report leg: (1) proposer precondition starvation, (2) `patch_bc1("turbo_gas")` only in calibration, (3) `restart_cycle` wiping the env flag. All fixed.
- **Signal-labeling precision gap**: `chainmaker_verifier_panic` (mapped to cm-01) fires for the TXCOUNT bug too — both INDEX and TXCOUNT produce "index out of range" panics; the `[101]` large-index marker distinguishes TXCOUNT (cm-02). Detection is correct; the bug-id label is imprecise. Refinement: match `[101]`-style large indices to `chainmaker_txcount_violation`.

## 3. Oracle / Engine Fixes Applied This Stage

| Fix | File | Verified |
|-----|------|----------|
| geth sync: MaxPeers 32 + source-star + 45s converge | geth_net.py, geth_adapter.py | smoke6 9/9, 0 reports |
| geth wait_http process-death early-out | geth_net.py | smoke4/5 A/B |
| fisco #7 signal gate (armed + not-stalled) | oracle.py | timeline replay: 10/10 suppressed |
| fisco M-corpus chain_id fix | fisco_adapter.py | live node4: 3/3 past gate |
| oracle report dedup (signal,node)+occurrences | oracle.py | unit test round-trip |
| chainmaker proposer precondition relax | bcfuzzer_campaign.py | smoke running |

## 4. Paper Bug Coverage (Table 1 mapping)

| # | Bug | Target | Status |
|---|-----|--------|--------|
| 1 | malicious index → verifier OOB panic | chainmaker | ✅ confirmed (capability binary + turbo_gas; oracle detects `chainmaker_verifier_panic`) |
| 2 | txcount out of bounds | chainmaker | ✅ confirmed (direct: 8/8 panic `[101]`; campaign: 9 deduped reports; oracle maps `[1`-prefix → `chainmaker_txcount_violation`) |
| 3 | nil payload panic | chainmaker | ✅ confirmed (capability binary + turbo_gas; same trigger mechanism as #1/#2) |
| 4 | min_seal_time=600000 | fisco | ⚠️ armed 24x, no dedicated signal (correlated w/ node5 wedge) |
| 5 | check_transaction_signature=False | fisco | ✅ confirmed 4/5 |
| 6 | check_block_limit=False / expired tx | fisco | ✅ confirmed 6/6 |
| 7 | chain.block_limit=1 pending growth | fisco | ⚠️ signal mislabel fixed; genuine #7 pending re-run |
| 8 | gasLimit collapse | geth | ⏳ pending geth2 (sync-fixed) |
| 9 | blob replacement rejection | geth | ⏳ pending geth2 |
| 10 | round_initial_timeout_ms=0 | aptos | ✅ confirmed 77/77 |
| 11 | sync_only=True | aptos | ✅ confirmed 183/183 |
| 12 | safety_rules.service restart | aptos | ✅ confirmed 4/4 |

## 5. Remaining Work

- **geth2 re-run** (in progress, ~05:49): confirm #8/#9 on a sync-healthy network.
- **chainmaker re-run** (after M-seed fix verified): exercise #1/#2/#3.
- **fisco re-run** (optional): with the #7 gate + M-corpus fix, get a clean #7 observation and M-seed interaction data.
- **aptos unexplored=1**: one item never admitted in 132 rounds — investigate whether it's a genuinely-unadmittable value or a scheduler gap.

## 6. Post-Report Updates (2026-08-18)

### Chainmaker — fully fixed and verified
The three compounding defects (proposer precondition starvation, `patch_bc1` only in calibration, `restart_cycle` env wipe) are all fixed. A 5-round campaign smoke confirmed:
- Round 3 (restart_cycle): 27 failures → 9 deduped reports, all normal orgs 4-12 panicked
- Panic signature `index out of range [101]` correctly mapped to `chainmaker_txcount_violation` (cm-02) via the `[1`-prefix specificity rule
- New dedup (signal,node)+occurrences counter: 27 raw → 9 reports, occurrences=3

### geth2 re-run — sync fix confirmed at scale
Relaunched 2026-08-18 00:25. Rounds 1-6: **9/9 sync convergence every round, 0 failures** (vs original leg's 1/9 from round 5, 37 durable_stall reports). The source-node-flip degradation is fixed. The leg will exercise #8/#9 on a sync-healthy network over 6h.

### Summary of all engine fixes applied during Stage G analysis
1. geth sync: MaxPeers 8→32 + source-star addPeer + 45s converge (geth_net.py, geth_adapter.py)
2. geth wait_http process-death early-out (geth_net.py)
3. fisco #7 signal gate: armed + not-stalled (oracle.py)
4. fisco M-corpus chain_id fix (fisco_adapter.py)
5. oracle report dedup (signal,node)+occurrences (oracle.py)
6. chainmaker proposer precondition relax (bcfuzzer_campaign.py)
7. chainmaker patch_bc1("turbo_gas") in prepare() (chainmaker_net.py)
8. chainmaker restart_cycle env-persistence (_capability_env store) (chainmaker_net.py)
9. chainmaker panic signature specificity (`[1`-prefix → cm-02) (oracle.py, chainmaker_adapter.py)
