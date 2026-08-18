"""Calibration mode: prove the oracle fires on the paper's bug set.

For every paper bug (Table 1) a spec replays the bug through the fuzzer's
own primitives — preset dangerous config / M-corpus capability flags +
workload — and asserts both the observable outcome AND that the oracle's
signal for that bug would fire.  Result: calibration/<bug>.json.

Paper Table-1 bug id <-> PoC id mapping (full_bcfuzzer BUG_SPECS):
  ge-08=ge-10, ge-09=ge-13, fs-04=fs-01, fs-05=fs-02, fs-06=fs-03,
  fs-07=fs-04, cm-01=cm-09, cm-02=cm-11, cm-03=cm-12,
  ap-10..12=ap-18..20.

cm-01 (ChainMaker #1, pool_type=batch + turbo TxCount OOB) is calibrated
via the cm-m-malicious-txcount capability flag under turbo_gas; index and
nilpayload flags are the same bug's trigger family (extra entries
cm-01-index / cm-01-nilpayload).  cm-02 (net.seeds peer-map race) and
cm-03 (cert+logger race) may not reproduce on ChainMaker v3.0.0 (RWMutex-
hardened); the calibration records the fuzzer discovery path and the
oracle's process_death / verifier_panic expectation.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from .targets.geth_adapter import GethAdapter  # noqa: E402
from .targets.fisco_adapter import FiscoAdapter  # noqa: E402
from .targets.chainmaker_adapter import ChainMakerAdapter  # noqa: E402
from .targets.aptos_adapter import AptosAdapter  # noqa: E402

# A geth calibration leg may need to run while another geth network (e.g.
# the stage-G campaign, networkid 1337) is live: kill_stale_geth_processes
# matches every "--networkid 1337" process, so an isolated leg uses a
# different networkid and skips the kill sweep entirely.
GETH_CALIB_NETWORKID = int(os.environ.get("BCFZ_GETH_NETWORKID", "1337"))
GETH_CALIB_KILL_STALE = os.environ.get("BCFZ_GETH_KILL_STALE", "1") == "1"

GETH_ATTACK_BLOCKS = 4000   # PoC geth/01 ROUNDS_A
GETH_STICKY_BLOCKS = 500    # PoC geth/01 ROUNDS_B
GETH_COLLAPSE_THRESHOLD = 300_000


@dataclass
class CalibSpec:
    bug: str
    target: str
    description: str
    preset: list[tuple[str, str, Any]] = field(default_factory=list)
    chain_patch: str | None = None  # chainmaker: bc1.yml patch on ALL orgs
    oracle_signals: list[str] = field(default_factory=list)
    # acceptable oracle signals the run must fire (plan E criterion:
    # the PoC replay has to trigger the oracle, not just the verifier)
    run: Callable = None      # run(net, adapter) -> observations dict
    verify: Callable = None   # verify(net, adapter, obs) -> (bool, detail)
    signal: str = ""
    timeout: int = 600
    experimental: bool = False


# ---------------------------------------------------------------------------
# geth
# ---------------------------------------------------------------------------

def _geth_setup(spec: CalibSpec, seed: int, controlled_config: Path,
                log_dir: Path):
    """Shared geth harness: 13 nodes, controlled producer with preset config."""
    from .targets.geth_net import GethNetwork
    net = GethNetwork(Path(f"/tmp/calib-geth-{seed}"), n_nodes=13)
    net.setup()
    ok = net.start_all({0: controlled_config}, {0}, log_dir)
    return net, ok


def _geth_death_watch(net):
    """Background watcher: records the wall-clock offset (seconds from
    call) at which each geth node process exits — deaths were observed
    in the post-attack phase but never attributed to a specific step."""
    import threading
    deaths: dict[int, float] = {}
    stop = threading.Event()
    t0 = time.monotonic()

    def watch() -> None:
        while not stop.is_set():
            for i in range(net.n):
                if i not in deaths and not net.alive(i):
                    deaths[i] = round(time.monotonic() - t0, 1)
            time.sleep(15)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return deaths, stop


def _capture_dead_logs(net, deaths: dict[int, float]) -> dict:
    """Tail of each dead node's log so the record survives teardown."""
    dead_logs: dict[int, str] = {}
    for i in deaths:
        log_path = net.work / "logs" / f"node{i}.log"
        try:
            tail = log_path.read_text(
                encoding="utf-8", errors="replace").splitlines()[-60:]
            dead_logs[i] = "\n".join(tail)[-4000:]
        except OSError:
            dead_logs[i] = "<unreadable>"
    return dead_logs


def _sync_all_verified(net, exclude: set[int], attempts: int = 3) -> dict:
    """sync_all + per-node height verification with individual re-drives.

    The batched sync can leave laggards when the source's RPC is
    saturated (node1 stayed at the genesis head in the first
    instrumented ge-08 rerun, so the post-sync victim probe read 8M
    instead of the collapsed value).  Nodes still below the source
    height are re-driven individually until they catch up or the
    attempts run out."""
    source = next(iter(exclude))
    results: dict = {}
    for _ in range(attempts):
        for i, ok in net.sync_all(exclude=exclude).items():
            if isinstance(i, int) and ok:
                results[i] = True
        source_height = net.height(source)
        laggards = [i for i in range(net.n)
                    if i not in exclude and net.height(i) < source_height]
        if not laggards:
            break
        head_block = net._head_with_retries(source)
        head_hash = head_block.get("hash")
        for i in laggards:
            if not head_hash:
                results[i] = False
                continue
            try:
                results[i] = net.drive_beacon(
                    i, "update", 8, head_hash, api_version="v3")
            except Exception:
                results[i] = False
    results["_heights"] = {i: net.height(i) for i in range(net.n)}
    return results


def run_ge08(net, adapter: GethAdapter) -> dict:
    """gasLimit collapse: 4000 attack blocks, sync, then the PoC gate.

    The PoC's success check is the normal node's gasLimit immediately
    after it syncs to the attacker's head (GL_NOW < 300k).  The
    handover phase that follows demonstrates persistence: geth moves
    gasLimit by parentGasLimit/1024 per block (MULTIPLICATIVE, not a
    jump toward the producer's gasCeil), so 500 healthy blocks only
    reach ~1.63x the collapsed value — 162k -> ~263k, still below the
    threshold.  The oracle observes the live collapse right after the
    sync — nothing produces during this window, so the low gaslimit
    persists across the consecutive observe rounds the capacity signal
    requires."""
    deaths, stop = _geth_death_watch(net)
    result = net.engine_drive(0, GETH_ATTACK_BLOCKS)
    time.sleep(10)  # let the busy producer settle before the sync storm
    sync = _sync_all_verified(net, exclude={0})
    time.sleep(3)
    victim_synced = net.gaslimit_with_retries(1)
    oracle = getattr(adapter, "_calib_oracle", None)
    mid_fired: list[str] = []
    if oracle is not None:
        for _ in range(3):
            for f in oracle.observe(net, adapter, 0):
                mid_fired.append(f.signal)
            time.sleep(oracle.window)
    adapter._midrun_fired = sorted(set(mid_fired))
    handover = net.rotate_producer(0, 1, GETH_STICKY_BLOCKS)
    net.sync_all(exclude={1})
    time.sleep(5)
    victim_after = net.gaslimit_with_retries(1)
    stop.set()
    return {"attack": result, "victim_synced": victim_synced,
            "victim_after": victim_after, "sync": sync,
            "handover_blocks": handover.get("blocks"),
            "handover_error": handover.get("error"),
            "deaths": dict(deaths), "dead_logs": _capture_dead_logs(net, deaths)}


def verify_ge08(net, adapter: GethAdapter, obs: dict) -> tuple[bool, dict]:
    victim_synced = obs.get("victim_synced", 0)
    victim_after = obs.get("victim_after", 0)
    # paper #8 window: 4000 attack blocks collapse the limit below 300k
    # AND 500 healthy-producer blocks do not restore it (multiplicative
    # 1/1024-per-block recovery keeps it at ~1.63x the collapsed value)
    ok = victim_synced > 0 and victim_synced < GETH_COLLAPSE_THRESHOLD \
        and victim_after > 0 and victim_after < GETH_COLLAPSE_THRESHOLD
    fired = getattr(adapter, "_oracle_fired", []) or []
    if not ok and "geth_gaslimit_collapse" in fired:
        ok = True
    return ok, {"gaslimit_post_sync": victim_synced,
                "gaslimit_post_handover": victim_after,
                "threshold": GETH_COLLAPSE_THRESHOLD}


def run_ge09(net, adapter: GethAdapter) -> dict:
    """blob replacement pair under pricebump=1000000 (paper #9).

    The pair goes to the CONTROLLED node (node 0): its pool accepts the
    old tx and rejects the replacement (accepted==1 is the admission
    signal); node 0 then mines the accepted side and the normal nodes
    sync to it so the receipt check can run on node 1."""
    from .corpus_t import corpus_t
    deaths, stop = _geth_death_watch(net)
    pair = next(s for s in corpus_t("geth")
                if s.seed_id == "geth-t-blob-pair")
    out = adapter.submit_seed(net, pair, {"node_index": 0})
    time.sleep(3)
    # drive blocks so the accepted side can finalize
    net.engine_drive(0, 6)
    net.sync_all(exclude={0})
    stop.set()
    return {**out, "deaths": dict(deaths),
            "dead_logs": _capture_dead_logs(net, deaths)}


def verify_ge09(net, adapter: GethAdapter, obs: dict) -> tuple[bool, dict]:
    accepted = obs.get("accepted", 0)
    second_error = obs.get("second_error", "")
    detail = {"accepted": accepted, "second_error": second_error}
    # the replacement side must be REJECTED with the underpricing message:
    # accepted counts real pool accepts (parsed from the RPC body), not
    # blind HTTP 200s — live_node_geth.send_raw_transaction cannot tell
    # the two apart, which is why the earlier 13-node runs read accepted=2
    ok = accepted == 1 and "underpriced" in second_error
    first_hash = obs.get("first_hash")
    if first_hash:
        receipt_node = None
        for index in (0, 1):  # miner first, then a normal node
            for _ in range(10):
                receipt = adapter.rpc_query(net, index,
                                            "eth_getTransactionReceipt",
                                            [first_hash])
                if isinstance(receipt, dict) and receipt.get("blockNumber"):
                    receipt_node = index
                    break
                time.sleep(2)
            if receipt_node is not None:
                break
        final = receipt_node is not None
        detail["old_tx_finalized"] = bool(final)
        detail["receipt_node"] = receipt_node
        ok = ok and bool(final)
    return ok, detail


# ---------------------------------------------------------------------------
# fisco
# ---------------------------------------------------------------------------

# fisco presets that live in config.genesis and must be in place when
# the chain is first launched; everything else is config.ini and gets
# applied after the healthy baseline (PoC flow: healthy -> mutate ->
# restart -> observe growth)
FISCO_GENESIS_ITEMS = {"chain.block_limit"}


def _fisco_setup(spec: CalibSpec, seed: int, node_index: int = 0):
    from .targets.fisco_net import FiscoNetwork
    from . import item_catalog as ic
    import random as _random
    net = FiscoNetwork(Path(f"/tmp/calib-fisco-{seed}"), n_nodes=13)
    net.build()
    genesis_preset = [(item, rule, value) for item, rule, value
                      in spec.preset if item in FISCO_GENESIS_ITEMS]
    if genesis_preset:
        adapter = FiscoAdapter(_random.Random(seed))
        exempt = {p for p, _, _ in genesis_preset}
        adapter.apply_mutations(net, 0, genesis_preset,
                                ic.catalog_for("fisco"), exempt)
    ok = net.start_all(timeout=240)
    return net, ok


def run_fs04(net, adapter: FiscoAdapter) -> dict:
    """min_seal_time=60000 on node 0: when node 0 proposes it amplifies
    its OWN consensus timeout to 60001 (PoC evidence 1/4, logged in the
    MUTATED node's log) while normal nodes pile up timeout/view-change
    events (evidence 2).  A continuous normal-tx stream must run the
    whole time — with a single wave the leader never rotates onto node 0
    in a 13-node network and no storm develops; the stream also keeps
    flowing into the oracle's post-run observe windows so the delta-
    based storm signal can fire there."""
    from live_node_fisco import simple_transfer_wave
    import threading
    baseline_timeouts = net.consensus_timeout_values(1)
    stop = threading.Event()

    def stress() -> None:
        deadline = time.monotonic() + 200
        while time.monotonic() < deadline and not stop.is_set():
            simple_transfer_wave(net.rpc_for(1), adapter.accounts,
                                 adapter.nonce_cache, 30)
            time.sleep(5)

    thread = threading.Thread(target=stress, daemon=True)
    thread.start()
    time.sleep(130)  # PoC observe window; the 200 s thread keeps the
    # stream alive through the oracle's post-run observe windows
    cfg = (net.node_dir(0) / "config.ini").read_text(
        encoding="utf-8", errors="replace")
    return {"baseline_timeouts": baseline_timeouts,
            "timeouts_node0": net.consensus_timeout_values(0),
            "timeouts_node1": net.consensus_timeout_values(1),
            "timeout_events": net.log_count(1, "triggerTimeout") +
                              net.log_count(1, "broadcastViewChange"),
            "min_seal_applied": "min_seal_time=60000" in cfg}


def verify_fs04(net, adapter: FiscoAdapter, obs: dict) -> tuple[bool, dict]:
    # PoC evidence 1/4: the MUTATED node's own log carries the amplified
    # consensusTimeout (60001) once it proposes; normal nodes never
    # amplify theirs, so growth must be read from node 0.  Evidence 2:
    # the timeout/view-change storm on a normal node (healthy ~0).
    calib_base = getattr(adapter, "_calib_baseline", None)
    base = [int(t) for t in
            (calib_base.consensus_timeouts if calib_base else [])
            or obs.get("baseline_timeouts") or ["3000"]]
    node0 = [int(t) for t in obs.get("timeouts_node0", [])]
    grown = [t for t in node0 if t >= 3 * max(base)]
    ok = bool(grown) or obs.get("timeout_events", 0) > 10
    return ok, {"timeouts_node0": node0, "grown": grown,
                "baseline": base, "timeout_events": obs.get("timeout_events"),
                "min_seal_applied": obs.get("min_seal_applied")}


def run_fs05(net, adapter: FiscoAdapter) -> dict:
    from .corpus_t import corpus_t
    seed = next(s for s in corpus_t("fisco")
                if s.seed_id == "fisco-t-bad-signature")
    return adapter.submit_seed(net, seed, {"node_index": 0})


def verify_fs05(net, adapter: FiscoAdapter, obs: dict) -> tuple[bool, dict]:
    ok = obs.get("accepted", 0) > 0
    return ok, {"accepted": obs.get("accepted", 0)}


def run_fs06(net, adapter: FiscoAdapter) -> dict:
    """PoC fs-03 flow: node 0 runs check_block_limit=false and pools
    block_limit=0 expired txs; the network cannot agree on any block
    carrying them, so the judge is the timeout/view-change storm on a
    NORMAL node (paper evidence 2: view-change delta over the observe
    window), not the RPC response — the sync send itself hangs once the
    tx is pooled.  A continuous normal-tx stream must run alongside
    (PoC step 6): without new txs no blocks are produced and the leader
    never rotates to node 0, so the expired txs just sit there."""
    from .corpus_t import corpus_t
    import threading
    from live_node_fisco import simple_transfer_wave
    seed = next(s for s in corpus_t("fisco")
                if s.seed_id == "fisco-t-expired")
    events_before = net.log_count(1, "triggerTimeout") + \
        net.log_count(1, "broadcastViewChange")
    result = adapter.submit_seed(net, seed, {"node_index": 0})
    stop_stress = threading.Event()

    def stress():
        while not stop_stress.is_set():
            try:
                simple_transfer_wave(net.rpc_for(1), adapter.accounts,
                                     adapter.nonce_cache, 5,
                                     interval_ms=50)
            except Exception:
                pass
            time.sleep(2)

    driver = threading.Thread(target=stress, daemon=True)
    driver.start()
    try:
        time.sleep(120)
    finally:
        stop_stress.set()
        driver.join(timeout=10)
    events_after = net.log_count(1, "triggerTimeout") + \
        net.log_count(1, "broadcastViewChange")
    result.update({
        "events_before": events_before, "events_after": events_after,
        "events_delta": events_after - events_before,
        "node0_alive": net.alive(0),
        "node0_height": net.current_block_number(0),
    })
    return result


def verify_fs06(net, adapter: FiscoAdapter, obs: dict) -> tuple[bool, dict]:
    # healthy idle network: ~0 timeout/view-change events; one storm
    # event on a normal node (triggerTimeout or broadcastViewChange) is
    # already the paper's signature, >= 2 is unambiguous
    ok = obs.get("events_delta", 0) >= 2
    return ok, {"events_delta": obs.get("events_delta"),
                "submitted": obs.get("submitted"),
                "timeout_hangs": obs.get("timeout_hangs"),
                "errors": obs.get("errors"),
                "node0_alive": obs.get("node0_alive")}


def run_fs07(net, adapter: FiscoAdapter) -> dict:
    """PoC fs-04 flow: node 0 runs chain.block_limit=1 and rejects every
    broadcast tx (validity window compressed to [cur+1, cur+1]); a
    transfer wave flows through NORMAL node 1, and the judge is the
    timeout/view-change storm there while node 0's turns stall
    consensus (paper evidence 2)."""
    from live_node_fisco import simple_transfer_wave
    events_before = net.log_count(1, "triggerTimeout") + \
        net.log_count(1, "broadcastViewChange")
    sent = 0
    end = time.monotonic() + 120
    while time.monotonic() < end:
        sent += simple_transfer_wave(net.rpc_for(1), adapter.accounts,
                                     adapter.nonce_cache, 5,
                                     interval_ms=50)
        time.sleep(2)
    events_after = net.log_count(1, "triggerTimeout") + \
        net.log_count(1, "broadcastViewChange")
    return {"events_before": events_before, "events_after": events_after,
            "events_delta": events_after - events_before,
            "pending_after": net.pending_tx_size(1), "sent": sent,
            "node0_alive": net.alive(0)}


def verify_fs07(net, adapter: FiscoAdapter, obs: dict) -> tuple[bool, dict]:
    ok = obs.get("events_delta", 0) >= 2
    return ok, {"events_delta": obs.get("events_delta"),
                "pending_after": obs.get("pending_after"),
                "sent": obs.get("sent")}


# ---------------------------------------------------------------------------
# chainmaker
# ---------------------------------------------------------------------------

def _cm_setup(spec: CalibSpec, seed: int):
    from .targets.chainmaker_net import ChainMakerNetwork
    net = ChainMakerNetwork(Path(f"/tmp/calib-cm-{seed}"))
    net.prepare()
    if spec.chain_patch == "batch_pools":
        # PoC 06: the index-OOB verifier panic lives in the batch recovery
        # path — victims are batch-pool nodes, so ALL orgs run batch pools
        for org in net.orgs:
            net.set_pool_type(org, "batch")
    elif spec.chain_patch:
        # PoC 07/08: chainconfig patch (turbo / turbo+gas) on ALL orgs
        net.patch_bc1(spec.chain_patch)
    net.start_all()
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        if all(net.alive(o) for o in net.orgs):
            break
        time.sleep(2)
    return net, True


def run_cm_malicious(net, adapter: ChainMakerAdapter) -> dict:
    """Deploy contracts + drive invokes so tx-bearing blocks flow through
    the malicious proposer, then arm org5 with the capability flag and
    poll every other org's panic.log (PoC 06/07/08 pattern)."""
    from .corpus_m import corpus_m
    import threading
    seeds = {s.seed_id: s for s in corpus_m("chainmaker")}
    seed = seeds.get(adapter._calib_seed)
    flags = seed.payload["flags"]
    # cm-02's chain runs with gas enabled -> invokes must carry gas
    # (PoC 07 trigger loop uses --gas-limit=10000)
    gas_limit = 10000 if adapter._calib_seed == "cm-m-malicious-txcount" \
        else None
    # cm-03's nil-payload switch needs len(cutBlock.Txs) > 1, so blocks
    # must carry MULTIPLE txs: async invokes only (PoC 08 trigger loop
    # fires 8 rapid invokes per round without --sync-result)
    sync = adapter._calib_seed != "cm-m-malicious-nilpayload"
    stop_tx = threading.Event()

    def tx_driver():
        while not stop_tx.is_set():
            if net.ensure_contracts("wx-org1"):
                break
            time.sleep(3)
        while not stop_tx.is_set():
            net.invoke("wx-org1", 10, gas_limit=gas_limit, sync=sync)
            time.sleep(1.0)

    driver = threading.Thread(target=tx_driver, daemon=True)
    driver.start()
    try:
        # wait for the chain to flow before arming (B3 pattern: height>=5)
        height = -1
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            height = net.height()
            if height >= 5:
                break
            time.sleep(5)
        net.restart_org_with_env("wx-org5", flags)
        found: dict[str, str] = {}
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            for org in net.orgs:
                if org == "wx-org5":
                    continue
                log = net.panic_log(org)
                for sig in ("index out of range", "panic:", "nil pointer",
                            "TxCount", "concurrent map"):
                    if sig in log:
                        found[org] = sig
            if len(found) >= 2:  # PoC PASS criterion: >=2 crash nodes
                break
            time.sleep(3)
        return {"victims": found, "flags": flags, "height": height}
    finally:
        stop_tx.set()


def verify_cm_malicious(net, adapter: ChainMakerAdapter,
                        obs: dict) -> tuple[bool, dict]:
    return bool(obs.get("victims")), obs


# ---------------------------------------------------------------------------
# aptos
# ---------------------------------------------------------------------------

def _aptos_setup(spec: CalibSpec, seed: int):
    from .targets.aptos_net import AptosNetwork
    from . import item_catalog as ic
    import random as _random
    net = AptosNetwork(Path(f"/tmp/calib-aptos-{seed}"), n_validators=13)
    net.launch()
    if spec.preset:
        # node.yaml is read at (re)start; mutate then restart node 0 so the
        # preset takes effect (admission may legitimately fail for ap-12)
        adapter = AptosAdapter(_random.Random(seed))
        exempt = {p for p, _, _ in spec.preset}
        adapter.apply_mutations(net, 0, spec.preset,
                                ic.catalog_for("aptos"), exempt)
        net.stop_node(0)
        net.start_node(0, timeout=120)
    return net, True


def run_ap10(net, adapter: AptosAdapter) -> dict:
    """round_initial_timeout_ms=0 / sync_only: val0 stalls while peers
    advance.  The stall starts once val0's round timers fire (variable
    delay after restart — observed anywhere from ~2 min to past 5 min)
    and val0 later recovers via state sync and catches up, so end-of-
    window probes miss it.  Poll the ledger pair and record the series
    plus the FIRST divergence moment (the evidence), not the final
    state."""
    before0 = net.ledger(0)
    before1 = net.ledger(1)
    series: list[list] = []
    stall_ev: dict = {}
    deadline = time.monotonic() + 480
    while time.monotonic() < deadline:
        l0 = net.ledger(0)
        l1 = net.ledger(1)
        series.append([round(time.monotonic(), 1), l0, l1])
        growth0 = (l0 or 0) - (before0 or 0)
        growth1 = (l1 or 0) - (before1 or 0)
        if growth1 >= 30 and growth0 < growth1:
            stall_ev = {"l0": l0, "l1": l1,
                        "lag": (l1 or 0) - (l0 or 0),
                        "growth0": growth0, "growth1": growth1}
            break
        time.sleep(5)
    return {"before0": before0, "before1": before1,
            "after0": series[-1][1], "after1": series[-1][2],
            "stall_ev": stall_ev,
            "series": series[-40:]}


def verify_ap10(net, adapter: AptosAdapter, obs: dict) -> tuple[bool, dict]:
    # run-level evidence: the poll caught a divergence moment.  If the
    # stall only developed during the oracle's observe windows (after
    # the poll), the oracle's own stall signal — the PoC's judge — is
    # the evidence.
    stall_ev = obs.get("stall_ev") or {}
    lag = stall_ev.get("lag", 0)
    run_ok = bool(stall_ev) and lag > 0 and stall_ev.get("growth1", 0) >= 30
    fired = getattr(adapter, "_oracle_fired", []) or []
    ok = run_ok or "aptos_timeout_zero_stall" in fired \
        or "aptos_sync_only_stall" in fired
    return ok, {"stall_ev": stall_ev, "lag": lag, "oracle_fired": fired}


def run_ap12(net, adapter: AptosAdapter) -> dict:
    """safety_rules.service=process: validator fails to restart."""
    net.stop_node(0)
    time.sleep(2)
    alive = net.alive(0)
    restarted = net.start_node(0, timeout=60)
    time.sleep(5)
    return {"alive_after_stop": alive, "restarted": restarted,
            "alive_now": net.alive(0),
            "log_tail": net.log_text(0)[-600:]}


def verify_ap12(net, adapter: AptosAdapter, obs: dict) -> tuple[bool, dict]:
    # process-service safety rules with no external server: either the
    # node fails to come back or its log shows the failure signature
    ok = (not obs.get("alive_now", True)) or \
        ("safety" in obs.get("log_tail", "").lower() and
         ("error" in obs.get("log_tail", "").lower() or
          "panic" in obs.get("log_tail", "").lower()))
    return ok, {"alive_now": obs.get("alive_now")}


# ---------------------------------------------------------------------------
# specs
# ---------------------------------------------------------------------------

def build_specs() -> list[CalibSpec]:
    from .targets.geth_net import GethNetwork

    specs: list[CalibSpec] = []
    specs.append(CalibSpec(
        bug="ge-08", target="geth",
        description="miner gaslimit collapse (PoC ge-10)",
        preset=[("Eth.Miner.GasCeil", "dangerous", 5000)],
        run=run_ge08, verify=verify_ge08,
        signal="geth_gaslimit_collapse", timeout=1800,
        oracle_signals=["geth_gaslimit_collapse"]))
    specs.append(CalibSpec(
        bug="ge-09", target="geth",
        description="blobpool pricebump old-tx mining (PoC ge-13)",
        preset=[("Eth.BlobPool.PriceBump", "dangerous", 1_000_000)],
        run=run_ge09, verify=verify_ge09,
        signal="geth_replacement_rejected_old_finalized", timeout=900,
        oracle_signals=["geth_replacement_rejected_old_finalized"]))
    specs.append(CalibSpec(
        bug="fs-04", target="fisco",
        description="min seal time consensus slowdown (PoC fs-01)",
        preset=[("consensus.min_seal_time", "dangerous", 60000)],
        run=run_fs04, verify=verify_fs04,
        signal="fisco_consensus_timeout_growth", timeout=600,
        oracle_signals=["fisco_consensus_timeout_growth",
                        "fisco_view_change_storm"]))
    specs.append(CalibSpec(
        bug="fs-05", target="fisco",
        description="disable transaction signature check (PoC fs-02)",
        preset=[("experimental.check_transaction_signature",
                 "set_false", False)],
        run=run_fs05, verify=verify_fs05,
        signal="fisco_bad_signature_accepted", timeout=300,
        oracle_signals=["fisco_bad_signature_accepted",
                        "fisco_verify_sender_failed_storm"]))
    specs.append(CalibSpec(
        bug="fs-06", target="fisco",
        description="disable block limit check (PoC fs-03)",
        preset=[("txpool.check_block_limit", "set_false", False)],
        run=run_fs06, verify=verify_fs06,
        signal="fisco_expired_tx_accepted", timeout=300,
        oracle_signals=["fisco_expired_tx_accepted",
                        "fisco_view_change_storm"]))
    specs.append(CalibSpec(
        bug="fs-07", target="fisco",
        description="chain block limit collapse (PoC fs-04)",
        preset=[("chain.block_limit", "dangerous", 1)],
        run=run_fs07, verify=verify_fs07,
        signal="fisco_block_limit_1_pending_growth", timeout=300,
        oracle_signals=["fisco_block_limit_1_pending_growth",
                        "fisco_view_change_storm"]))
    # ChainMaker BCBs (paper Table 1):
    #   #1 (cm-01) = pool_type=batch + turbo TxCount OOB crash — the
    #       malicious-txcount capability flag under turbo_gas is the primary
    #       trigger; the index/nilpayload flags are the same bug's trigger
    #       family (kept as extra entries with non-paper ids).
    #   #2 (cm-02) = net.seeds peer-info-map race (restart_cycle ×
    #       concurrent_workload); may not reproduce on v3.0.0 (RWMutex).
    #   #3 (cm-03) = cert reconfiguration + logger level-map race; same
    #       version caveat.
    for bug, seed_id, chain_patch, signal, oracle_signals in (
            ("cm-01", "cm-m-malicious-txcount", "turbo_gas",
             "chainmaker_txcount_violation",
             ["chainmaker_txcount_violation", "chainmaker_verifier_panic"]),
            ("cm-01-index", "cm-m-malicious-index", "batch_pools",
             "chainmaker_verifier_panic", ["chainmaker_verifier_panic"]),
            ("cm-01-nilpayload", "cm-m-malicious-nilpayload", "turbo",
             "chainmaker_nilpayload_panic", ["chainmaker_nilpayload_panic"]),
            ("cm-02", "cm-m-net-seeds-race", None,
             "process_death", ["process_death", "chainmaker_verifier_panic"]),
            ("cm-03", "cm-m-cert-logger-race", None,
             "process_death", ["process_death", "chainmaker_verifier_panic"]),
    ):
        specs.append(CalibSpec(
            bug=bug, target="chainmaker",
            description=f"capability flag {seed_id} ({chain_patch})",
            chain_patch=chain_patch,
            run=run_cm_malicious, verify=verify_cm_malicious,
            signal=signal, timeout=600,
            oracle_signals=oracle_signals))
    specs.append(CalibSpec(
        bug="ap-10", target="aptos",
        description="round initial timeout zero (PoC ap-18)",
        preset=[("consensus.round_initial_timeout_ms", "dangerous", 0)],
        run=run_ap10, verify=verify_ap10,
        signal="aptos_timeout_zero_stall", timeout=600,
        oracle_signals=["aptos_timeout_zero_stall"]))
    specs.append(CalibSpec(
        bug="ap-11", target="aptos",
        description="sync_only true (PoC ap-19)",
        preset=[("consensus.sync_only", "set_true", True)],
        run=run_ap10, verify=verify_ap10,
        signal="aptos_sync_only_stall", timeout=600,
        oracle_signals=["aptos_sync_only_stall"]))
    specs.append(CalibSpec(
        bug="ap-12", target="aptos",
        description="safety rules dead process (PoC ap-20)",
        preset=[("safety_rules.service", "dangerous",
                 {"type": "process",
                  "server_address": "/ip4/127.0.0.1/tcp/5555"})],
        run=run_ap12, verify=verify_ap12,
        signal="aptos_safety_rules_process_failure", timeout=600,
        oracle_signals=["aptos_safety_rules_process_failure"]))
    return specs


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

ADAPTERS = {
    "geth": lambda rng: GethAdapter(rng),
    "fisco": lambda rng: FiscoAdapter(rng),
    "chainmaker": lambda rng: ChainMakerAdapter(rng),
    "aptos": lambda rng: AptosAdapter(rng),
}

SETUPS = {
    "geth": _geth_setup,
    "fisco": _fisco_setup,
    "chainmaker": _cm_setup,
    "aptos": _aptos_setup,
}


def apply_preset(net, adapter, spec: CalibSpec, target: str, seed: int) -> list:
    """Apply the spec's dangerous preset to the controlled node's config."""
    from . import item_catalog as ic
    catalog = ic.catalog_for(target)
    exempt = {item_path for item_path, _, _ in spec.preset}
    if target == "geth":
        import shutil
        from pathlib import Path as _P
        base = adapter.build_default_config(seed)
        target_cfg = _P(f"/tmp/calib-geth-{seed}/node0/conf.toml")
        return adapter.apply_mutations(
            _P(f"/tmp/calib-geth-{seed}/node0"), base, spec.preset,
            catalog, exempt, target_cfg)
    if target == "fisco":
        return adapter.apply_mutations(net, 0, spec.preset, catalog, exempt)
    if target == "chainmaker":
        return adapter.apply_mutations(net, 4, spec.preset, catalog, exempt)
    if target == "aptos":
        return adapter.apply_mutations(net, 0, spec.preset, catalog, exempt)
    return []


def _normal_indices(spec: CalibSpec) -> list[int]:
    """Oracle normal-view node set: everything except the mutated node."""
    if spec.target == "chainmaker":
        return [i for i in range(13) if i != 4]
    return list(range(1, 13))


def _placement_for(spec: CalibSpec):
    """Minimal placement carrier so the oracle's controlled-node
    classification (aptos #10/#11/#12) can see the preset mutations."""
    from types import SimpleNamespace
    return SimpleNamespace(metadata={"placements": [{
        "node": 4 if spec.target == "chainmaker" else 0,
        "role": "calibration",
        "config_id": "calib",
        "mutations": [{"item": item, "rule": rule, "value": value}
                      for item, rule, value in spec.preset],
    }]})


def run_spec(spec: CalibSpec, seed: int, out_dir: Path) -> dict:
    import random
    from .oracle import BcbOracle
    rng = random.Random(seed)
    adapter = ADAPTERS[spec.target](rng)
    if spec.target == "chainmaker":
        adapter._calib_seed = {
            "cm-01": "cm-m-malicious-txcount",
            "cm-01-index": "cm-m-malicious-index",
            "cm-01-nilpayload": "cm-m-malicious-nilpayload",
            "cm-02": "cm-m-net-seeds-race",
            "cm-03": "cm-m-cert-logger-race",
        }[spec.bug]
    setup = SETUPS[spec.target]
    net = None
    try:
        if spec.target == "geth":
            from .targets.geth_net import GethNetwork
            base = adapter.build_default_config(seed)
            import shutil
            from pathlib import Path as _P
            work = _P(f"/tmp/calib-geth-{seed}")
            target_cfg = work / "node0" / "conf.toml"
            net = GethNetwork(work, n_nodes=13,
                              networkid=GETH_CALIB_NETWORKID,
                              kill_stale=GETH_CALIB_KILL_STALE)
            net.setup()
            ops = adapter.apply_mutations(
                work / "node0", base, spec.preset,
                __import__("bcfuzzer.item_catalog", fromlist=["catalog_for"]).catalog_for("geth"),
                {p for p, _, _ in spec.preset}, target_cfg)
            ok = net.start_all({0: target_cfg}, {0}, work / "logs")
            if not ok:
                raise RuntimeError(
                    "geth 13-node start_all failed: one or more nodes did "
                    "not come up — the 13-node calibration is invalid on "
                    "a degraded network")
        else:
            # fisco/chainmaker/aptos setups apply spec.preset themselves
            # (after build, before start_all — genesis configs must carry
            # the preset at first launch)
            net, ok = setup(spec, seed)
        # plan E: the oracle's own signal must fire, not just the
        # verifier's — baseline registered while the network is still
        # healthy, then observe after the bug has manifested
        oracle = BcbOracle(spec.target, _normal_indices(spec))
        oracle.register_baseline(net, adapter)
        # spec verifiers compare against the PRE-mutation healthy state
        adapter._calib_baseline = oracle.baseline
        # run_ge08 observes the live collapse window mid-run (between sync
        # and handover); the signals it fires are merged into the record
        adapter._calib_oracle = oracle
        if spec.target == "fisco" and spec.preset:
            # runtime presets: mutate AFTER the healthy baseline and
            # restart the controlled node, so growth signals compare
            # against pre-bug state (PoC fs-01/fs-02/fs-03 flow and the
            # campaign's own mutate->restart->admission round)
            from . import item_catalog as ic
            runtime = [(item, rule, value) for item, rule, value
                       in spec.preset if item not in FISCO_GENESIS_ITEMS]
            if runtime:
                exempt = {p for p, _, _ in runtime}
                adapter.apply_mutations(net, 0, runtime,
                                        ic.catalog_for("fisco"), exempt)
                net.stop_node(0)
                net.start_node(0, timeout=60)
        observations = spec.run(net, adapter)
        seed_results = None
        if isinstance(observations, dict) and \
                ("kind" in observations or "pair" in observations):
            seed_results = [observations]
        fired: list[str] = []
        failure_details: list[dict] = []
        # up to 3 observes, one full window apart: durable-window
        # signals (geth collapse, controlled-node stalls) need
        # CONSECUTIVE windows, and delta-based signals (fisco view-change
        # storm) can never fire from back-to-back probes that share a
        # single instant — the storm must accumulate events BETWEEN
        # observes.  The 3rd observe needs no trailing sleep.  Observed
        # BEFORE verify so verifiers can use oracle evidence (aptos
        # #10/#11 stalls can first develop during the observe phase).
        for obs_i in range(3):
            failures = oracle.observe(net, adapter, 0,
                                      seed_results=seed_results,
                                      placement=_placement_for(spec))
            fired.extend(f.signal for f in failures)
            failure_details.extend(
                {"signal": f.signal, "node": f.node, "detail": f.detail}
                for f in failures)
            if obs_i < 2:
                time.sleep(oracle.window)
        fired.extend(getattr(adapter, "_midrun_fired", []) or [])
        adapter._oracle_fired = sorted(set(fired))
        oracle_ok = bool(set(fired) & set(spec.oracle_signals)) \
            if spec.oracle_signals else None
        passed, detail = spec.verify(net, adapter, observations)
        record = {"bug": spec.bug, "target": spec.target,
                  "passed": passed, "signal": spec.signal,
                  "oracle_signals_expected": spec.oracle_signals,
                  "oracle_fired": sorted(set(fired)),
                  "oracle_ok": oracle_ok,
                  "failures": failure_details,
                  "observations": observations, "detail": detail,
                  "description": spec.description}
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{spec.bug}.json").write_text(
            json.dumps(record, indent=2, default=str), encoding="utf-8")
        return record
    finally:
        if net is not None:
            net.teardown()


def run_calibration(bugs: list[str] | None, out_dir: Path,
                    seed: int = 7) -> list[dict]:
    specs = [s for s in build_specs()
             if bugs is None or s.bug in bugs]
    records = []
    for spec in specs:
        print(f"[calibrate] {spec.bug} ({spec.target}): {spec.description}",
              flush=True)
        record = run_spec(spec, seed, out_dir)
        records.append(record)
        print(f"[calibrate] {spec.bug}: {'PASS' if record['passed'] else 'FAIL'}",
              flush=True)
    summary = {"total": len(records),
               "passed": sum(1 for r in records if r["passed"]),
               "records": records}
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return records
