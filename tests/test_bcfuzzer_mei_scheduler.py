"""Stage C smoke tests: MEI classification and two-level scheduling."""

from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "bcfuzzer"))

from bcfuzzer.common import Seed  # noqa: E402
from bcfuzzer.item_catalog import GETH_ITEMS, item_by_path  # noqa: E402
from bcfuzzer.mei import CONSISTENT_THRESHOLD, MeiState, summarize  # noqa: E402
from bcfuzzer.scheduler import (  # noqa: E402
    RoundPlan, TwoLevelScheduler, placement_hash)


def test_mei_classification() -> None:
    mei = MeiState()
    item = item_by_path("geth", "Eth.TxPool.PriceBump")
    for i in range(CONSISTENT_THRESHOLD - 1):
        mei.record_admission(item, "max", 10**9 + i, admitted=False)
    assert mei.status(item) == "unexplored"
    mei.record_admission(item, "max", 10**9 + 99, admitted=False)
    assert mei.status(item) == "consistent"
    assert mei.rejected_count(item) == CONSISTENT_THRESHOLD

    item2 = item_by_path("geth", "Eth.Miner.GasCeil")
    mei.record_admission(item2, "dangerous", 5000, admitted=True)
    assert mei.status(item2) == "inconsistent"
    assert 5000 in [v for _, v in mei.valid_pairs(item2)]

    counts = mei.status_counts("geth", GETH_ITEMS)
    assert counts["consistent"] == 1 and counts["inconsistent"] == 1
    assert "consistent=1" in summarize(mei, GETH_ITEMS)


def test_mei_roundtrip() -> None:
    mei = MeiState()
    item = item_by_path("geth", "Eth.TxPool.GlobalSlots")
    mei.record_admission(item, "min", 1, admitted=True)
    mei.record_admission(item, "scale_10", 20480, admitted=False)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mei.json"
        mei.save(path)
        mei2 = MeiState.load(path)
    assert mei2.status(item) == "inconsistent"
    assert {v for _, v in mei2.valid_pairs(item)} == {1}
    assert mei2.rejected_count(item) == 1


def _plan_from(plan: RoundPlan, index: int):
    return plan.placement_for(index)


def test_scheduler_roles_and_exploration() -> None:
    seeds = [
        Seed(seed_id="t-normal", corpus="T", role="normal"),
        Seed(seed_id="m-controlled", corpus="M", role="controlled"),
    ]
    rng = random.Random(11)
    sched = TwoLevelScheduler("geth", GETH_ITEMS, seeds,
                              n_nodes=13, controlled_indices=[0, 1, 2, 3],
                              rng=rng, exploration_rounds=2)
    mei = MeiState()

    plan1 = sched.next_round(mei)
    assert plan1.round_id == 1
    assert len(plan1.placements) == 13
    assert _plan_from(plan1, 0).role == "exploration"
    assert _plan_from(plan1, 1).role == "fuzzing"
    assert _plan_from(plan1, 12).role == "normal"
    assert _plan_from(plan1, 0).mutations, "exploration node must mutate"
    # exploration mutations must come from a catalog item
    item_path = _plan_from(plan1, 0).metadata["item"]
    assert any(i.path == item_path for i in GETH_ITEMS)

    # least-explored item gets picked in round 2
    plan2 = sched.next_round(mei)
    assert _plan_from(plan2, 0).role == "exploration"
    item2 = _plan_from(plan2, 0).metadata["item"]
    assert item2 != item_path or _plan_from(plan2, 0).mutations

    # from round 3 the first controlled node becomes a fuzzing node
    plan3 = sched.next_round(mei)
    assert _plan_from(plan3, 0).role == "fuzzing"


def test_placement_hash_and_least_tested() -> None:
    seeds = [Seed(seed_id=f"t-{i}", corpus="T", role="normal") for i in range(4)]
    rng = random.Random(3)
    sched = TwoLevelScheduler("geth", GETH_ITEMS, seeds, 13, [0, 1], rng)
    mei = MeiState()
    plan = sched.next_round(mei)
    normal_node = plan.placement_for(5)
    assert placement_hash(plan.placements) == plan.placement_hash
    # least-tested order: t-0 first
    first = sched.pick_seed(plan, normal_node)
    assert first is not None and first.seed_id == "t-0"
    second = sched.pick_seed(plan, normal_node)
    assert second is not None and second.seed_id == "t-1"

    # same assignment -> same hash -> counters shared; different -> fresh
    plan2 = sched.next_round(mei)
    assert plan2.placement_hash == plan.placement_hash


def test_pick_seed_preconditions_and_starvation_bound() -> None:
    seeds = [Seed(seed_id="blocked", corpus="T", role="normal"),
             Seed(seed_id="ok", corpus="T", role="normal")]
    rng = random.Random(5)
    sched = TwoLevelScheduler("geth", GETH_ITEMS, seeds, 13, [0], rng)
    plan = sched.next_round(MeiState())
    node = plan.placement_for(6)

    def always_false(seed: Seed) -> bool:
        return False

    assert sched.pick_seed(plan, node, precondition_ok=always_false) is None

    def allow_ok(seed: Seed) -> bool:
        return seed.seed_id == "ok"

    chosen = sched.pick_seed(plan, node, precondition_ok=allow_ok)
    assert chosen is not None and chosen.seed_id == "ok"


def test_pool_admission() -> None:
    rng = random.Random(8)
    sched = TwoLevelScheduler("geth", GETH_ITEMS, [], 13, [0, 1, 2, 3], rng)
    assert sched.pool_size() == 1
    sched.admit_config("cfg-abc")
    sched.admit_config("cfg-abc")
    assert sched.pool_size() == 2
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scheduler.json"
        sched.save(path)
        loaded = TwoLevelScheduler.load(path, GETH_ITEMS, [], rng, 13)
    assert loaded.pool == ["default", "cfg-abc"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all mei/scheduler tests passed")
