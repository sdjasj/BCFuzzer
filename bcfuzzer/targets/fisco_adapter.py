"""Campaign adapter: FISCO config prep, admission probe, T/M seed execution.

Config edits go through the INI ConfigEditor (config.ini / config.genesis),
which can also insert the paper's trigger keys that do not exist in the
stock config (experimental.check_transaction_signature,
[chain] block_limit).  Sanitization replicates sanitize_fisco_baseline_config
minus the exempted dangerous-but-legal keys.

Seeds:
  - T: signed transfer wave (normal node), expired block_limit=0 tx
    (controlled node, paper #6), and the 65-zero-byte bad-signature tx
    (controlled node, paper #5 — exactly the PoC construction).
  - M: Tars-layer variants submitted straight to the controlled node's
    RPC, each structurally valid so it passes the chain-id gate:
    unsigned (reaches the signature-verify path, fs-05 decides admission),
    2 MiB input payload (decode+verify stress), and truncated buffer
    (Tars decoder bounds probe).
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from live_node_fisco import (  # noqa: E402
    FISCO_BOOL_DEFAULTS, FISCO_INT_BOUNDS, GROUP_ID, TarsWriter,
    build_transaction_data, calc_tx_hash, rpc_call)
from eth_account import Account  # noqa: E402

from ..common import Seed  # noqa: E402


def build_bad_signature_tx(priv_key: bytes, *, to: str,
                           block_limit: int = 1500) -> bytes:
    """PoC bug_check_transaction_signature construction: 65 zero-byte sig."""
    nonce = ("0x" + Account.create().address[2:16] +
             str(int(time.time() * 1000) % 100000))
    tx_hash = calc_tx_hash(to=to, block_limit=block_limit, nonce=nonce)
    writer = TarsWriter()
    writer.head(1, TarsWriter.STRUCT_BEGIN)
    writer.buf += build_transaction_data(
        to=to, block_limit=block_limit, nonce=nonce)
    writer.write_bytes(2, tx_hash)
    writer.write_bytes(3, b"\x00" * 65)
    writer.write_byte(9, 0)
    writer.end_struct()
    return bytes(writer.buf)


class FiscoAdapter:
    target = "fisco"

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.accounts = [Account.create() for _ in range(4)]
        self.nonce_cache: dict[str, int] = {}

    # ------------------------------------------------------- config plumbing

    def pristine_files(self, net, index: int) -> list[Path]:
        """Config files restored from the campaign-start snapshot before
        each round (see campaign.restore_pristine)."""
        node_dir = net.node_dir(index)
        return [node_dir / name for name in ("config.ini", "config.genesis")
                if (node_dir / name).is_file()]

    def apply_mutations(self, net, index: int, mutations, catalog,
                        exempt_keys: set[str]):
        """Mutate node i's config.ini/config.genesis in place (INI editor)."""
        from ..mutator import ConfigEditor, mutate_one

        node_dir = net.node_dir(index)
        editor = ConfigEditor(node_dir)
        ops = []
        counter = 0
        for item_path, rule, value in mutations:
            item = next((i for i in catalog if i.path == item_path), None)
            if item is None:
                continue
            config = node_dir / (item.file or "config.ini")
            counter += 1
            ops.append(mutate_one(editor, config, item, rule, self.rng,
                                  counter, force_value=value))
        self.sanitize_with_exempt(node_dir, exempt_keys)
        return ops

    def sanitize_with_exempt(self, node_dir: Path, exempt: set[str]) -> int:
        from config_mutators import parse_lines
        from live_node_fisco import _bounded_int, _bool_value

        changed = 0
        for name in ("config.ini", "config.genesis"):
            config = node_dir / name
            if not config.is_file():
                continue
            text = config.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            for entry in parse_lines(text):
                key = (entry["section"], entry["key"])
                if entry["section"] + "." + entry["key"] in exempt:
                    continue
                if key in FISCO_INT_BOUNDS:
                    low, high, default = FISCO_INT_BOUNDS[key]
                    new_value = _bounded_int(entry["value"], low, high, default)
                elif key in FISCO_BOOL_DEFAULTS:
                    new_value = _bool_value(entry["value"],
                                            FISCO_BOOL_DEFAULTS[key])
                else:
                    continue
                new_line = (f"{entry['indent']}{entry['key']} "
                            f"{entry['sep']} {new_value}")
                if (entry["line"] < len(lines)
                        and lines[entry["line"]] != new_line):
                    lines[entry["line"]] = new_line
                    changed += 1
            if changed:
                config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return changed

    # ----------------------------------------------------------- admission

    def probe_admission(self, net, index: int) -> bool:
        """Mutated config admitted = node restarts and rejoins consensus.

        30 s was too tight: a restarted PBFT node re-reaches a new view in
        10-45 s (measured on the 13-node network), and a node that is
        healthy but slow was marked invalid — which poisons the MEI for
        exactly the values the bug triggers need."""
        return net.start_node(index, timeout=90)

    # -------------------------------------------------------------- seeds

    def submit_seed(self, net, seed: Seed, ctx: dict) -> dict:
        kind = seed.payload.get("kind", "transfer_wave")
        rpc = net.rpc_for(ctx.get("node_index", 0))
        if kind == "transfer_wave":
            from live_node_fisco import simple_transfer_wave
            sent = simple_transfer_wave(
                rpc, self.accounts, self.nonce_cache,
                seed.payload.get("count", 30))
            return {"sent": sent}
        if kind == "expired_tx":
            # block_limit=0: rejected by default nodes, accepted by a
            # controlled node with check_block_limit=false (paper #6).
            # PoC fs-03 construction: a fresh private key per tx.  The
            # RPC response is NOT the signal — once the tx is pooled and
            # the network cannot agree on the block carrying it, the
            # sync send hangs and rpc_call reports a 5 s timeout
            # ("RPC 同步等待挂起" = accepted).
            from eth_keys import keys
            submitted = 0
            timeout_hangs = 0
            errors: list[str] = []
            for _ in range(seed.payload.get("count", 2)):
                priv = keys.PrivateKey(Account.create().key)
                to = priv.public_key.to_checksum_address()
                nonce = ("0x" + Account.create().address[2:16] +
                         str(int(time.time() * 1000) % 100000))
                tx_hash = calc_tx_hash(to=to, block_limit=0, nonce=nonce)
                signature = priv.sign_msg_hash(tx_hash)
                recid = signature.v if signature.v <= 3 else signature.v - 27
                sig_bytes = (signature.r.to_bytes(32, "big") +
                             signature.s.to_bytes(32, "big") +
                             bytes([recid]))
                writer = TarsWriter()
                writer.head(1, TarsWriter.STRUCT_BEGIN)
                writer.buf += build_transaction_data(
                    to=to, block_limit=0, nonce=nonce)
                writer.write_bytes(2, tx_hash)
                writer.write_bytes(3, sig_bytes)
                writer.write_byte(9, 0)
                writer.end_struct()
                response = rpc_call(rpc, "sendTransaction",
                                    [GROUP_ID, "",
                                     "0x" + bytes(writer.buf).hex(), False])
                if "error" not in response:
                    submitted += 1
                elif "timed out" in str(response.get("error", "")):
                    timeout_hangs += 1
                else:
                    errors.append(str(response.get("error"))[:120])
            return {"kind": "expired_tx", "submitted": submitted,
                    "timeout_hangs": timeout_hangs, "errors": errors,
                    "node": ctx.get("node_index", 0)}
        if kind == "bad_signature":
            priv = bytes(self.accounts[1].key)
            accepted = 0
            for _ in range(seed.payload.get("count", 2)):
                raw = build_bad_signature_tx(priv, to=self.accounts[1].address)
                response = rpc_call(rpc, "sendTransaction",
                                    [GROUP_ID, "", "0x" + raw.hex(), False])
                if "error" not in response:
                    accepted += 1
                time.sleep(0.2)
            return {"kind": "bad_signature", "accepted": accepted}
        if kind in ("tars_empty", "tars_oversized", "tars_truncated"):
            return self._tars_variant(net, seed, kind, rpc)
        return {"skipped": True}

    def _tars_variant(self, net, seed: Seed, kind: str, rpc: str) -> dict:
        """Malformed-input variants built on a STRUCTURALLY VALID base.

        The pre-fix versions sent bare/oversized buffers without a chain_id,
        which the node's RPC gate rejects as "Chain ID mismatch!" before any
        deeper layer runs — the M corpus never reached the txpool (stageG3
        fisco leg: 3/3 rejected every round at the RPC boundary).  Carrying
        the real chain_id/group_id gets each variant past the gate:
          tars_empty     -> data struct valid, NO signature: the txpool
                            verify path decides (with fs-05's
                            check_transaction_signature=False armed on the
                            controlled node it is admitted to the pool)
          tars_oversized -> valid tx with a 2 MiB input payload: decode +
                            verify stress on the oversized-input path
          tars_truncated -> valid tx buffer cut in half: the Tars decoder's
                            bounds handling (peekBuf overflow) is the probe
        """
        to = self.accounts[0].address
        nonce = ("0x" + Account.create().address[2:16] +
                 str(int(time.time() * 1000) % 100000))
        writer = TarsWriter()
        writer.head(1, TarsWriter.STRUCT_BEGIN)
        if kind == "tars_empty":
            writer.buf += build_transaction_data(to=to, nonce=nonce)
            writer.write_byte(9, 0)
            writer.end_struct()
            raw = bytes(writer.buf)
        elif kind == "tars_oversized":
            writer.buf += build_transaction_data(
                to=to, nonce=nonce, input_data=b"\xab" * (2 * 1024 * 1024))
            writer.write_byte(9, 0)
            writer.end_struct()
            raw = bytes(writer.buf)
        else:  # truncated
            writer.buf += build_transaction_data(to=to, nonce=nonce)
            writer.write_bytes(2, calc_tx_hash(to=to, nonce=nonce))
            writer.write_bytes(3, b"\x00" * 65)
            writer.write_byte(9, 0)
            writer.end_struct()
            raw = bytes(writer.buf[: len(writer.buf) // 2])
        response = rpc_call(rpc, "sendTransaction",
                            [GROUP_ID, "", "0x" + raw.hex(), False])
        return {"kind": kind, "submitted": "error" not in response,
                "error": response.get("error")}

    # --------------------------------------------------------------- probes

    def node_probes(self, net, index: int) -> dict:
        return {
            "alive": net.alive(index),
            "height": net.current_block_number(index),
            "pbft_view": net.pbft_view(index),
            "pending": net.pending_tx_size(index),
            "reach_new_view": net.log_count(index, "reachNewView"),
            "timeout_events": net.log_count(index, "triggerTimeout") +
                              net.log_count(index, "broadcastViewChange"),
            "verify_sender_failed": net.log_count(
                index, "verify sender for tx failed"),
            "consensus_timeouts": net.consensus_timeout_values(index),
        }
