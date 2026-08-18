"""Two-level type-aware configuration scheduler (design §3.2).

Outer level — role assignment and the config file pool:
  - `k` controlled nodes: node 0 plays the exploration role during the first
    `exploration_rounds` rounds (mutating the least-explored items to probe
    admission), the other k-1 nodes play the fuzzing role; the remaining
    nodes stay on the default configuration (normal role).
  - A mutated config that passes admission joins the pool; fuzzing rounds
    draw a base config from the pool and re-apply one inconsistent item.

Inner level — workload placement:
  - placement_hash = sha256 over the ordered (node, config) assignment, so
    identical placements share a test counter.
  - Each seed's execution count is tracked per (placement_hash, seed_id);
    the least-tested seed whose preconditions hold is chosen (max_scan=8 to
    avoid starvation).

The scheduler is pure: it proposes (item, rule, value) edits and seed ids;
the campaign materializes them on disk and feeds admission verdicts back
into the MEI (the only feedback path — design's feedback separation).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .common import ItemSpec, Seed, load_json, save_json, stable_hash
from .mei import MeiState
from .mutator import RULES_FOR_KIND, generate_value


@dataclass
class NodePlan:
    node_index: int
    role: str                     # exploration | fuzzing | normal
    config_id: str                # base config from the pool ("default" = stock)
    mutations: list[tuple[str, str, Any]] = field(default_factory=list)
    seed_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoundPlan:
    round_id: int
    placements: list[NodePlan]
    placement_hash: str = ""

    def placement_for(self, node_index: int) -> NodePlan:
        for plan in self.placements:
            if plan.node_index == node_index:
                return plan
        raise KeyError(node_index)

    @property
    def metadata(self) -> dict:
        """Placement summary attached to BugReport observations."""
        return {"placements": [
            {"node": p.node_index, "role": p.role,
             "config_id": p.config_id,
             "mutations": [{"item": item, "rule": rule, "value": value}
                           for item, rule, value in p.mutations]}
            for p in self.placements]}


def placement_hash(plans: list[NodePlan]) -> str:
    assignment = ";".join(f"{p.node_index}={p.config_id}" for p in sorted(
        plans, key=lambda p: p.node_index))
    return stable_hash(assignment)


class TwoLevelScheduler:
    def __init__(self, target: str, catalog: list[ItemSpec], seeds: list[Seed],
                 n_nodes: int, controlled_indices: list[int],
                 rng: random.Random, exploration_rounds: int = 5) -> None:
        self.target = target
        self.catalog = catalog
        self.seeds = seeds
        self.n_nodes = n_nodes
        self.controlled = controlled_indices
        self.k = len(controlled_indices)
        self.rng = rng
        self.exploration_rounds = exploration_rounds
        self.round_id = 0
        self.pool: list[str] = ["default"]
        self.counts: dict[str, dict[str, int]] = {}
        self._last_plan: RoundPlan | None = None

    # ------------------------------------------------------------------ pool

    def admit_config(self, config_id: str) -> None:
        if config_id not in self.pool:
            self.pool.append(config_id)

    def pool_size(self) -> int:
        return len(self.pool)

    # ------------------------------------------------------------- mutations

    def _unexplored_candidates(self, item: ItemSpec,
                               mei: MeiState) -> list[tuple[str, Any]]:
        """(rule, value) pairs for this item never tried before."""
        out: list[tuple[str, Any]] = []
        for rule in RULES_FOR_KIND[item.kind]:
            value = generate_value(item, rule, item.default, self.rng)
            if not mei.is_explored(item, rule, value):
                out.append((rule, value))
        if item.dangerous_legal:
            for value in item.dangerous_legal:
                if not mei.is_explored(item, "dangerous", value):
                    out.append(("dangerous", value))
        return out

    def _p_unexplored(self, item: ItemSpec, mei: MeiState) -> float:
        """P_unexplored(i) = 1 / (|V_i| + 1) with |V_i| = explored values."""
        vi = len(mei.explored.get(item.path, set()))
        return 1.0 / (vi + 1.0)

    def _pick_exploration_item(self, mei: MeiState) -> ItemSpec:
        """Least-explored item first (deterministic tie-break by path)."""
        def key(item: ItemSpec) -> tuple[int, str]:
            return (len(mei.explored.get(item.path, set())), item.path)
        return min(self.catalog, key=key)

    def _pick_fuzzing_item(self, mei: MeiState) -> ItemSpec:
        """Fuzzing-phase item choice over the FULL catalog.

        Fix for the observed starvation: the old code drew only from items
        that had become inconsistent, so the first item with one admission
        hogged every round while the rest of the catalog stayed untouched
        (chainmaker leg: 115 rounds, 3 of ~30 items mutated).  Now 25% of
        picks inject breadth by taking the least-explored item instead."""
        inconsistent = [i for i in self.catalog
                        if mei.status(i) == "inconsistent"]
        if self.rng.random() < 0.25 or not inconsistent:
            return self._pick_exploration_item(mei)
        return self.rng.choice(inconsistent)

    # -------------------------------------------------------------- round plan

    def next_round(self, mei: MeiState) -> RoundPlan:
        self.round_id += 1
        exploring = self.round_id <= self.exploration_rounds
        plans: list[NodePlan] = []
        controlled_set = set(self.controlled)
        for index in range(self.n_nodes):
            if index not in controlled_set:
                plans.append(NodePlan(node_index=index, role="normal",
                                      config_id="default"))
                continue
            role = "exploration" if (exploring and index == self.controlled[0]) else "fuzzing"
            plan = NodePlan(node_index=index, role=role, config_id="default")
            if role == "exploration":
                item = self._pick_exploration_item(mei)
                candidates = self._unexplored_candidates(item, mei)
                if candidates:
                    rule, value = self.rng.choice(candidates)
                    plan.mutations = [(item.path, rule, value)]
                plan.metadata["item"] = item.path
            else:
                # fuzzing: reuse a pool config; re-mutate one item.  With
                # P_unexplored(i) pick a never-tried (rule, value) for the
                # chosen item; otherwise replay an ADMITTED (rule, value)
                # pair — the old code re-wrapped the admitted value under
                # rule="dangerous", which for list items is a no-op edit
                # that still poisoned the MEI with spurious invalids.
                plan.config_id = self.rng.choice(self.pool)
                item = self._pick_fuzzing_item(mei)
                if self.rng.random() < self._p_unexplored(item, mei):
                    candidates = self._unexplored_candidates(item, mei)
                    if candidates:
                        rule, value = self.rng.choice(candidates)
                        plan.mutations = [(item.path, rule, value)]
                if not plan.mutations:
                    pairs = mei.valid_pairs(item)
                    if pairs:
                        rule, value = self.rng.choice(pairs)
                        plan.mutations = [(item.path, rule, value)]
                plan.metadata["item"] = item.path
            plans.append(plan)
        plan = RoundPlan(round_id=self.round_id, placements=plans)
        plan.placement_hash = placement_hash(plans)
        self._last_plan = plan
        return plan

    # ------------------------------------------------------- workload placement

    def pick_seed(self, plan: RoundPlan, node: NodePlan,
                  precondition_ok: Callable[[Seed], bool] | None = None) -> Seed | None:
        """Least-tested seed for this placement hash; max_scan=8 starvation bound."""
        counts = self.counts.setdefault(plan.placement_hash, {})
        node_kind = "controlled" if node.role in ("exploration", "fuzzing") else "normal"
        eligible = [s for s in self.seeds
                    if s.role == "normal"
                    or (node_kind == "controlled"
                        and s.role in ("controlled", "proposer", "engine"))]
        if not eligible:
            return None
        eligible.sort(key=lambda s: (counts.get(s.seed_id, 0), s.seed_id))
        scan = 0
        for seed in eligible:
            scan += 1
            if scan > 8:  # starvation bound: stop scanning blocked seeds
                break
            if precondition_ok is not None and not precondition_ok(seed):
                continue
            counts[seed.seed_id] = counts.get(seed.seed_id, 0) + 1
            return seed
        return None

    def record_seed_execution(self, placement_hash: str, seed_id: str) -> None:
        counts = self.counts.setdefault(placement_hash, {})
        counts[seed_id] = counts.get(seed_id, 0) + 1

    # ------------------------------------------------------------- persistence

    def save(self, path: Path) -> None:
        save_json(path, {
            "target": self.target,
            "round_id": self.round_id,
            "pool": self.pool,
            "counts": self.counts,
            "controlled": self.controlled,
        })

    @classmethod
    def load(cls, path: Path, catalog: list[ItemSpec], seeds: list[Seed],
             rng: random.Random, n_nodes: int) -> "TwoLevelScheduler":
        data = load_json(path)
        if not data:
            raise FileNotFoundError(path)
        sched = cls(data["target"], catalog, seeds, n_nodes,
                    data["controlled"], rng)
        sched.round_id = data.get("round_id", 0)
        sched.pool = data.get("pool", ["default"])
        sched.counts = data.get("counts", {})
        return sched
