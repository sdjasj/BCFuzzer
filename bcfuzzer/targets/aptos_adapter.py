"""Campaign adapter: Aptos config prep, admission probe, T/M seeds.

Config edits go through the YAML ConfigEditor against the controlled
validator's node.yaml (PoC #12 pattern: kill validator, mutate node.yaml,
`aptos-node -f` restart).  Sanitization replicates sanitize_mempool_config
minus the exempted dangerous-but-legal keys.

Seeds:
  - T: signed transfer waves against the controlled validator's API
    (live_node submit_transfers) and malformed BCS transaction probes.
  - M: REST-layer variant payloads (malformed_probes) plus transfer waves
    targeted at a validator whose consensus timeout is zero / sync_only is
    on — the liveness-loss configurations of paper bugs #10/#11.
"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import yaml  # noqa: E402

from live_node_aptos import (  # noqa: E402
    MEMPOOL_BOUNDS, MEMPOOL_INPUT, _bounded_int, ledger_version,
    malformed_transaction_probes, read_yaml, submit_transfers)

from ..common import Seed  # noqa: E402

NODE_LOG_SIGNATURES = (
    "panic",
    "Connection refused",
    "quorum",
    "timeout",
    "fatal",
)


class AptosAdapter:
    target = "aptos"

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    # ------------------------------------------------------- config plumbing

    def pristine_files(self, net, index: int) -> list[Path]:
        """node.yaml restored from the campaign-start snapshot before each
        round (see campaign.restore_pristine)."""
        path = net.runtime / f"{index}" / "node.yaml"
        return [path] if path.is_file() else []

    def apply_mutations(self, net, index: int, mutations, catalog,
                        exempt_keys: set[str]):
        """Mutate validator i's node.yaml in place (YAML)."""
        from ..mutator import ConfigEditor, mutate_one

        cfg_dir = net.runtime / f"{index}"
        editor = ConfigEditor(cfg_dir)
        ops = []
        counter = 0
        for item_path, rule, value in mutations:
            item = next((i for i in catalog if i.path == item_path), None)
            if item is None:
                continue
            config = cfg_dir / (item.file or "node.yaml")
            counter += 1
            ops.append(mutate_one(editor, config, item, rule, self.rng,
                                  counter, force_value=value))
        self.sanitize_with_exempt(cfg_dir / "node.yaml", exempt_keys)
        return ops

    def sanitize_with_exempt(self, config: Path, exempt: set[str]) -> int:
        """sanitize_mempool_config minus exempted dangerous keys."""
        data = read_yaml(config)
        mempool = data.setdefault("mempool", {})
        changed = 0
        for key, default in MEMPOOL_INPUT.items():
            if f"mempool.{key}" in exempt:
                continue
            low, high = MEMPOOL_BOUNDS[key]
            new_value = _bounded_int(mempool.get(key, default), low, high, default)
            if mempool.get(key) != new_value:
                mempool[key] = new_value
                changed += 1
        if changed:
            config.write_text(yaml.safe_dump(data, sort_keys=False),
                              encoding="utf-8")
        return changed

    # ----------------------------------------------------------- admission

    def probe_admission(self, net, index: int, timeout: int = 120) -> bool:
        """Mutated config admitted = validator restarts and serves its API
        (the PoC #12 kill-mutate-restart cycle)."""
        net.stop_node(index)
        time.sleep(2)
        return net.start_node(index, timeout=timeout)

    # -------------------------------------------------------------- seeds

    def submit_seed(self, net, seed: Seed, ctx: dict) -> dict:
        kind = seed.payload.get("kind", "transfer_wave")
        index = ctx.get("node_index", 0)
        api = net.api_of(index)
        if kind == "transfer_wave":
            accepted, rejected = asyncio.run(submit_transfers(
                api, net.root_key, seed.payload.get("count", 30),
                seed.payload.get("varied", True)))
            return {"accepted": accepted, "rejected": rejected}
        if kind == "malformed_probes":
            observed = malformed_transaction_probes(api)
            return {"observed_http_errors": observed}
        if kind == "ledger_poll":
            return {"ledger": net.ledger(index)}
        return {"skipped": True}

    # --------------------------------------------------------------- probes

    def node_probes(self, net, index: int) -> dict:
        text = net.log_text(index)
        return {
            "alive": net.alive(index),
            "ledger": net.ledger(index),
            "log_signatures": {sig: text.count(sig)
                               for sig in NODE_LOG_SIGNATURES if sig in text},
        }
