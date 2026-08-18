"""Campaign adapter: geth config prep, admission probe, seed execution.

Reuses live_node_geth's config generation (targets.apply_case) and workload
helpers (send_*/build_blob_raw_tx) while adding the two pieces the fuzzing
engine needs on top of the coverage runner:

  - sanitize-with-exemption: the stock sanitize_geth_baseline_config clamps
    every numeric key to its live-admissible bounds, which would silently
    erase the paper's dangerous-but-legal trigger values (GasCeil 5000,
    BlobPool.PriceBump 1000000).  The exemption set (from the mutation ops)
    keeps exactly those keys untouched.
  - admission probe: RPC up + (for producers) a short engine_drive proving
    the node can actually seal blocks with the mutated config.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from live_node_geth import (  # noqa: E402
    GETH_NUMERIC_BOUNDS, ROOT, build_blob_raw_tx, peer_count, rpc_call,
    send_blob_replacements, send_blob_txs, send_data_txs, send_raw_transaction,
    send_replacement_txs, send_txs)
from targets import apply_case  # noqa: E402

from ..common import MutationOp, Seed  # noqa: E402
from ..mutator import ConfigEditor, mutate_one  # noqa: E402

DEFAULT_CASE = "miner-txpool-balanced"
BLOB_CASE = "blobpool-constrained"


def send_raw_capture(url: str, raw_hex: str) -> tuple[bool, str]:
    """eth_sendRawTransaction with the response body parsed.

    live_node_geth.send_raw_transaction returns True on any HTTP 200, but
    geth reports pool rejections as a JSON-RPC error INSIDE the 200 body
    ("replacement transaction underpriced: ..."), so the stock helper
    cannot distinguish accept from reject.  Returns (accepted, errmsg).
    """
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps({"jsonrpc": "2.0",
                              "method": "eth_sendRawTransaction",
                              "params": [raw_hex], "id": 1}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = json.loads(r.read())
    except Exception as exc:  # connection-level failure
        return False, f"http error: {exc}"
    if "result" in body:
        return True, ""
    err = body.get("error") or {}
    return False, err.get("message", str(body))


def blob_tx_hash(raw_hex: str) -> str | None:
    """Canonical type-3 tx hash: keccak(0x03 || rlp(inner)).

    The v1 sidecar fields (blobs/commitments/cell_proofs) are NOT part of
    the tx hash, so hashing the full v1 raw never matches the mined tx —
    receipts looked up by that hash are always null."""
    import rlp
    from eth_utils import keccak
    try:
        data = bytes.fromhex(raw_hex[2:])
        if data[0] != 0x03:
            return None
        parts = rlp.decode(data[1:])
        inner = parts[0]
        return "0x" + keccak(bytes([0x03]) + rlp.encode(inner)).hex()
    except (ValueError, TypeError):
        return None


class GethAdapter:
    target = "geth"

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self._op_counter = 0

    # ------------------------------------------------------- config plumbing

    def pristine_files(self, net, index: int) -> list[Path]:
        # geth rebuilds a fresh base config per round (campaign.run_round),
        # so the runtime configs never accumulate mutations
        return []

    def build_default_config(self, seed: int,
                             case: str = DEFAULT_CASE) -> Path:
        run_dir = Path("/tmp") / f"bcfuzzer-geth-{seed}-{time.time_ns()}"
        run_dir.mkdir(parents=True, exist_ok=True)
        meta = apply_case("geth", ROOT, case, run_dir, seed)
        config = Path(meta["output"])
        # the upstream template caps peers at 8; the sync star needs the
        # source to serve 12 inbound fetches (stageG3 geth leg: normals
        # could not all fetch the chain once the source stopped being the
        # mesh star center)
        text = config.read_text(encoding="utf-8")
        if "MaxPeers = 8" in text:
            text = text.replace("MaxPeers = 8", "MaxPeers = 32", 1)
            config.write_text(text, encoding="utf-8")
        return config

    def apply_mutations(self, node_dir: Path, base_config: Path,
                        mutations: list[tuple[str, str, object]],
                        catalog: list, exempt_keys: set[str],
                        target_path: Path) -> list[MutationOp]:
        """Copy the base config and apply scheduler-chosen (item, rule, value)."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            import shutil
            shutil.copy2(base_config, target_path)
        editor = ConfigEditor(node_dir)
        ops: list[MutationOp] = []
        for item_path, rule, value in mutations:
            item = next((i for i in catalog if i.path == item_path), None)
            if item is None:
                continue
            self._op_counter += 1
            ops.append(mutate_one(editor, target_path, item, rule, self.rng,
                                  self._op_counter, force_value=value))
        self._op_counter += 1
        self.sanitize_with_exempt(target_path, exempt_keys)
        return ops

    def sanitize_with_exempt(self, config: Path, exempt: set[str]) -> int:
        """Stock sanitizer minus the dangerous-but-legal trigger keys."""
        from config_mutators import parse_lines
        from live_node_geth import _bounded_int

        text = config.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        changed = 0
        for entry in parse_lines(text):
            key = (entry["section"], entry["key"])
            if key in GETH_NUMERIC_BOUNDS:
                if entry["section"] + "." + entry["key"] in exempt:
                    continue
                low, high, default = GETH_NUMERIC_BOUNDS[key]
                new_value = _bounded_int(entry["value"], low, high, default)
            elif key == ("Eth.BlobPool", "Datadir"):
                raw = entry["value"].strip()
                new_value = (raw if raw.startswith('"') and raw.endswith('"')
                             else '"blobpool"')
            else:
                continue
            new_line = f"{entry['indent']}{entry['key']} {entry['sep']} {new_value}"
            if entry["line"] < len(lines) and lines[entry["line"]] != new_line:
                lines[entry["line"]] = new_line
                changed += 1
        if changed:
            config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return changed

    # ----------------------------------------------------------- admission

    def probe_admission(self, net, index: int, is_producer: bool) -> bool:
        """A mutated config is admitted only if the node lives and (for the
        producer role) actually seals blocks."""
        if not net.alive(index):
            return False
        # 10 s was too tight on a box starting 13 nodes at once: healthy
        # mutated nodes (PriceBump 1e6 etc.) were marked invalid, which
        # poisons the MEI for exactly the values the bug triggers need.
        # The 13-node mesh also forms lazily (smoke: verdicts were False
        # for nodes that later meshed and followed the chain), so poll
        # for the first peer instead of taking one early sample.
        if not net.wait_http(index, timeout=30):
            return False
        if is_producer:
            result = net.engine_drive(index, 5)
            return result.get("blocks") == 5
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if net.peer_count(index) >= 1:
                return True
            time.sleep(3)
        return False

    # -------------------------------------------------------------- seeds

    def submit_seed(self, net, seed: Seed, ctx: dict) -> dict:
        """Execute one T-corpus seed against a normal node's RPC."""
        url = net.rpc_url(ctx.get("node_index", 1))
        payload = seed.payload
        kind = payload.get("kind", "simple")
        out: dict = {}
        if kind == "simple":
            out["sent"] = send_txs(payload.get("count", 40), url=url)
        elif kind == "replacement":
            nonce = payload.get("nonce", 0)
            out["accepted"] = send_replacement_txs(nonce, url=url)
        elif kind == "data":
            out["accepted"] = send_data_txs(
                payload.get("nonce", 0), payload.get("count", 6),
                payload.get("data_size", 1024), url=url)
        elif kind == "blob":
            out["accepted"] = send_blob_txs(
                payload.get("nonce", 0), payload.get("count", 4), url=url)
        elif kind == "blob_replacement":
            out["accepted"] = send_blob_replacements(
                payload.get("nonce", 0), url=url)
        elif kind == "blob_pair":
            # paper #9: two blob txs, same nonce, fee x2.5 (PoC geth/02 shape).
            # Sent to the CONTROLLED node (blobpool.pricebump=1000000): its
            # pool accepts the old tx and rejects the replacement, so the
            # accepted counter is the bug's admission signal — the pair must
            # reach the mutated pool, not a normal node's default one.
            nonce = payload.get("nonce", 0)
            first = build_blob_raw_tx(nonce, 20_000_000_000, 20_000_000_000,
                                      1_000_000_000, payload.get("recipient",
                                                                "0x71562b71999873db5b286df9577581998cbf4e81"),
                                      0x40)
            second = build_blob_raw_tx(nonce, 50_000_000_000, 50_000_000_000,
                                       2_500_000_000, payload.get("recipient",
                                                                  "0x71562b71999873db5b286df9577581998cbf4e81"),
                                       0x41)
            accepted = 0
            raw_errors: dict[str, str] = {}
            for label, raw in (("first", first), ("second", second)):
                if raw:
                    ok_send, err = send_raw_capture(url, raw)
                    if ok_send:
                        accepted += 1
                    else:
                        raw_errors[label] = err
                time.sleep(0.4)
            first_hash = blob_tx_hash(first) if first else None
            out = {"accepted": accepted, "pair": True,
                   "errors": raw_errors,
                   "first_error": raw_errors.get("first", ""),
                   "second_error": raw_errors.get("second", ""),
                   "first_hash": first_hash,
                   "node": ctx.get("node_index", 0)}
        elif kind == "beacon_drive":
            # M-corpus: fake-beacon drive variants on the controlled node
            out["driven"] = net.drive_beacon(
                ctx.get("node_index", 0), payload.get("mode", "update"),
                payload.get("rounds", 3), period=payload.get("period", 1))
        elif kind == "engine_rapid":
            # M-corpus: rapid engine payload stress on the controlled producer
            result = net.engine_drive(
                ctx.get("node_index", 0), payload.get("blocks", 50),
                period=payload.get("period", 0.15))
            out = {"blocks": result.get("blocks"),
                   "milestones": result.get("milestones", {})}
        else:
            out = {"skipped": True}
        return out

    # --------------------------------------------------------------- probes

    def node_probes(self, net, index: int) -> dict:
        """Per-node observations feeding the oracle (read-only, normal view)."""
        return {
            "alive": net.alive(index),
            "height": net.height(index),
            "gaslimit": net.gaslimit(index),
            "peers": peer_count(net.rpc_url(index)),
        }

    def rpc_query(self, net, index: int, method: str,
                  params: list | None = None):
        return rpc_call(net.rpc_url(index), method, params)
