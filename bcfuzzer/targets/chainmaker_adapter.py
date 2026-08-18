"""Campaign adapter: ChainMaker config prep, admission probe, T/M seeds.

Config edits go through the YAML ConfigEditor against the controlled org's
chainmaker.yml.  Sanitization replicates sanitize_chainmaker_config
(generalized from wx-org1 to the controlled org) minus the exempted
dangerous-but-legal keys (txpool.pool_type=batch, net.seeds edits, ...).

Seeds:
  - T: cmc contract invokes (normal tx wave) and consensus/chain-config
    governance queries.
  - M: capability-flag seeds — the controlled org is restarted with
    CM_MALICIOUS_* env vars set (the env-gated malicious switches compiled
    into the capability binary), which is the delivery channel for paper
    bugs #1-#3.  Precondition "proposer" gates placement to rounds where
    the controlled org is the current TBFT proposer.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import yaml  # noqa: E402

from live_node_chainmaker import (  # noqa: E402
    _bounded_int, _bool_value, org_domain, release_name)

from ..common import Seed  # noqa: E402

# signature fragments the oracle greps panic.log for (PoC-verified)
PANIC_SIGNATURES = (
    # the TXCOUNT bug's `index out of range [101]` must be matched BEFORE
    # the generic "index out of range" so it maps to cm-02 not cm-01
    "index out of range [1",
    "index out of range",
    "concurrent map",
    "panic:",
    "fatal error:",
    "nil pointer",
    "unexpected end of JSON",
    "TxCount",
)


class ChainMakerAdapter:
    target = "chainmaker"

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    # ------------------------------------------------------- config plumbing

    def org_of(self, net, index: int) -> str:
        return net.orgs[index]

    def config_dir(self, net, index: int) -> Path:
        org = self.org_of(net, index)
        return (net.runtime / release_name(org) / "config" / org_domain(org))

    def pristine_files(self, net, index: int) -> list[Path]:
        """Config files a round's mutations may edit; the campaign restores
        them from the campaign-start snapshot before every round so
        mutations never accumulate on the live runtime dir."""
        cfg_dir = self.config_dir(net, index)
        return sorted(p for p in cfg_dir.glob("*.y*ml") if p.is_file())

    def apply_mutations(self, net, index: int, mutations, catalog,
                        exempt_keys: set[str]):
        """Mutate the controlled org's chainmaker.yml in place (YAML)."""
        from ..mutator import ConfigEditor, mutate_one

        cfg_dir = self.config_dir(net, index)
        editor = ConfigEditor(cfg_dir)
        ops = []
        counter = 0
        for item_path, rule, value in mutations:
            item = next((i for i in catalog if i.path == item_path), None)
            if item is None:
                continue
            config = cfg_dir / (item.file or "chainmaker.yml")
            counter += 1
            ops.append(mutate_one(editor, config, item, rule, self.rng,
                                  counter, force_value=value))
        self.sanitize_with_exempt(cfg_dir, exempt_keys)
        return ops

    def sanitize_with_exempt(self, cfg_dir: Path, exempt: set[str]) -> int:
        """sanitize_chainmaker_config minus exempted dangerous keys."""
        cfg = cfg_dir / "chainmaker.yml"
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        changed = 0

        txpool = data.setdefault("txpool", {})
        if "txpool.pool_type" not in exempt:
            pool_type = str(txpool.get("pool_type", "normal")).strip().strip('"').strip("'")
            if pool_type not in {"normal", "batch"}:
                pool_type = "normal"
            if txpool.get("pool_type") != pool_type:
                txpool["pool_type"] = pool_type
                changed += 1

        tx_bounds = {
            "max_txpool_size": (1, 100_000, 2048),
            "max_config_txpool_size": (1, 10_000, 10),
            "common_queue_num": (1, 256, 8),
            "batch_max_size": (1, 10_000, 50),
            "batch_create_timeout": (1, 600_000, 50),
        }
        for key, (low, high, default) in tx_bounds.items():
            if f"txpool.{key}" in exempt:
                continue
            new_value = _bounded_int(txpool.get(key, default), low, high, default)
            if txpool.get(key) != new_value:
                txpool[key] = new_value
                changed += 1
        if "txpool.is_dump_txs_in_queue" not in exempt:
            new_dump = _bool_value(txpool.get("is_dump_txs_in_queue", True), True)
            if txpool.get("is_dump_txs_in_queue") != new_dump:
                txpool["is_dump_txs_in_queue"] = new_dump
                changed += 1

        ratelimit = data.setdefault("rpc", {}).setdefault("ratelimit", {})
        if "rpc.ratelimit.enabled" not in exempt:
            new_enabled = _bool_value(ratelimit.get("enabled", False), False)
            if ratelimit.get("enabled") != new_enabled:
                ratelimit["enabled"] = new_enabled
                changed += 1
        rate_bounds = {
            "type": (0, 1, 0),
            "token_per_second": (-1, 1_000_000, -1),
            "token_bucket_size": (-1, 1_000_000, -1),
        }
        for key, (low, high, default) in rate_bounds.items():
            if f"rpc.ratelimit.{key}" in exempt:
                continue
            new_value = _bounded_int(ratelimit.get(key, default), low, high, default)
            if ratelimit.get(key) != new_value:
                ratelimit[key] = new_value
                changed += 1

        if changed:
            cfg.write_text(yaml.safe_dump(data, sort_keys=False),
                           encoding="utf-8")
        return changed

    # ----------------------------------------------------------- admission

    def probe_admission(self, net, index: int, timeout: int = 90) -> bool:
        """Mutated config admitted = controlled org restarts and serves RPC."""
        org = self.org_of(net, index)
        if not net.start_org(org):
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            height = net.height(org)
            if height >= 0:
                return True
            time.sleep(2)
        return False

    # -------------------------------------------------------------- seeds

    def submit_seed(self, net, seed: Seed, ctx: dict) -> dict:
        kind = seed.payload.get("kind", "invoke_wave")
        org = self.org_of(net, ctx.get("node_index", 0))
        if kind == "invoke_wave":
            count = seed.payload.get("count", 30)
            accepted = net.invoke(org, count)
            return {"sent": accepted}
        if kind == "malicious":
            # M-corpus: capability flags via restart-with-env.  The
            # scheduler's precondition check guarantees the org is the
            # current proposer when seed.preconditions requires it.
            flags = {k: str(v) for k, v in seed.payload.get("flags", {}).items()}
            if not flags:
                return {"skipped": True}
            ok = net.restart_org_with_env(org, flags)
            return {"restarted": ok, "flags": flags}
        if kind == "gov_query":
            ok, out = net.cmc_capture_org(org, "consensus", "status")
            return {"ok": ok, "output": out[-500:]}
        if kind == "net_seeds_race":
            # BCB #2: rewrite the controlled org's net.seeds (reorder/
            # subset/duplicate — all legal) and restart it, forcing normal
            # peers to re-verify connections.  The race fires when
            # concurrent_workload overlaps (see sequences.py).  May not
            # panic on v3.0.0 (RWMutex-hardened).
            import yaml as _yaml
            cfg = net.config_dir(net, ctx.get("node_index", 0)) / "chainmaker.yml"
            try:
                data = _yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
                seeds = (data.get("net") or {}).get("seeds") or []
                if seeds:
                    data.setdefault("net", {})["seeds"] = seeds[1:] + seeds[:1]
                    cfg.write_text(_yaml.dump(data, sort_keys=False),
                                   encoding="utf-8")
                ok = net.restart_org_with_env(org, {})
                return {"restarted": ok, "rewrote": "net.seeds"}
            except Exception as exc:  # noqa: BLE001
                return {"restarted": False, "error": str(exc)}
        if kind == "cert_logger_race":
            # BCB #3: rotate the controlled org's signing cert and restart
            # (rejoin), racing the logger level-map access on normal peers.
            import shutil as _sh
            cfg_dir = self.config_dir(net, ctx.get("node_index", 0))
            certdir = cfg_dir / "certs" / "node"
            try:
                key = certdir / "node.sign.key"
                if key.is_file():
                    _sh.copy2(str(key), str(key) + ".bak")
                ok = net.restart_org_with_env(org, {})
                return {"restarted": ok, "rotated": "signing cert"}
            except Exception as exc:  # noqa: BLE001
                return {"restarted": False, "error": str(exc)}
        return {"skipped": True}

    # --------------------------------------------------------------- probes

    def node_probes(self, net, index: int) -> dict:
        org = self.org_of(net, index)
        panic = net.panic_log(org)
        system = net.system_log(org)
        sigs = {}
        for sig in PANIC_SIGNATURES:
            if sig in panic:
                # a generic signature (e.g. "index out of range" → cm-01)
                # is suppressed if a more-specific signature already matched
                # whose text starts with this one ("index out of range [1"
                # for the TXCOUNT bug → cm-02), so one panic yields one signal
                if any(already.startswith(sig) for already in sigs):
                    continue
                sigs[sig] = panic.count(sig)
        return {
            "alive": net.alive(org),
            "height": net.height(org),
            "panic_signatures": sigs,
            "round_advances": system.count("attempt enterNewRound"),
            "propose_timeouts": system.count("propose timeout"),
        }
