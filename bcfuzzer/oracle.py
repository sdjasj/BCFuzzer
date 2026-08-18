"""BCB Oracle (design §3.6): baseline, three failure classes, capacity.

Baseline: registered during an idle window with a default config (block
rate / view-change rate / panic counts / gasLimit / ledger growth).

Per round the oracle observes the *normal nodes'* view (read-only) and
classifies failures:

  peer_failure       peer process death + language-panic signatures
                     (geth/chainmaker/fisco/aptos panic fragments)
  progress_failure  chain stops committing for PERSISTENCE_WINDOWS
                     consecutive 20 s windows while nodes stay alive;
                     fisco view-change storms (consensusTimeout growth,
                     triggerTimeout/broadcastViewChange) count as
                     progress failure (paper #4)
  transaction_failure  tx-level anomalies: bad-signature txs accepted
                     into the pool (#5), expired txs accepted (#6),
                     replacement rejected while the old tx finalizes (#9)
  capacity           geth-only: gasLimit/21000 nominal capacity.  A gas
                     limit below the PoC collapse threshold (300k) that
                     PERSISTS across rounds (durable window) is the
                     paper #8 oracle.

BugReport is emitted once per failure signature (dedup); minimization and
regression recheck are driven by the campaign.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from .common import BugReport, MutationOp

WINDOW_SEC = 20.0
PERSISTENCE_WINDOWS = 3
GETH_COLLAPSE_THRESHOLD = 300_000  # PoC geth/01 success threshold
FISCO_TIMEOUT_GROWTH_FACTOR = 3    # consensusTimeout >= 3x baseline (#4)
FISCO_VIEW_CHANGE_GROWTH = 10      # triggerTimeout+broadcastViewChange >= 10x baseline
# when the healthy baseline rate is 0 (an idle network logs ~0 timeout
# events per window), a sustained burst still has to be detectable:
# >= 2 events/window per normal node (paper #4/#6/#7 evidence: each
# consensus stall logs triggerTimeout + broadcastViewChange)
FISCO_VIEW_CHANGE_FLOOR_PER_SEC = 0.1

# panic-log fragment -> per-bug signal (the generic panic_<sig> signal
# stays available for fragments with no mapping).  The TXCOUNT bug (cm-02)
# panics with `index out of range [101]` — a large index — so it is matched
# by "index out of range [1" BEFORE the generic "index out of range"
# (cm-01) catches it.  Both bugs panic on OOB index; the index magnitude
# distinguishes them (cm-01 corrupts index[0]=0xFFFFFFFF → small-index
# panic; cm-02 inflates TxCount by 100 → index ≥100).
PANIC_SIGNAL_MAP: dict[str, dict[str, str]] = {
    "chainmaker": {
        "index out of range [1": "chainmaker_txcount_violation",
        "index out of range": "chainmaker_verifier_panic",
        "TxCount": "chainmaker_txcount_violation",
        "nil pointer": "chainmaker_nilpayload_panic",
    },
    "fisco": {
        "verify sender for tx failed": "fisco_verify_sender_failed_storm",
    },
    "aptos": {
        "safety": "aptos_safety_rules_process_failure",
    },
}

# signal -> (targets, severity, description); calibration asserts each fired
SIGNAL_LIBRARY: dict[str, dict[str, Any]] = {
    "process_death": {
        "targets": set(), "severity": "critical", "bug": "",
        "desc": "peer process death observed from the normal-node view"},
    "durable_stall": {
        "targets": set(), "severity": "critical", "bug": "",
        "desc": "normal-node height stalled for 3 consecutive windows"},
    "fisco_view_change_storm": {
        "targets": {"fisco"}, "severity": "warning", "bug": "fs-04",
        "desc": "triggerTimeout+broadcastViewChange >= 10x baseline"},
    "geth_gaslimit_collapse": {
        "targets": {"geth"}, "severity": "critical", "bug": "ge-08",
        "desc": "normal-node gasLimit < 300k persisting after producer handover"},
    "geth_replacement_rejected_old_finalized": {
        "targets": {"geth"}, "severity": "warning", "bug": "ge-09",
        "desc": "blob replacement rejected while the old blob tx finalizes"},
    "fisco_consensus_timeout_growth": {
        "targets": {"fisco"}, "severity": "warning", "bug": "fs-04",
        "desc": "consensusTimeout >= 3x baseline + view-change storm"},
    "fisco_bad_signature_accepted": {
        "targets": {"fisco"}, "severity": "critical", "bug": "fs-05",
        "desc": "65-zero-byte-signature tx accepted into the pool"},
    "fisco_verify_sender_failed_storm": {
        "targets": {"fisco"}, "severity": "critical", "bug": "fs-05",
        "desc": "verify sender for tx failed log storm"},
    "fisco_expired_tx_accepted": {
        "targets": {"fisco"}, "severity": "warning", "bug": "fs-06",
        "desc": "block_limit=0 tx accepted by controlled node"},
    "fisco_block_limit_1_pending_growth": {
        "targets": {"fisco"}, "severity": "warning", "bug": "fs-07",
        "desc": "pending tx pool growth under block_limit=1"},
    "chainmaker_verifier_panic": {
        "targets": {"chainmaker"}, "severity": "critical", "bug": "cm-01",
        "desc": "peer org panic.log shows index out of range / panic:"},
    "chainmaker_txcount_violation": {
        "targets": {"chainmaker"}, "severity": "critical", "bug": "cm-02",
        "desc": "TxCount out-of-bounds verification failure"},
    "chainmaker_nilpayload_panic": {
        "targets": {"chainmaker"}, "severity": "critical", "bug": "cm-03",
        "desc": "nil payload verification panic"},
    "aptos_timeout_zero_stall": {
        "targets": {"aptos"}, "severity": "warning", "bug": "ap-10",
        "desc": "round_initial_timeout_ms=0 validator stalls consensus"},
    "aptos_sync_only_stall": {
        "targets": {"aptos"}, "severity": "warning", "bug": "ap-11",
        "desc": "sync_only validator never proposes; quorum waits"},
    "aptos_safety_rules_process_failure": {
        "targets": {"aptos"}, "severity": "critical", "bug": "ap-12",
        "desc": "safety_rules.service=process fails startup / panics"},
}


def keccak(raw: bytes) -> str:
    return "0x" + hashlib.sha3_256(raw).hexdigest()


@dataclass
class Baseline:
    target: str
    height_rate: float = 0.0            # blocks/ledger per window
    view_change_rate: float = 0.0       # fisco per window
    consensus_timeouts: list[str] = field(default_factory=list)
    gaslimit: int = 0                   # geth
    panic_signatures: dict = field(default_factory=dict)
    pending: int = 0                    # fisco
    ts: float = 0.0


@dataclass
class Failure:
    category: str      # peer | progress | transaction | capacity
    signal: str
    node: int
    detail: dict = field(default_factory=dict)


def _height_of(probe: dict) -> int:
    value = probe.get("height", probe.get("ledger"))
    if isinstance(value, int):
        return value
    return -1


class BcbOracle:
    def __init__(self, target: str, normal_indices: list[int],
                 window_sec: float = WINDOW_SEC) -> None:
        self.target = target
        self.normal_indices = list(normal_indices)
        self.window = window_sec
        # 13-org TBFT block production is slower than the other targets;
        # require more consecutive stalled windows before declaring a
        # durable stall so the slow-but-healthy chain never false-fires
        self.stall_windows = 9 if target == "chainmaker" \
            else PERSISTENCE_WINDOWS
        self.baseline: Baseline | None = None
        self._last: dict[int, tuple[float, int]] = {}
        self._stall: dict[int, int] = {}
        self._ever_grew = False
        self._view_change_last: dict[int, int] = {}
        self._timeout_last: dict[int, list[str]] = {}
        self._pending_last: dict[int, int] = {}
        self._collapse_rounds: dict[int, int] = {}
        self._bl1_armed_round: int | None = None
        self.reports: list[BugReport] = []
        self._seen_signatures: set[str] = set()
        self._reports_by_sig: dict[str, BugReport] = {}
        self._round = 0

    # ------------------------------------------------------------- baseline

    def register_baseline(self, net, adapter) -> Baseline:
        probes = {i: adapter.node_probes(net, i) for i in self.normal_indices}
        t0 = time.monotonic()
        time.sleep(self.window)
        probes2 = {i: adapter.node_probes(net, i) for i in self.normal_indices}
        heights = [max(0, _height_of(probes2[i]) - _height_of(probes[i]))
                   for i in self.normal_indices]
        rate = (sum(heights) / len(heights)) if heights else 0.0
        panics: dict[str, int] = {}
        for i in self.normal_indices:
            for sig, count in self._panic_signatures(probes2[i]).items():
                panics[sig] = panics.get(sig, 0) + count
        gaslimit = 0
        if self.target == "geth":
            gaslimit = max((probes2[i].get("gaslimit", 0)
                            for i in self.normal_indices), default=0)
        timeouts = sorted({str(t)
                           for i in self.normal_indices
                           for t in probes2[i].get("consensus_timeouts", [])})
        pending = max((probes2[i].get("pending", 0)
                       for i in self.normal_indices), default=0)
        view_changes = sum(
            max(0, probes2[i].get("timeout_events", 0)
                - probes[i].get("timeout_events", 0))
            for i in self.normal_indices)
        self.baseline = Baseline(
            target=self.target, height_rate=rate,
            view_change_rate=view_changes / self.window,
            consensus_timeouts=timeouts, gaslimit=gaslimit,
            panic_signatures=panics, pending=pending,
            ts=time.monotonic())
        for i in self.normal_indices:
            self._last[i] = (time.monotonic(), _height_of(probes2[i]))
        return self.baseline

    def settle_after_restarts(self, net, adapter,
                              settle_sec: float = 45.0) -> None:
        """Re-baseline the view-change counters after the round's serial
        restarts (fisco only).  A restarted PBFT node re-reaches a view
        in 10-45 s and the view-change traffic that causes is restart
        noise, not a bug: without settling, every round's storm delta
        includes the aftershocks and benign rounds false-fire (measured:
        36-69 events on a round with benign mutations).  A true
        min_seal_time=60000 (#4) storm keeps bursting every ~60 s for
        the whole round, so the post-settle observation window — seeds,
        sequences, then observe() — still catches it."""
        if self.target != "fisco":
            return
        time.sleep(settle_sec)
        for i in self.normal_indices:
            probe = adapter.node_probes(net, i)
            self._view_change_last[i] = probe.get("timeout_events", 0)

    # ----------------------------------------------------------- observation

    def observe(self, net, adapter, round_id: int,
                seed_results: list[dict] | None = None,
                placement=None) -> list[Failure]:
        self._round = round_id
        failures: list[Failure] = []
        probes = {i: adapter.node_probes(net, i) for i in self.normal_indices}
        now = time.monotonic()

        # peer_failure: death or language panics
        for i, probe in probes.items():
            if not probe.get("alive", True):
                failures.append(Failure("peer", "process_death", i,
                                        {"round": round_id}))
            for sig, count in self._panic_signatures(probe).items():
                base = (self.baseline.panic_signatures.get(sig, 0)
                        if self.baseline else 0)
                if count > base:
                    mapped = (PANIC_SIGNAL_MAP.get(self.target, {})
                              .get(sig, f"panic_{sig}"))
                    failures.append(Failure(
                        "peer", mapped, i,
                        {"count": count, "baseline": base}))

        # progress_failure: durable stall on normal nodes
        normal_growth = 0
        for i, probe in probes.items():
            height = _height_of(probe)
            last_ts, last_height = self._last.get(i, (now, height))
            normal_growth = max(normal_growth, height - last_height)
            elapsed = max(now - last_ts, 1e-6)
            if height < 0:
                # probe failure (rpc error): keep the window state, do
                # not count a stall we have no evidence for
                self._last[i] = (now, last_height)
                continue
            if height == 0 and last_height > 0:
                # chains never rewind: a 0 read after a positive height
                # is a failed rpc (fisco getBlockNumber returns 0 on
                # error), not a stall
                self._last[i] = (now, last_height)
                continue
            if height > last_height:
                self._ever_grew = True
            if (self._ever_grew and height <= last_height
                    and elapsed >= self.window * 0.8):
                # a chain that NEVER grew past its baseline height is not
                # "stalled" — it never had evidence of working (geth
                # normal nodes only import when driven; a pathological
                # round that keeps them at genesis must not flood the
                # leg with stall reports)
                self._stall[i] = self._stall.get(i, 0) + 1
            else:
                self._stall[i] = 0
            if self._stall.get(i, 0) >= self.stall_windows:
                failures.append(Failure(
                    "progress", "durable_stall", i,
                    {"windows": self._stall[i], "height": height}))
            self._last[i] = (now, max(height, last_height))

        # fisco: consensus timeout growth + view-change storm (paper #4)
        if self.target == "fisco":
            if self._block_limit_armed(placement):
                self._bl1_armed_round = round_id
            bl1_active = (self._bl1_armed_round is not None
                          and round_id - self._bl1_armed_round <= 1)
            for i, probe in probes.items():
                timeouts = sorted({str(t) for t in
                                   probe.get("consensus_timeouts", [])})
                base_timeouts = (self.baseline.consensus_timeouts
                                 if self.baseline else [])
                base_timeouts = base_timeouts or ["3000"]
                grown = [t for t in timeouts
                         if int(t) >= FISCO_TIMEOUT_GROWTH_FACTOR
                         * max(int(b) for b in base_timeouts)]
                if grown:
                    failures.append(Failure(
                        "progress", "fisco_consensus_timeout_growth", i,
                        {"values": grown, "baseline": base_timeouts}))
                events = probe.get("timeout_events", 0)
                delta = max(0, events - self._view_change_last.get(i, events))
                self._view_change_last[i] = events
                if self.baseline:
                    threshold = max(
                        FISCO_VIEW_CHANGE_GROWTH
                        * self.baseline.view_change_rate * self.window,
                        FISCO_VIEW_CHANGE_FLOOR_PER_SEC * self.window)
                    if delta >= threshold:
                        failures.append(Failure(
                            "progress", "fisco_view_change_storm", i,
                            {"rate": delta}))
                pending = probe.get("pending", 0)
                # gate 1: only while chain.block_limit=1 is (recently)
                # armed — the signal name claims #7, so firing it without
                # the trigger armed is a mislabel.  gate 2: a node in
                # durable stall piles up pending because its executor is
                # stuck, not because of block_limit — that pileup is a
                # stall symptom and must not masquerade as a #7
                # observation (stageG3 fisco leg: rounds 54-59 fired
                # pending_growth on deadlocked node5, including rounds
                # 54-55 which PREDATE the round-56 arming)
                if (self.baseline and bl1_active
                        and self._stall.get(i, 0) < self.stall_windows
                        and pending >= max(
                            2 * self.baseline.pending + 10, 50)):
                    failures.append(Failure(
                        "transaction", "fisco_block_limit_1_pending_growth",
                        i, {"pending": pending,
                            "baseline_pending": self.baseline.pending}))

        # transaction_failure from seed results
        for result in seed_results or []:
            failure = self._classify_seed_result(result)
            if failure is not None:
                failures.append(failure)

        # capacity (geth, durable window)
        if self.target == "geth":
            for i in probes:
                gaslimit = probes[i].get("gaslimit", 0)
                if gaslimit and gaslimit < GETH_COLLAPSE_THRESHOLD:
                    self._collapse_rounds[i] = self._collapse_rounds.get(i, 0) + 1
                    if self._collapse_rounds.get(i, 0) >= PERSISTENCE_WINDOWS:
                        failures.append(Failure(
                            "capacity", "geth_gaslimit_collapse", i,
                            {"gaslimit": gaslimit,
                             "rounds_below": self._collapse_rounds[i]}))
                else:
                    self._collapse_rounds[i] = 0

        # controlled-node view (aptos #10/#11/#12): the signature lives on
        # the MUTATED node itself while the normal view stays healthy, so
        # the normal-only probes above can never see it
        if self.target == "aptos" and placement is not None:
            self._observe_controlled_aptos(
                net, adapter, placement, failures, normal_growth)

        return failures

    @staticmethod
    def _block_limit_armed(placement) -> bool:
        """True when a controlled node's mutation this round set
        chain.block_limit to 1 (the paper #7 trigger value)."""
        for node_plan in getattr(placement, "placements", None) or []:
            for item_path, _rule, value in \
                    getattr(node_plan, "mutations", []) or []:
                if item_path == "chain.block_limit" and value in (1, "1"):
                    return True
        return False

    def _observe_controlled_aptos(self, net, adapter, placement,
                                  failures: list[Failure],
                                  normal_growth: int) -> None:
        meta = getattr(placement, "metadata", None) or {}
        for p in meta.get("placements", []):
            node = p.get("node")
            if node is None:
                continue
            items = {m.get("item") for m in p.get("mutations", [])}
            if not items:
                continue
            probe = adapter.node_probes(net, node)
            alive = probe.get("alive", True)
            if "safety_rules.service" in items and not alive:
                # PoC ap-20: process-service safety rules never restart
                failures.append(Failure(
                    "peer", "aptos_safety_rules_process_failure", node,
                    {"alive": alive}))
            stall_items = items & {"consensus.round_initial_timeout_ms",
                                   "consensus.sync_only"}
            if stall_items and alive:
                ledger = _height_of(probe)
                key = ("c", node)
                now = time.monotonic()
                last_ts, last_h = self._last.get(key, (now, ledger))
                if ledger < 0:
                    self._last[key] = (now, last_h)
                elif ledger <= last_h:
                    self._stall[key] = self._stall.get(key, 0) + 1
                else:
                    self._stall[key] = 0
                    self._last[key] = (now, ledger)
                if (self._stall.get(key, 0) >= self.stall_windows
                        and normal_growth > 0):
                    signal = ("aptos_sync_only_stall"
                              if "consensus.sync_only" in items
                              else "aptos_timeout_zero_stall")
                    failures.append(Failure(
                        "progress", signal, node,
                        {"windows": self._stall[key],
                         "ledger": ledger,
                         "normal_growth": normal_growth}))

    def _classify_seed_result(self, result: dict) -> Failure | None:
        kind = result.get("kind", "")
        if result.get("kind") == "bad_signature" and result.get("accepted", 0) > 0:
            return Failure("transaction", "fisco_bad_signature_accepted",
                           result.get("node", 0), {"accepted": result["accepted"]})
        if result.get("kind") == "expired_tx" and (
                result.get("submitted", 0) > 0 or
                result.get("timeout_hangs", 0) > 0):
            # a sync sendTransaction hang after a 5 s rpc timeout is the
            # PoC fs-03 "RPC 同步等待挂起" case: the tx was pooled and
            # the network cannot agree on the block carrying it
            return Failure("transaction", "fisco_expired_tx_accepted",
                           result.get("node", 0), result)
        if result.get("pair") and (result.get("accepted", 0) < 2 or
                                   "underpriced" in result.get(
                                       "second_error", "")):
            # replacement side rejected (pool-level, parsed from the RPC
            # body — the stock send_raw_transaction is blind to HTTP-200
            # JSON-RPC errors); the old tx may still finalize (#9)
            return Failure("transaction",
                           "geth_replacement_rejected_old_finalized",
                           result.get("node", 0),
                           {"accepted": result.get("accepted", 0),
                            "second_error": result.get("second_error", "")})
        if result.get("kind") in ("tars_empty", "tars_oversized", "tars_truncated") \
                and result.get("error"):
            return None  # M-corpus probes: errors are expected outcomes
        return None

    # ---------------------------------------------------------------- reports

    def report(self, failure: Failure, round_id: int, placement,
               ops: list[MutationOp]) -> BugReport | None:
        """Emit a deduplicated BugReport for one failure.

        The signature keys on (signal, node).  Keying on the mutation set
        never deduped across rounds — every round's placement differs, so
        a persistent condition re-fired a near-identical report each round
        (stageG3 fisco leg: 59 durable_stall + 10 pending_growth reports
        for two findings).  A per-round repeat of the same observation on
        the same node is ONE finding; it accumulates an occurrences
        counter while the timeline keeps the full per-round evidence.  A
        same-round network-wide storm is still one report per affected
        node (the final analysis groups them by signal)."""
        node = getattr(failure, "node", None)
        signature = f"{self.target}:{failure.signal}:{node}"
        existing = self._reports_by_sig.get(signature)
        if existing is not None:
            observed = existing.observed
            observed["occurrences"] = observed.get("occurrences", 1) + 1
            observed["last_round"] = round_id
            return None
        info = SIGNAL_LIBRARY.get(failure.signal, {})
        bug_tags = [info.get("bug", "")]
        from .common import Placement as _P  # noqa: F401
        report = BugReport(
            bug_id=f"bcfuzzer-{len(self.reports):03d}",
            target=self.target,
            category=failure.category,
            signal=failure.signal,
            round_id=round_id,
            severity=info.get("severity", "warning"),
            observed={**failure.detail,
                      "node": node,
                      "occurrences": 1,
                      "last_round": round_id,
                      "placement": placement.metadata if placement else {}},
            signature=signature,
            minimized_ops=[op for op in ops],
        )
        self._reports_by_sig[signature] = report
        self.reports.append(report)
        return report

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _panic_signatures(probe: dict) -> dict[str, int]:
        signatures = probe.get("panic_signatures",
                               probe.get("log_signatures", {}))
        return {str(k): int(v) for k, v in signatures.items() if v}

    def capacity(self, net, adapter, index: int) -> int | None:
        if self.target == "geth" and hasattr(net, "nominal_capacity"):
            return net.nominal_capacity(index)
        return None

    def save(self, path) -> None:
        from .common import save_json
        save_json(path, {"baseline": self.baseline,
                         "reports": self.reports,
                         "seen": sorted(self._reports_by_sig)})

    def load(self, path) -> None:
        from .common import load_json
        data = load_json(path)
        if data.get("baseline"):
            self.baseline = Baseline(**data["baseline"])
        self.reports = data.get("reports", [])
        self._reports_by_sig = {r.signature: r for r in self.reports}
