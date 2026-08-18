"""Sequence primitives (design §3.5).

Each primitive is a small target-dispatched interaction script composed of
network + adapter calls.  Sequences are attached to a round's placement as
additional seed-like workload (the campaign records them in the seed
counts, so least-tested scheduling covers them too).

  drive_blocks(n)      advance the chain by n blocks (rapidly where a
                       fast path exists: geth engine_drive, fisco wave,
                       chainmaker cmc invokes, aptos transfers)
  rotate_role          hand the producer/leader/proposer role to another
                       node (geth: beacon handover, PoC phase 2;
                       fisco/chainmaker/aptos: wait for natural rotation
                       via view/round polling)
  restart_cycle(n)     restart a node n times under the current mutated
                       config (admission stress; paper #2/#3 companion)
  concurrent_workload  background transaction thread while a restart
                       cycle runs (overlap service for #2/#3)
  submit_pair          geth: nonce-equal replacement pair (paper #9)
"""

from __future__ import annotations

import threading
import time

from .corpus_t import corpus_t
from .corpus_m import corpus_m


def drive_blocks(net, adapter, target: str, count: int,
                 node_index: int = 1, ctx: dict | None = None) -> dict:
    """Advance the chain by ~count blocks and return observations."""
    ctx = ctx or {"node_index": node_index}
    if target == "geth":
        result = net.engine_drive(0, count)
        return {"blocks": result.get("blocks"),
                "milestones": result.get("milestones", {})}
    if target == "fisco":
        from live_node_fisco import simple_transfer_wave
        adapter.nonce_cache = getattr(adapter, "nonce_cache", {})
        sent = simple_transfer_wave(net.rpc_for(node_index), adapter.accounts,
                                    adapter.nonce_cache, max(4, count))
        return {"sent": sent}
    if target == "chainmaker":
        org = net.orgs[node_index]
        return {"invoked": net.invoke(org, max(4, count))}
    if target == "aptos":
        import asyncio
        from live_node_aptos import submit_transfers
        accepted, rejected = asyncio.run(submit_transfers(
            net.api_of(node_index), net.root_key, max(4, count), True))
        return {"accepted": accepted, "rejected": rejected}
    return {"skipped": True}


def rotate_role(net, adapter, target: str, old_index: int, new_index: int,
                rounds: int | None = None) -> dict:
    """Move the producer/leader role to `new_index` (or wait for rotation)."""
    if target == "geth":
        # PoC geth/01 phase 2: sync the new producer to the old head, then
        # drive it from the old head's timestamp + 1.
        rounds = rounds or 500
        return net.rotate_producer(old_index, new_index, rounds=rounds)
    if target == "chainmaker":
        # TBFT rotates the proposer per round; wait until `new_index`
        # proposes (cmc consensus status polling), bounded by timeout.
        org = net.orgs[new_index]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            proposer = net.current_proposer()
            if proposer == org:
                return {"proposer": org, "waited": True}
            time.sleep(3)
        return {"proposer": net.current_proposer(), "timeout": True}
    if target == "fisco":
        # PBFT view rotation: wait until node new_index holds the view leader
        # slot (view % n == new_index), bounded.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            view = net.pbft_view(new_index)
            if view >= 0 and view % net.n == new_index:
                return {"view": view, "leader": new_index}
            time.sleep(2)
        return {"view": net.pbft_view(new_index), "timeout": True}
    if target == "aptos":
        # aptos rotates by epoch; just wait for ledger growth on new_index
        before = net.ledger(new_index)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if net.ledger(new_index) is not None and \
                    (before is None or net.ledger(new_index) > before + 2):
                return {"ledger": net.ledger(new_index)}
            time.sleep(2)
        return {"ledger": net.ledger(new_index)}
    return {"skipped": True}


def restart_cycle(net, adapter, target: str, index: int, cycles: int,
                  ctx: dict | None = None) -> dict:
    """Restart node `index` `cycles` times under the current config."""
    results = []
    for cycle in range(cycles):
        if target == "geth":
            net.stop_all()  # geth restarts need the full net back up
            ok = net.start_all(net.configs, net.miners,
                               net.work / "logs")
            results.append(ok)
        elif target == "chainmaker":
            org = net.orgs[index]
            net.stop_org(org)
            time.sleep(2)
            results.append(net.start_org(org))
        elif target == "fisco":
            net.stop_node(index)
            results.append(net.start_node(index))
        elif target == "aptos":
            net.stop_node(index)
            time.sleep(2)
            results.append(net.start_node(index))
        time.sleep(1)
    return {"cycles": cycles, "results": results,
            "survived": sum(1 for r in results if r)}


def concurrent_workload(net, adapter, target: str, index: int,
                        duration: float, ctx: dict | None = None) -> dict:
    """Background tx thread for `duration` seconds; returns thread stats."""
    seeds = [s for s in corpus_t(target) if s.seed_id.endswith("-wave")
             or "transfer" in s.seed_id or "invoke" in s.seed_id]
    seed = seeds[0] if seeds else None
    if seed is None:
        return {"skipped": True}
    stop = threading.Event()
    stats = {"submitted": 0}

    def worker() -> None:
        while not stop.is_set():
            try:
                result = adapter.submit_seed(
                    net, seed, {**ctx, "node_index": index})
                stats["submitted"] += int(result.get("sent", 0)
                                          or result.get("accepted", 0)
                                          or result.get("invoked", 0))
            except Exception:
                pass
            time.sleep(0.5)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    time.sleep(duration)
    stop.set()
    thread.join(timeout=5)
    return stats


def submit_pair(net, adapter, target: str, index: int,
                ctx: dict | None = None) -> dict:
    """geth: nonce-equal blob replacement pair (paper #9 shape)."""
    if target != "geth":
        return {"skipped": True}
    seeds = {s.seed_id: s for s in corpus_t(target)}
    pair = seeds.get("geth-t-blob-pair")
    if pair is None:
        return {"skipped": True}
    return adapter.submit_seed(net, pair, {**(ctx or {}),
                                           "node_index": index})


SEQUENCES = {
    "drive_blocks": drive_blocks,
    "rotate_role": rotate_role,
    "restart_cycle": restart_cycle,
    "concurrent_workload": concurrent_workload,
    "submit_pair": submit_pair,
}
