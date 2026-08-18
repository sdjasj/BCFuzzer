#!/usr/bin/env python3
"""BCFuzzer fuzzing engine campaign driver (design §3).

Three modes:
  fuzz       run the full engine: 13-node network per target, two-level
             scheduler, T/M corpora, sequence primitives, BCB oracle.
  calibrate  replay every paper bug through the fuzzer's own primitives
             and assert the oracle signal fires (bcfuzzer/calibration.py).
  regress    re-run the inter-node-bugs-final PoCs via full_bcfuzzer's
             BUG_SPECS (bcfuzzer/regression.py).

Layout under --output:  state/ (mei.json, scheduler.json, oracle.json,
campaign.json), timeline.jsonl, result.json, calibration/|regression/.

Exit code 0 = clean run (failures found or none); 2 = engine crash.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from bcfuzzer.common import BugReport, save_json  # noqa: E402
from bcfuzzer.mei import MeiState, summarize  # noqa: E402
from bcfuzzer.scheduler import RoundPlan, TwoLevelScheduler  # noqa: E402
from bcfuzzer.corpus_t import corpus_t  # noqa: E402
from bcfuzzer.corpus_m import corpus_m  # noqa: E402
from bcfuzzer.oracle import BcbOracle  # noqa: E402
from bcfuzzer.sequences import (  # noqa: E402
    concurrent_workload, drive_blocks, restart_cycle, rotate_role,
    submit_pair)
from bcfuzzer import item_catalog  # noqa: E402

ADAPTERS = {
    "geth": "bcfuzzer.targets.geth_adapter:GethAdapter",
    "fisco": "bcfuzzer.targets.fisco_adapter:FiscoAdapter",
    "chainmaker": "bcfuzzer.targets.chainmaker_adapter:ChainMakerAdapter",
    "aptos": "bcfuzzer.targets.aptos_adapter:AptosAdapter",
}


def load_adapter(target: str, rng: random.Random):
    module_path, class_name = ADAPTERS[target].split(":")
    module = __import__(module_path, fromlist=[class_name])
    return getattr(module, class_name)(rng)


class NetSession:
    """Per-target network lifecycle across rounds."""

    def __init__(self, target: str, runtime: Path, n_nodes: int,
                 seed: int) -> None:
        self.target = target
        self.runtime = Path(runtime)
        self.n = n_nodes
        self.seed = seed
        self.net = None
        self.network: Any = None

    def build(self):
        if self.target == "geth":
            from bcfuzzer.targets.geth_net import GethNetwork
            return GethNetwork(self.runtime, n_nodes=self.n)
        if self.target == "fisco":
            from bcfuzzer.targets.fisco_net import FiscoNetwork
            return FiscoNetwork(self.runtime, n_nodes=self.n)
        if self.target == "chainmaker":
            from bcfuzzer.targets.chainmaker_net import ChainMakerNetwork
            return ChainMakerNetwork(self.runtime)
        if self.target == "aptos":
            from bcfuzzer.targets.aptos_net import AptosNetwork
            return AptosNetwork(self.runtime, n_validators=self.n)
        raise ValueError(self.target)

    def ensure_ready(self) -> Any:
        if self.network is not None:
            return self.network
        network = self.build()
        if self.target == "geth":
            network.setup()
            # default-config network for the baseline window; round 1
            # replaces the producer config with the mutated one
            network.start_all({}, {0}, self.runtime / "baseline-logs")
        elif self.target == "fisco":
            network.build()
            network.start_all(timeout=240)
        elif self.target == "chainmaker":
            network.prepare()
            network.start_all()
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                if all(network.alive(o) for o in network.orgs):
                    break
                time.sleep(2)
        elif self.target == "aptos":
            network.launch()
        self.network = network
        return network

    def teardown(self) -> None:
        if self.network is not None:
            if os.environ.get("BCFZ_KEEP_RUNTIME") == "1":
                # debugging: leave node dirs/logs behind for inspection
                self.network.stop_all() if hasattr(self.network, "stop_all") \
                    else None
            else:
                self.network.teardown()
            self.network = None


class Campaign:
    def __init__(self, target: str, out_dir: Path, n_nodes: int,
                 controlled: list[int], seed: int, resume_state: Path | None,
                 exploration_rounds: int = 2) -> None:
        self.target = target
        self.out_dir = out_dir
        self.state_dir = out_dir / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.n_nodes = n_nodes
        self.controlled = controlled
        self.rng = random.Random(seed)
        self.catalog = item_catalog.catalog_for(target)
        self.seeds = corpus_t(target) + corpus_m(target)
        self.adapter = load_adapter(target, self.rng)
        self.mei = MeiState()
        self.scheduler = TwoLevelScheduler(
            target, self.catalog, self.seeds, n_nodes, controlled, self.rng,
            exploration_rounds=exploration_rounds)
        self.oracle = BcbOracle(target, [i for i in range(n_nodes)
                                         if i not in set(controlled)])
        self.timeline: list[dict] = []
        self._pristine: dict[int, dict[str, bytes | None]] = {}
        if resume_state is not None:
            self._resume(resume_state)

    # ------------------------------------------------------------- state

    def _resume(self, state_dir: Path) -> None:
        if (state_dir / "mei.json").is_file():
            self.mei = MeiState.load(state_dir / "mei.json")
        if (state_dir / "scheduler.json").is_file():
            self.scheduler = TwoLevelScheduler.load(
                state_dir / "scheduler.json", self.catalog, self.seeds,
                self.rng, self.n_nodes)
        if (state_dir / "oracle.json").is_file():
            self.oracle.load(state_dir / "oracle.json")

    def persist(self, round_id: int, round_record: dict) -> None:
        self.mei.save(self.state_dir / "mei.json")
        self.scheduler.save(self.state_dir / "scheduler.json")
        self.oracle.save(self.state_dir / "oracle.json")
        save_json(self.state_dir / "campaign.json",
                  {"target": self.target, "round_id": round_id,
                   "controlled": self.controlled,
                   "last_round": round_record})
        with (self.out_dir / "timeline.jsonl").open("a",
                                                    encoding="utf-8") as fh:
            fh.write(json.dumps(round_record, default=str) + "\n")

    # --------------------------------------------------------- precondition

    def precondition_ok(self, seed, node_plan) -> bool:
        pre = seed.preconditions or {}
        if "config" in pre:
            key, expected = str(pre["config"]).split("=", 1)
            matched = any(
                path == key and str(value).lower() == expected.lower()
                for path, _, value in node_plan.mutations)
            if not matched:
                return False
        if "role" in pre and pre["role"] == "proposer":
            if self.target != "chainmaker":
                return False
            # TBFT round-robins the proposer through all orgs; the
            # capability flag takes effect on the restarted org's next
            # proposal, and the malicious batch panics the VERIFIER orgs
            # (detected by the oracle), so the seed need not wait for the
            # controlled org to currently hold the role.  The prior
            # `current_proposer() == org` gate starved every M seed in the
            # stageG3 chainmaker leg: cmc consensus status returns no
            # proposer field (the org is base64-encoded inside protobuf
            # vote signatures), so current_proposer() never resolved and
            # 0/3 M seeds executed across 50 rounds.
            return True
        return True

    # ------------------------------------------------------------ one round

    def snapshot_pristine(self, net) -> None:
        """Capture every controlled node's config files at campaign start.

        Non-geth targets edit the live runtime configs in place, so without
        a restore step mutations accumulate across rounds and the node
        config drifts into garbage (chainmaker leg: one item mutated 100+
        times, 94% of probed configs rejected)."""
        self._pristine = {}
        for index in self.controlled:
            self._pristine[index] = {
                str(p): (p.read_bytes() if p.is_file() else None)
                for p in self.adapter.pristine_files(net, index)}

    def restore_pristine(self, index: int) -> None:
        for name, data in self._pristine.get(index, {}).items():
            path = Path(name)
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

    def apply_placements(self, session: NetSession, plan: RoundPlan,
                         round_work: Path) -> dict[int, list]:
        """Materialize per-node configs; returns {node: ops}."""
        net = session.network
        ops_by_node: dict[int, list] = {}
        exempt_by_node: dict[int, set[str]] = {}
        for node_plan in plan.placements:
            if node_plan.role == "normal" or not node_plan.mutations:
                continue
            self.restore_pristine(node_plan.node_index)
            exempt = {path for path, _, _ in node_plan.mutations}
            exempt_by_node[node_plan.node_index] = exempt
            if self.target == "geth":
                base = self.adapter.build_default_config(
                    self.scheduler.round_id * 100 + node_plan.node_index)
                cfg = round_work / f"node{node_plan.node_index}" / "conf.toml"
                ops = self.adapter.apply_mutations(
                    round_work / f"node{node_plan.node_index}", base,
                    node_plan.mutations, self.catalog, exempt, cfg)
                ops_by_node[node_plan.node_index] = ops
            elif self.target == "fisco":
                net.stop_node(node_plan.node_index)
                ops = self.adapter.apply_mutations(
                    net, node_plan.node_index, node_plan.mutations,
                    self.catalog, exempt)
                ops_by_node[node_plan.node_index] = ops
            elif self.target == "chainmaker":
                net.stop_org(net.orgs[node_plan.node_index])
                ops = self.adapter.apply_mutations(
                    net, node_plan.node_index, node_plan.mutations,
                    self.catalog, exempt)
                ops_by_node[node_plan.node_index] = ops
            elif self.target == "aptos":
                net.stop_node(node_plan.node_index)
                ops = self.adapter.apply_mutations(
                    net, node_plan.node_index, node_plan.mutations,
                    self.catalog, exempt)
                ops_by_node[node_plan.node_index] = ops
        return ops_by_node

    def admission_pass(self, session: NetSession, plan: RoundPlan,
                       ops_by_node: dict[int, list]) -> dict[int, bool]:
        verdicts: dict[int, bool] = {}
        for node_plan in plan.placements:
            if node_plan.role == "normal":
                continue
            net = session.network
            if self.target == "geth":
                admitted = self.adapter.probe_admission(
                    net, node_plan.node_index, node_plan.node_index == 0)
            else:
                admitted = self.adapter.probe_admission(
                    net, node_plan.node_index)
            verdicts[node_plan.node_index] = bool(admitted)
            for op in ops_by_node.get(node_plan.node_index, []):
                item = next(i for i in self.catalog
                            if i.path == op.item_path)
                self.mei.record_admission(item, op.rule, op.new_value,
                                          bool(admitted))
            if admitted:
                self.scheduler.admit_config(
                    f"round{plan.round_id}-node{node_plan.node_index}")
        return verdicts

    def run_seeds(self, plan: RoundPlan, seed_results: list) -> None:
        normal_node = next(i for i in range(self.n_nodes)
                           if i not in set(self.controlled))
        for node_plan in plan.placements:
            if node_plan.role == "normal":
                continue
            seed = self.scheduler.pick_seed(
                plan, node_plan,
                lambda s: self.precondition_ok(s, node_plan))
            if seed is None:
                continue
            target_node = node_plan.node_index \
                if seed.role in ("controlled", "proposer", "engine") \
                else normal_node
            ctx = {"node_index": target_node, "round": plan.round_id}
            try:
                result = self.adapter.submit_seed(
                    self.network, seed, ctx) or {}
            except Exception as exc:  # a seed must never kill the round
                result = {"error": str(exc)}
            result = {**result,
                      "kind": seed.payload.get("kind", ""),
                      "seed_id": seed.seed_id,
                      "node": target_node}
            seed_results.append(result)
            self.scheduler.record_seed_execution(
                plan.placement_hash, seed.seed_id)

    def run_sequences(self, plan: RoundPlan, round_work: Path) -> list[dict]:
        seq_out: list[dict] = []
        controlled0 = self.controlled[0]
        normal_node = next(i for i in range(self.n_nodes)
                           if i not in set(self.controlled))
        try:
            seq_out.append({"seq": "drive_blocks", **drive_blocks(
                self.network, self.adapter, self.target, 30,
                node_index=normal_node)})
        except Exception as exc:
            seq_out.append({"seq": "drive_blocks", "error": str(exc)})
        if plan.round_id % 5 == 0:
            try:
                rounds = 60 if self.target == "geth" else None
                seq_out.append({"seq": "rotate_role", **rotate_role(
                    self.network, self.adapter, self.target,
                    controlled0, normal_node, rounds=rounds)})
            except Exception as exc:
                seq_out.append({"seq": "rotate_role", "error": str(exc)})
        if plan.round_id % 3 == 0:
            try:
                seq_out.append({"seq": "restart_cycle", **restart_cycle(
                    self.network, self.adapter, self.target, controlled0,
                    2)})
            except Exception as exc:
                seq_out.append({"seq": "restart_cycle", "error": str(exc)})
            try:
                seq_out.append({"seq": "concurrent_workload",
                                **concurrent_workload(
                                    self.network, self.adapter, self.target,
                                    normal_node, 8.0)})
            except Exception as exc:
                seq_out.append({"seq": "concurrent_workload",
                                "error": str(exc)})
        if self.target == "geth" and plan.round_id % 4 == 0:
            try:
                seq_out.append({"seq": "submit_pair", **submit_pair(
                    self.network, self.adapter, self.target, normal_node)})
            except Exception as exc:
                seq_out.append({"seq": "submit_pair", "error": str(exc)})
        return seq_out

    def run_round(self, session: NetSession, plan: RoundPlan) -> dict:
        net = session.network
        self.network = net
        t0 = time.monotonic()
        round_work = session.runtime / f"round-{plan.round_id}"
        seed_results: list[dict] = []
        try:
            if self.target == "geth":
                # shared datadirs, per-round mutated configs: stop the
                # baseline/previous round's processes, restart with the
                # round's mutated configs, mesh, then drive
                net.stop_all()
                ops_by_node = {}
                configs: dict[int, Path] = {}
                for node_plan in plan.placements:
                    if node_plan.role == "normal":
                        continue
                    base = self.adapter.build_default_config(
                        plan.round_id * 100 + node_plan.node_index)
                    cfg = round_work / f"node{node_plan.node_index}" / "conf.toml"
                    exempt = {p for p, _, _ in node_plan.mutations}
                    ops = self.adapter.apply_mutations(
                        round_work / f"node{node_plan.node_index}", base,
                        node_plan.mutations, self.catalog, exempt, cfg)
                    ops_by_node[node_plan.node_index] = ops
                    configs[node_plan.node_index] = cfg
                net.start_all(configs, {0}, round_work / "logs")
                verdicts = {}
                for node_plan in plan.placements:
                    if node_plan.role == "normal":
                        continue
                    admitted = self.adapter.probe_admission(
                        net, node_plan.node_index, node_plan.node_index == 0)
                    verdicts[node_plan.node_index] = bool(admitted)
                    for op in ops_by_node.get(node_plan.node_index, []):
                        item = next(i for i in self.catalog
                                    if i.path == op.item_path)
                        self.mei.record_admission(item, op.rule, op.new_value,
                                                  bool(admitted))
                    if admitted:
                        self.scheduler.admit_config(
                            f"round{plan.round_id}-node{node_plan.node_index}")
            else:
                ops_by_node = self.apply_placements(session, plan, round_work)
                verdicts = self.admission_pass(
                    session, plan, ops_by_node)
                # view-change aftershocks of the round's serial restarts
                # must not count as storm signal (see oracle.settle_after_restarts)
                self.oracle.settle_after_restarts(net, self.adapter)
        except Exception:
            traceback.print_exc()
            return {"round_id": plan.round_id, "error": "setup",
                    "elapsed": time.monotonic() - t0}
        self.run_seeds(plan, seed_results)
        sequences = self.run_sequences(plan, round_work)
        if self.target == "geth":
            # post-merge blocks are not p2p-announced: drive the normal
            # nodes to the producer's head every round, or the oracle's
            # normal-view probes (gasLimit collapse #8, progress stalls)
            # keep reading genesis values forever
            try:
                controlled = set(self.controlled)
                heads = {i: net.height(i) for i in controlled}
                source = max(heads, key=heads.get)
                sync = net.sync_all(exclude=controlled, source=source)
                sequences.append({"seq": "sync_normals",
                                  "source": source,
                                  "heads": heads,
                                  "synced": [i for i, ok in sync.items()
                                             if ok and isinstance(i, int)],
                                  "heights_after": sync.get("_heights"),
                                  "converged": sync.get("_converged"),
                                  "sync_error": sync.get("_error")})
            except Exception as exc:
                sequences.append({"seq": "sync_normals",
                                  "error": str(exc)})
        failures = []
        try:
            failures = self.oracle.observe(
                net, self.adapter, plan.round_id,
                seed_results=seed_results, placement=plan)
        except Exception:
            traceback.print_exc()
        reports: list[dict] = []
        for failure in failures:
            report = self.oracle.report(failure, plan.round_id, plan,
                                        [op for ops in ops_by_node.values()
                                         for op in ops])
            if report is not None:
                reports.append(report)
        record = {
            "round_id": plan.round_id,
            "placement_hash": plan.placement_hash,
            "verdicts": verdicts,
            "mutations": {node: [(op.item_path, op.rule, op.new_value)
                                 for op in ops]
                          for node, ops in ops_by_node.items()},
            "seeds": seed_results,
            "sequences": sequences,
            "failures": [{"category": f.category, "signal": f.signal,
                          "node": f.node, "detail": f.detail}
                         for f in failures],
            "mei": self.mei.status_counts(self.target, self.catalog),
            "elapsed": time.monotonic() - t0,
        }
        self.timeline.append(record)
        self.persist(plan.round_id, record)
        if self.target == "geth":
            net.stop_all()
        return record

    # -------------------------------------------------------------- fuzz

    def _warmup_fisco(self, net) -> None:
        """Kick block production before baseline + round-1 restarts.

        A fresh 13-node PBFT net seals no blocks while the pool is empty
        (heights stay 0 for minutes untouched), and serial restarts
        before block 1 can leave the chain stuck at genesis — which is
        also the confounder that made the settle test's bug arm miss its
        storm: a chain that never commits has no consensus activity to
        observe.  Send one wave through a normal node and wait for
        block 1 so the baseline and the first restarts happen on a chain
        that has committed."""
        seed = next(s for s in self.seeds
                    if s.seed_id == "fisco-t-transfer-wave")
        try:
            out = self.adapter.submit_seed(net, seed, {"node_index": 4})
            print(f"[warmup] fisco transfer wave via node4: {out}",
                  flush=True)
        except Exception:
            traceback.print_exc()
            return
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                heights = [net.current_block_number(i)
                           for i in range(4, 13)]
                if max(heights) >= 1:
                    print(f"[warmup] block 1 committed, normal heights="
                          f"{heights}", flush=True)
                    return
            except Exception:
                pass
            time.sleep(3)
        print("[warmup] WARNING: no block 1 after 180 s", flush=True)

    def run_fuzz(self, rounds: int | None, budget_minutes: int | None,
                 round_deadline: float | None) -> dict:
        session = NetSession(self.target, self.out_dir / "runtime", self.n_nodes,
                             self.scheduler.round_id * 1000)
        session.ensure_ready()
        self.network = session.network
        self.snapshot_pristine(session.network)
        if self.target == "fisco":
            self._warmup_fisco(session.network)
        print(f"[baseline] registering idle window on {self.target}...",
              flush=True)
        try:
            self.oracle.register_baseline(session.network, self.adapter)
        except Exception:
            traceback.print_exc()
        deadline = time.monotonic() + budget_minutes * 60 \
            if budget_minutes else None
        count = 0
        last_plan = None
        try:
            while True:
                count += 1
                if rounds is not None and count > rounds:
                    break
                if deadline is not None and time.monotonic() > deadline:
                    break
                t0 = time.monotonic()
                plan = self.scheduler.next_round(self.mei)
                last_plan = plan
                print(f"[round {plan.round_id}] placement="
                      f"{plan.placement_hash[:12]}...", flush=True)
                record = self.run_round(session, plan)
                print(f"[round {plan.round_id}] verdicts="
                      f"{record.get('verdicts')} "
                      f"failures={len(record.get('failures', []))} "
                      f"elapsed={record.get('elapsed', 0):.1f}s", flush=True)
                if round_deadline and time.monotonic() - t0 > round_deadline:
                    print(f"[round {plan.round_id}] exceeded deadline, "
                          "stopping", flush=True)
                    break
        finally:
            # always tear down — a crash mid-round must not leak 13 nodes
            session.teardown()
        return self.finish()

    # ------------------------------------------------------------- results

    def finish(self) -> dict:
        result = {
            "target": self.target,
            "rounds": self.scheduler.round_id,
            "mei_summary": summarize(self.mei, self.catalog),
            "pool_size": self.scheduler.pool_size(),
            "reports": [r for r in self.oracle.reports],
            "timeline": self.timeline,
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)
        save_json(self.out_dir / "result.json", result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True,
                        choices=["geth", "chainmaker", "fisco", "aptos"])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--nodes", type=int, default=13)
    parser.add_argument("--controlled", type=int, default=4,
                        help="k controlled nodes")
    parser.add_argument("--rounds", type=int, default=None)
    parser.add_argument("--budget-minutes", type=int, default=None)
    parser.add_argument("--round-deadline", type=float, default=None,
                        help="stop after a round exceeds this many seconds")
    parser.add_argument("--mode", default="fuzz",
                        choices=["fuzz", "calibrate", "regress"])
    parser.add_argument("--bugs", default="",
                        help="comma-separated bug ids (calibrate/regress)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--state", type=Path, default=None,
                        help="resume campaign state from this directory")
    parser.add_argument("--exploration-rounds", type=int, default=5)
    args = parser.parse_args()

    if args.mode == "calibrate":
        from bcfuzzer.calibration import run_calibration
        bugs = [b.strip() for b in args.bugs.split(",") if b.strip()] or None
        run_calibration(bugs, args.output / "calibration", seed=args.seed)
        return 0
    if args.mode == "regress":
        from bcfuzzer.regression import run_regression
        bugs = [b.strip() for b in args.bugs.split(",") if b.strip()] or None
        run_regression(args.target, bugs, args.output / "regression")
        return 0

    if args.rounds is None and args.budget_minutes is None:
        args.rounds = 10
    shutil.rmtree(args.output, ignore_errors=True)
    args.output.mkdir(parents=True, exist_ok=True)
    campaign = Campaign(args.target, args.output, args.nodes,
                        list(range(args.controlled)), args.seed,
                        args.state,
                        exploration_rounds=args.exploration_rounds)
    try:
        result = campaign.run_fuzz(args.rounds, args.budget_minutes,
                                   args.round_deadline)
        print(json.dumps(
            {"target": args.target, "rounds": result["rounds"],
             "pool_size": result["pool_size"],
             "reports": len(result["reports"])}, indent=2))
        return 0
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
