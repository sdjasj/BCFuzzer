# Environment — pinned platforms and isolated network construction

BCFuzzer runs against four live blockchain networks, each built from a
pinned source commit. The platform adapters (`live_node_*.py`) and the
PoC scripts assume the four source trees are checked out as siblings under
a common workspace root (default `/home/geth/tse`; override with
`BCFZ_WORKSPACE`). Run `setup.sh` to verify the layout.

## Pinned versions

| Platform | Version | Commit | Topology |
|----------|---------|--------|----------|
| ChainMaker | v3.0.0 | `2b8f85a` | 13-org TBFT |
| FISCO-BCOS | 3.16.4 | `fb90450` | 13-node PBFT (air) |
| go-ethereum | 1.17.5 | (Osaka) | 13 execution nodes + fake beacon |
| aptos-core | — | `7f99ad42` | 13-validator local swarm |

## Toolchains

| Tool | Version | Used for |
|------|---------|----------|
| Go | 1.24 + goc (coverage) | ChainMaker binary (instrumented), aptos-node |
| Rust | 1.94.1 + llvm-profdata/llvm-cov | aptos coverage |
| gcc | 14 (RelWithDebInfo + coverage) | FISCO-BCOS coverage build |
| Python | 3.10+ | engine + adapters |

The `goc` coverage server listens on `127.0.0.1:17771`; the engine starts
it once per process (`goc_utils.ensure_goc_binary`). For Aptos,
`llvm_profile_flush.c` is an `LD_PRELOAD` helper that calls
`__llvm_profile_write_file` so signal-killed validators still dump
profiles.

## Constructing the isolated 13-node networks

### Go-Ethereum (13 execution nodes + fake beacon)

1. Build go-ethereum 1.17.5 from the `go-ethereum/` source tree (the
   engine's `ensure_instrumented_binary` produces a goc-instrumented
   binary).
2. The fake beacon client (`test_cases/geth/01_*/fake_beacon_client.py`
   or `02_*/fake_beacon_client.py`) drives the Engine API (FCU →
   getPayload → newPayload) to advance the chain; post-merge blocks are
   not p2p-announced, so normals fetch from peers.
3. The engine's `GethNetwork` generates keys, inits nodes, meshes them
   (star-around-node0 + ring), and syncs normals to the producer via
   `admin_addPeer` to the highest-head source.

### FISCO-BCOS (13-node PBFT, air mode)

1. Build FISCO-BCOS 3.16.4 from `FISCO-BCOS/` with gcc-14 coverage flags.
2. `build_chain.sh -p 30300,20200 -l 127.0.0.1:13` generates node0..12
   (p2p 30300+i, RPC 20200+i).
3. The engine's `FiscoNetwork` starts all 13 nodes, waits for
   `reachNewView` on each (180s budget), and probes via
   `getPbftView`/`getPendingTxSize`/`current_block_number`.

### ChainMaker (13-org TBFT)

1. From `chainmaker-go/scripts/`, run `prepare.sh 13 1 11301 12301 32351
   22351 23351 -c 1 -l INFO -v false -j false --vlog=INFO --jlog=INFO`
   then `build_release.sh` — natively supports 13 orgs.
2. The engine's `ChainMakerNetwork.prepare()` extracts the 13 tarballs,
   installs the capability-instrumented binary (env-gated
   `CM_MALICIOUS_*` source patches), patches `bc1.yml`
   (`turbo_gas` = consensus-message-turbo + enable_gas), and starts all
   13 orgs.
3. Contracts (`fact`/`counter` WASM) are deployed once via `cmc`.

### Aptos (13-validator local swarm)

1. Build aptos-core from `aptos-core/` (Rust + `forge`).
2. The engine's `AptosNetwork.launch()` runs `forge --suite run_forever
   --num-validators 13 test local-swarm`, waits until every validator's
   API reports `ledger_version > 0`, detaches Forge, and restarts any
   validator that did not survive detachment via
   `aptos-node -f node.yaml`.

## Per-round teardown and restore

Each round builds a fresh 13-node network, assigns 4 controlled nodes
(exploration + fuzzing roles), mutates their configs, runs admission
probes, executes T/M seeds and sequences, observes via the BCB Oracle,
then tears down. Non-geth targets restore pristine configs each round
(snapshot/rollback in `mutator.py`) so mutations do not accumulate.
