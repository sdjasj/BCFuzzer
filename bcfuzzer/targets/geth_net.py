"""13-node geth network factory (PoS, TTD-zero, fake-beacon driven).

Generalizes the two-node setup of live_node_geth.py to `n_nodes` execution
nodes on loopback (ports p2p=30310+i, http=8545+i, authrpc=8551+n+i — the
authrpc range is pushed above the http range: with the original 8551+i the
authrpc port of node i equals the http port of node i+6, so six nodes died
at startup with "bind: address already in use" on every 13-node launch).
Any node can be the clique producer: the signer keystore is copied into
every node's datadir and the fake beacon (inter-node-bugs-final PoC client)
drives the engine API on that node's authrpc port.

`engine_drive` replicates the fast block loop of
inter-node-bugs-final/geth/01_miner_gaslimit_collapse/poc_bcb10_gaslimit_collapse.sh
(FCU -> getPayload -> newPayload -> FCU, 0.15 s per block, explicit
timestamps, gasLimit milestones) — the loop that collapses the chain gas
limit under a `miner.gaslimit=5000` producer (paper bug #8).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from live_node_geth import (  # noqa: E402
    FAKE_CL, BLOB_FAKE_CL, GENESIS, PEER_GETH, PASSWORD,
    drive_fake_beacon, kill_stale_geth_processes, make_keys, rpc_call)

# Genesis gasLimit kept at 8M like the PoC: the 4000-block collapse then
# lands below the 300k oracle threshold instead of ~603k with a 30M start.
GENESIS_GAS_LIMIT_HEX = "0x7a1200"  # 8_000_000
CLIQUE_PERIOD = 5                   # seconds; block timestamp must advance

FAST_BLOCKS_PERIOD = 0.15
COLLAPSE_BLOCKS = 4000              # phase-1 attack blocks (PoC ROUNDS_A)
STICKY_BLOCKS = 500                 # phase-2 normal-producer blocks (PoC ROUNDS_B)
COLLAPSE_THRESHOLD = 300_000        # PoC success threshold


def patch_genesis_gaslimit() -> None:
    data = json.loads(GENESIS.read_text(encoding="utf-8"))
    data["gasLimit"] = GENESIS_GAS_LIMIT_HEX
    GENESIS.write_text(json.dumps(data), encoding="utf-8")


def extract_blob_hashes(transactions: list) -> list[str]:
    """Blob versioned hashes of type-3 txs in a payload (PoC geth/02 helper)."""
    import rlp
    hashes: list[str] = []
    for raw in transactions:
        if isinstance(raw, str):
            data = bytes.fromhex(raw[2:]) if raw.startswith("0x") else bytes.fromhex(raw)
        else:
            data = bytes(raw)
        if not data or data[0] != 0x03:
            continue
        try:
            body = rlp.decode(data[1:])
            inner = body[0] if isinstance(body[0], list) else body
            for h in inner[10]:
                hashes.append("0x" + h.hex())
        except Exception:
            continue
    return hashes


class GethNetwork:
    def __init__(self, work: Path, n_nodes: int = 13,
                 instrumented: bool = False,
                 binary: Path | None = None,
                 networkid: int = 1337,
                 kill_stale: bool = True) -> None:
        # networkid/kill_stale let concurrent geth networks coexist:
        # live_node_geth.kill_stale_geth_processes matches every process
        # whose cmdline contains "--networkid 1337", so a parallel network
        # must use a different networkid AND skip the kill sweep (its own
        # sweep would otherwise murder the sibling network).
        self.networkid = networkid
        self.kill_stale = kill_stale
        self.work = Path(work)
        self.n = n_nodes
        self.nodes = [f"node{i}" for i in range(n_nodes)]
        self.procs: dict[int, subprocess.Popen] = {}
        self.signers: dict[int, str] = {}
        self.binary = binary or PEER_GETH
        if instrumented:
            from live_node_geth import ensure_instrumented_binary
            self.binary = ensure_instrumented_binary()
        self.configs: dict[int, Path | None] = {i: None for i in range(n_nodes)}
        self.miners: set[int] = set()
        # per-instance port offset derived from the work dir, in a range
        # disjoint from the fisco network's [0, 5000) offset range so
        # concurrent campaigns can never fight over p2p/rpc ports
        self.port_offset = 6000 + int(hashlib.md5(
            str(Path(work).resolve()).encode()).hexdigest()[:6], 16) % 5000

    # ------------------------------------------------------------- lifecycle

    def setup(self) -> str:
        """Create signer key, patched genesis, init all node datadirs."""
        self.work.mkdir(parents=True, exist_ok=True)
        signer = make_keys(self.work)
        patch_genesis_gaslimit()
        keystore = self.work / "node0" / "keystore"
        for node in self.nodes[1:]:
            shutil.copytree(keystore, self.work / node / "keystore",
                            dirs_exist_ok=True)
        for node in self.nodes:
            subprocess.run(
                [str(self.binary), "--datadir", str(self.work / node),
                 "init", str(GENESIS)],
                check=True, capture_output=True, text=True, timeout=120)
        return signer

    def rpc_url(self, index: int) -> str:
        return f"http://127.0.0.1:{8545 + self.port_offset + index}"

    def authrpc_port(self, index: int) -> int:
        return 8551 + self.n + self.port_offset + index

    def start_node(self, index: int, config: Path | None, mine: bool,
                   log_path: Path) -> subprocess.Popen:
        d = self.work / self.nodes[index]
        argv = [str(self.binary)]
        if config is not None:
            argv += ["--config", str(config)]
        argv += ["--datadir", str(d), "--networkid", str(self.networkid),
                 "--syncmode", "full",
                 "--port", str(30310 + self.port_offset + index),
                 "--http", "--http.port", str(8545 + self.port_offset + index),
                 "--http.addr", "127.0.0.1",
                 "--http.api", "eth,net,web3,txpool,admin",
                 "--authrpc.addr", "127.0.0.1",
                 "--authrpc.port", str(self.authrpc_port(index)),
                 "--bootnodes", "", "--netrestrict", "127.0.0.0/8"]
        if mine:
            argv += ["--password", str(PASSWORD)]
        env = os.environ.copy()
        env["GOC_SERVICE_NAME"] = f"geth-{self.nodes[index]}"
        fh = log_path.open("w")
        proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT,
                                env=env)
        self.procs[index] = proc
        self.configs[index] = config
        if mine:
            self.miners.add(index)
        return proc

    def start_all(self, configs: dict[int, Path | None],
                  miners: set[int], logs_dir: Path) -> bool:
        logs_dir.mkdir(parents=True, exist_ok=True)
        if self.kill_stale:
            kill_stale_geth_processes()
        for index in range(self.n):
            self.start_node(index, configs.get(index), index in miners,
                            logs_dir / f"{self.nodes[index]}.log")
        ok = True
        for index in range(self.n):
            if not self.wait_http(index, timeout=90):
                ok = False
        # mesh even when some node failed to come up (its mutated config
        # killed it at parse): the surviving nodes still need peers for
        # admission probes and the sync fetch — one dead controlled node
        # must not strand the other twelve (smoke4 round 1: a config-fatal
        # node made ok=False and skipped the mesh entirely)
        self.connect_mesh()
        return ok

    def connect_mesh(self, wait: float = 6.0) -> None:
        """Star around node0 plus a ring among all nodes.

        addPeer silently no-ops when the target's P2P listener is not up
        yet (fresh boot, cold caches), so poll peer counts and re-add
        until every live node sees at least one peer — otherwise the
        admission probes and the sync fetch race an unmeshed network
        (smoke: round-1 verdicts were False on nodes that later meshed
        and followed the chain)."""
        if sum(1 for i in range(self.n) if self.alive(i)) < 2:
            return  # nothing to mesh
        deadline = time.monotonic() + 90
        while True:
            enodes: dict[int, str] = {}
            for index in range(self.n):
                if not self.alive(index):
                    continue
                info = rpc_call(self.rpc_url(index), "admin_nodeInfo") or {}
                if isinstance(info, dict) and info.get("enode"):
                    enodes[index] = info["enode"]
            for index in range(1, self.n):
                if 0 in enodes and self.alive(index):
                    rpc_call(self.rpc_url(index), "admin_addPeer",
                             [enodes[0]])
            for index in range(self.n):
                ring = (index + 1) % self.n
                if ring in enodes and self.alive(index):
                    rpc_call(self.rpc_url(index), "admin_addPeer",
                             [enodes[ring]])
            time.sleep(wait)
            unmeshed = [i for i in range(self.n)
                        if self.alive(i) and self.peer_count(i) < 1]
            if not unmeshed or time.monotonic() > deadline:
                return

    def wait_http(self, index: int, timeout: int = 60) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if rpc_call(self.rpc_url(index), "eth_blockNumber") is not None:
                return True
            proc = self.procs.get(index)
            if proc is not None and proc.poll() is not None:
                # the process died (mutated config rejected at parse,
                # e.g. blobpool.datacap=-1) — stop polling and let the
                # round record the failed admission instead of burning
                # the full timeout (smoke4 round 1: a config-fatal node
                # ate 90s of start_all and skipped the mesh entirely)
                return False
            time.sleep(1)
        return False

    def alive(self, index: int) -> bool:
        proc = self.procs.get(index)
        return proc is not None and proc.poll() is None

    def stop_all(self) -> None:
        for proc in self.procs.values():
            if proc is not None and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass  # raced with the process exiting on its own
        for proc in self.procs.values():
            if proc is not None:
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.procs.clear()
        self.miners.clear()
        if self.kill_stale:
            kill_stale_geth_processes()

    # ------------------------------------------------------------- observers

    def height(self, index: int) -> int:
        result = rpc_call(self.rpc_url(index), "eth_blockNumber")
        return int(result, 16) if isinstance(result, str) else 0

    def gaslimit(self, index: int) -> int:
        block = rpc_call(self.rpc_url(index), "eth_getBlockByNumber",
                         ["latest", False])
        if isinstance(block, dict) and isinstance(block.get("gasLimit"), str):
            return int(block["gasLimit"], 16)
        return 0

    def head(self, index: int) -> dict:
        block = rpc_call(self.rpc_url(index), "eth_getBlockByNumber",
                         ["latest", False])
        return block if isinstance(block, dict) else {}

    def peer_count(self, index: int) -> int:
        result = rpc_call(self.rpc_url(index), "net_peerCount")
        return int(result, 16) if isinstance(result, str) else 0

    def jwtsecret(self, index: int) -> Path:
        return self.work / self.nodes[index] / "geth" / "jwtsecret"

    # ------------------------------------------------------- engine driving

    def _engine_rpc(self, port: int, jwtfile: Path, method: str,
                    params: list) -> dict:
        raw = jwtfile.read_bytes().strip()
        secret = bytes.fromhex(raw[2:].decode()) if raw.startswith(b"0x") \
            else bytes.fromhex(raw.decode())
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(
            json.dumps({"iat": int(time.time())}).encode()).rstrip(b"=")
        signing = header + b"." + payload
        token = (signing + b"." + base64.urlsafe_b64encode(
            hmac.new(secret, signing, hashlib.sha256).digest()).rstrip(b"=")
                 ).decode()
        response = requests.post(
            f"http://127.0.0.1:{port}",
            json={"jsonrpc": "2.0", "method": method, "params": params,
                  "id": 1},
            headers={"Authorization": f"Bearer {token}"}, timeout=30)
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"engine {method}: {data['error']}")
        return data["result"]

    def engine_drive(self, index: int, rounds: int,
                     period: float = FAST_BLOCKS_PERIOD,
                     start_ts: int = 0) -> dict:
        """Fast block loop (PoC fast_blocks) on the post-prague engine API:
        FCUv3 -> getPayloadV5 -> newPayloadV4 -> FCUv3.  The genesis has
        shanghai/cancun/prague active at block 0, so V1 is rejected
        ("fcuV1 called post-shanghai") and newPayload must carry the
        executionRequests field ("nil executionRequests post-prague") —
        exactly the v5 flow the geth/02 blob PoC client uses."""
        port = self.authrpc_port(index)
        jwtfile = self.jwtsecret(index)
        if not jwtfile.is_file():
            return {"error": "no jwtsecret", "milestones": {}}
        head = self.head(index).get("hash")
        if not head:
            return {"error": "no head", "milestones": {}}
        head0 = head
        ts = start_ts or (int(self.head(index).get("timestamp", "0x0"), 16) + 1)
        milestones: dict[int, int] = {}
        for i in range(rounds):
            attrs = {
                "timestamp": hex(ts),
                "prevRandao": "0x" + f"{i:064x}"[-64:],
                "suggestedFeeRecipient":
                    "0x0000000000000000000000000000000000000001",
                "withdrawals": [],
                "parentBeaconBlockRoot": "0x" + "11" * 32,
            }
            fcu = self._engine_rpc(
                port, jwtfile, "engine_forkchoiceUpdatedV3",
                [{"headBlockHash": head, "safeBlockHash": head,
                  "finalizedBlockHash": head0}, attrs])
            pid = fcu.get("payloadId")
            if not pid:
                return {"error": "no payloadId", "milestones": milestones,
                        "blocks": i}
            time.sleep(period)
            env = self._engine_rpc(port, jwtfile, "engine_getPayloadV5", [pid])
            exec_payload = env["executionPayload"]
            block_hash = exec_payload["blockHash"]
            gas_limit = int(exec_payload["gasLimit"], 16)
            vhashes = extract_blob_hashes(exec_payload["transactions"])
            self._engine_rpc(
                port, jwtfile, "engine_newPayloadV4",
                [exec_payload, vhashes, "0x" + "11" * 32, []])
            self._engine_rpc(
                port, jwtfile, "engine_forkchoiceUpdatedV3",
                [{"headBlockHash": block_hash, "safeBlockHash": block_hash,
                  "finalizedBlockHash": head0}, None])
            head = block_hash
            ts += 1
            if i in (0, rounds // 4, rounds // 2, 3 * rounds // 4, rounds - 1):
                milestones[i + 1] = gas_limit
        return {"blocks": rounds, "milestones": milestones, "final_head": head}

    def drive_beacon(self, index: int, mode: str, rounds: int,
                     head_hash: str | None = None, period: int = 1,
                     api_version: str = "v1",
                     script: Path = FAKE_CL) -> bool:
        """Run the PoC fake beacon client against this node's engine port."""
        jwt = self.jwtsecret(index)
        if not jwt.is_file():
            return False
        target = head_hash or self.head(index).get("hash")
        if not target:
            return False
        return drive_fake_beacon(
            self.authrpc_port(index), jwt, target, mode, rounds, period,
            api_version=api_version, script=script)

    def gaslimit_with_retries(self, index: int, attempts: int = 6,
                              delay: float = 5.0) -> int:
        """eth_getBlockByNumber.latest on a busy post-attack node can
        exceed the 5 s rpc timeout; retry before giving up."""
        for _ in range(attempts):
            value = self.gaslimit(index)
            if value:
                return value
            time.sleep(delay)
        return 0

    def _head_with_retries(self, index: int, attempts: int = 6,
                           delay: float = 5.0) -> dict:
        """eth_getBlockByNumber on a busy post-attack producer can exceed
        the 5 s rpc timeout; retry before giving up.  The raw error block
        and liveness are returned so callers can tell overload from death."""
        block: dict = {}
        for _ in range(attempts):
            block = self.head(index)
            if isinstance(block, dict) and block.get("hash"):
                return block
            time.sleep(delay)
        return block

    def sync_all(self, exclude: set[int], rounds: int = 8,
                 api_version: str = "v3",
                 source: int | None = None) -> dict[int, bool]:
        """Bring every non-excluded node to the current chain head via the
        fake beacon (post-merge blocks are not p2p-announced; this is how
        the PoCs make the normal node accept the attacker's blocks).

        13-node note: driving every target at once makes the source serve
        12 concurrent full-chain downloads and starves its HTTP RPC, so
        targets are driven in small batches with a settle pause; a node
        whose import outlasts the beacon timeout is marked False instead
        of raising.

        The FCU-to-unknown-head fetch only succeeds once the node is
        connected to a peer that carries the chain (smoke: round 1 raced
        the mesh and every node stayed at genesis), and the fake client
        exits 0 even when every engine call failed, so "the beacon ran"
        is not evidence of sync.  We therefore poll heights after the
        batch pass and re-drive laggards until they converge."""
        if source is None:
            source = next(iter(exclude)) if exclude else 0
        head_block = self._head_with_retries(source)
        head_hash = head_block.get("hash")
        results: dict[int, bool] = {}
        if not head_hash:
            results["_error"] = True
            results["_raw"] = head_block
            results["_alive"] = self.alive(source)
            return results
        head_number = int(head_block.get("number", "0x0"), 16)
        targets = [i for i in range(self.n) if i not in exclude]

        # star around the SOURCE before driving: the FCU-triggered fetch
        # asks the node's peers for the chain, and post-merge blocks are
        # not p2p-announced, so without a direct link to the source every
        # normal must wait for hop-by-hop relay along the ring — which
        # the round-end teardown kills before it completes (stageG3 geth
        # leg: convergence fell to 1/9 once node3 replaced node0 as the
        # highest-head source and stopped being the star center)
        src_info = rpc_call(self.rpc_url(source), "admin_nodeInfo") or {}
        if isinstance(src_info, dict) and src_info.get("enode"):
            src_enode = src_info["enode"]
            for index in targets:
                if self.alive(index):
                    rpc_call(self.rpc_url(index), "admin_addPeer",
                             [src_enode])

        def _drive_batch(batch: list[int]) -> None:
            for index in batch:
                try:
                    results[index] = self.drive_beacon(
                        index, "update", rounds, head_hash,
                        api_version=api_version)
                except subprocess.TimeoutExpired:
                    results[index] = False
            time.sleep(5)

        for start in range(0, len(targets), 3):
            _drive_batch(targets[start:start + 3])
        # convergence: poll, re-mesh and re-drive the laggards up to
        # three extra passes; the fetch runs async after the beacon
        # client exits, so wait for real import progress between passes
        for _pass in range(3):
            laggards = [i for i in targets
                        if self.alive(i) and self.height(i) < head_number]
            if not laggards:
                break
            self.connect_mesh(wait=2.0)
            _drive_batch(laggards)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if not [i for i in laggards
                        if self.alive(i) and self.height(i) < head_number]:
                    break
                time.sleep(5)
        time.sleep(2)
        results["_heights"] = {i: self.height(i) for i in targets}
        results["_converged"] = [
            i for i in targets if self.height(i) >= head_number]
        return results

    def rotate_producer(self, old_index: int, new_index: int,
                        rounds: int = STICKY_BLOCKS) -> dict:
        """Hand the producer role over (PoC phase 2): sync the new producer to
        the old head, then drive it from the head's timestamp + 1."""
        old_head = self._head_with_retries(old_index)
        head_hash = old_head.get("hash")
        if not head_hash:
            return {"error": "no head on old producer", "raw": old_head,
                    "alive": self.alive(old_index), "milestones": {}}
        self.drive_beacon(new_index, "update", 8, head_hash, api_version="v3")
        time.sleep(3)
        start_ts = int(old_head.get("timestamp", "0x0"), 16) + 1
        return self.engine_drive(new_index, rounds, start_ts=start_ts)

    # ------------------------------------------------------------- capacity

    def nominal_capacity(self, index: int) -> int:
        """tx/block the chain currently admits (PoC metric)."""
        gas_limit = self.gaslimit(index)
        return gas_limit // 21000 if gas_limit else 0

    def teardown(self) -> None:
        self.stop_all()
        shutil.rmtree(self.work, ignore_errors=True)


def main() -> int:
    """Smoke test (plan B1): 13 nodes up, meshed, engine_drive 20 blocks."""
    import argparse
    import tempfile
    parser = argparse.ArgumentParser(description="geth 13-node net smoke test")
    parser.add_argument("--nodes", type=int, default=13)
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="geth13-net-", dir="/tmp"))
    logs = work / "logs"
    net = GethNetwork(work, n_nodes=args.nodes)
    try:
        net.setup()
        ok = net.start_all({}, {0}, logs)
        print(f"start_all={ok}")
        for i in range(args.nodes):
            print(f"node{i}: peers={net.peer_count(i)} height={net.height(i)} "
                  f"gaslimit={net.gaslimit(i)}")
        result = net.engine_drive(0, args.rounds)
        print("engine_drive:", json.dumps(result, indent=2))
        return 0 if ok and result.get("blocks") == args.rounds else 1
    finally:
        net.teardown()


if __name__ == "__main__":
    raise SystemExit(main())
