"""13-node FISCO-BCOS PBFT network factory.

Generalizes live_node_fisco.build_network/start_chain from 4 to n nodes:
build_chain.sh -l 127.0.0.1:13 produces node0..node12 (p2p 30300+i,
RPC 20200+i, verified in scripts).  The coverage-instrumented binary is
linked in the same way as the coverage runner (fisco-bcos-cov symlink +
start.sh rewrite); the fuzzing default is the plain air binary.

Consensus readiness = every node's log shows reachNewView (the PoC gate,
relaxed to 240 s for 13 nodes).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from live_node_fisco import (  # noqa: E402
    BUILD_CHAIN, PEER_NODE_BIN, RPC_CERT,
    configure_rpc_tls, cov_node_bin, coverage_env_exports,
    rpc_call, terminate_pids)


class FiscoNetwork:
    def __init__(self, runtime: Path, n_nodes: int = 13,
                 instrumented: bool = False) -> None:
        self.runtime = Path(runtime)
        self.n = n_nodes
        self.instrumented = instrumented
        self.net_dir: Path | None = None
        # per-instance port offset derived from the runtime dir, in a
        # range disjoint from the geth network's [6000, 11000) offset
        # range so concurrent fisco networks (or fisco + geth) never
        # fight over p2p/rpc ports (PORT_OFFSET is a fixed 0 and two
        # 13-node networks then split the port space between them)
        self.port_offset = int(hashlib.md5(
            str(Path(runtime).resolve()).encode()).hexdigest()[:6], 16) % 5000

    # ------------------------------------------------------------- lifecycle

    def build(self, timeout: int = 300) -> Path:
        nodes = self.runtime / "nodes"
        if (nodes / "127.0.0.1").exists():
            shutil.rmtree(nodes)
        p2p_start = 30300 + self.port_offset
        rpc_start = 20200 + self.port_offset
        subprocess.run(
            ["bash", str(BUILD_CHAIN), "-p", f"{p2p_start},{rpc_start}",
             "-l", f"127.0.0.1:{self.n}",
             "-o", str(nodes), "-e", str(PEER_NODE_BIN)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout)
        net_dir = nodes / "127.0.0.1"
        if self.instrumented:
            for node in range(self.n):
                node_dir = net_dir / f"node{node}"
                cov_link = node_dir / "fisco-bcos-cov"
                cov_link.unlink(missing_ok=True)
                cov_link.symlink_to(cov_node_bin())
                script = node_dir / "start.sh"
                text = script.read_text(encoding="utf-8")
                text = re.sub(r"^fisco_bcos=.*$", f"fisco_bcos={cov_link}",
                              text, flags=re.MULTILINE)
                if "GCOV_PREFIX=" not in text:
                    text = text.replace(
                        "cd ${SHELL_FOLDER}\n",
                        "cd ${SHELL_FOLDER}\n" + coverage_env_exports(),
                        1,
                    )
                script.write_text(text, encoding="utf-8")
        configure_rpc_tls(net_dir)
        self.net_dir = net_dir
        return net_dir

    def node_dir(self, index: int) -> Path:
        assert self.net_dir is not None
        return self.net_dir / f"node{index}"

    def rpc_for(self, index: int) -> str:
        return f"https://127.0.0.1:{20200 + self.port_offset + index}"

    def start_all(self, timeout: int = 240) -> bool:
        assert self.net_dir is not None
        started = subprocess.run(
            ["bash", "start_all.sh"], cwd=self.net_dir, timeout=180,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if started.returncode != 0:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(self._reached_new_view(i) for i in range(self.n)):
                return True
            time.sleep(3)
        return False

    def stop_all(self) -> None:
        if self.net_dir is None:
            return
        try:
            subprocess.run(["bash", "stop_all.sh"], cwd=self.net_dir,
                           timeout=180, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            pass
        terminate_pids(self.net_dir, sig=15, wait_seconds=12)
        terminate_pids(self.net_dir, sig=15, wait_seconds=15)
        terminate_pids(self.net_dir, sig=9, wait_seconds=10)

    def _node_pids(self, index: int) -> list[int]:
        """PIDs of this node's processes.  live_node_fisco.pids_under
        matches the bare path as a substring, so ".../node1" also hits
        ".../node10/../fisco-bcos" and stop_node(1) killed nodes 10-12.
        We require the path separator after the node dir."""
        needle = (str(self.node_dir(index)) + "/").encode()
        pids = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                cmdline = (proc / "cmdline").read_bytes()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if needle in cmdline:
                pids.append(int(proc.name))
        return pids

    def _terminate_node(self, index: int, *, sig: int,
                        wait_seconds: float) -> None:
        for pid in self._node_pids(index):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if not self._node_pids(index):
                return
            time.sleep(0.2)

    def start_node(self, index: int, timeout: int = 90) -> bool:
        """Restart semantics.  Two pitfalls fixed after the first smoke:

        - "reachNewView" is a FIRST-BOOT marker: a restarted PBFT node
          re-reaches views but never prints it again, so a healthy
          restart was never admitted.  The restart gate is instead
          "init PBFT success" (printed ~50 ms into a successful start,
          AFTER config validation — an invalid config dies before it)
          plus a live process plus an answering RPC.

        - The pre-restart log still contains reachNewView, so the fresh
          log (new log_<ts>.log of the restarted process) is required —
          otherwise a failed relaunch reports success."""
        node_dir = self.node_dir(index)
        pre = {f.name for f in self.log_files(index)}
        subprocess.run(["bash", "start.sh"], cwd=node_dir, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            fresh = [f for f in self.log_files(index) if f.name not in pre]
            if fresh and self.alive(index):
                try:
                    if "init PBFT success" in fresh[-1].read_text(
                            errors="replace"):
                        response = rpc_call(self.rpc_for(index),
                                            "getBlockNumber", ["group0", ""])
                        if "result" in response:
                            return True
                except OSError:
                    pass
            time.sleep(1)
        return False

    def stop_node(self, index: int) -> None:
        node_dir = self.node_dir(index)
        try:
            subprocess.run(["bash", "stop.sh"], cwd=node_dir, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            pass
        # _terminate_node, NOT live_node_fisco.terminate_pids: the bare
        # substring needle ".../node1" matches ".../node10/../fisco-bcos"
        # and killed nodes 10-12 alongside the controlled node.
        self._terminate_node(index, sig=15, wait_seconds=10)
        self._terminate_node(index, sig=9, wait_seconds=5)

    # ------------------------------------------------------------- observers

    def log_files(self, index: int) -> list[Path]:
        log_dir = self.node_dir(index) / "log"
        return sorted(log_dir.glob("log*"))

    def _log_text(self, index: int) -> str:
        text = ""
        for path in self.log_files(index):
            try:
                text += path.read_text(errors="replace")
            except OSError:
                continue
        return text

    def _reached_new_view(self, index: int) -> bool:
        return "reachNewView" in self._log_text(index)

    def log_count(self, index: int, pattern: str) -> int:
        return self._log_text(index).count(pattern)

    def alive(self, index: int) -> bool:
        return bool(self._node_pids(index))

    def current_block_number(self, index: int) -> int:
        response = rpc_call(self.rpc_for(index), "getBlockNumber",
                            ["group0", ""])
        result = response.get("result", 0)
        if isinstance(result, str):
            try:
                return int(result, 16) if result.startswith("0x") else int(result)
            except ValueError:
                return 0
        return int(result) if isinstance(result, int) else 0

    def pbft_view(self, index: int) -> int:
        response = rpc_call(self.rpc_for(index), "getPbftView",
                            ["group0", ""])
        result = response.get("result", "")
        if isinstance(result, str) and result.startswith("0x"):
            try:
                return int(result, 16)
            except ValueError:
                return -1
        try:
            return int(result)
        except (TypeError, ValueError):
            return -1

    def pending_tx_size(self, index: int) -> int:
        response = rpc_call(self.rpc_for(index), "getPendingTxSize",
                            ["group0", ""])
        result = response.get("result", "")
        if isinstance(result, str) and result.startswith("0x"):
            try:
                return int(result, 16)
            except ValueError:
                return 0
        try:
            return int(result)
        except (TypeError, ValueError):
            return 0

    def sealer_list(self, index: int) -> list:
        response = rpc_call(self.rpc_for(index), "getSealerList",
                            ["group0", ""])
        result = response.get("result", [])
        return result if isinstance(result, list) else []

    def consensus_timeout_values(self, index: int) -> list[str]:
        matches = re.findall(r"consensusTimeout=(\d+)", self._log_text(index))
        return sorted(set(matches))

    def teardown(self) -> None:
        self.stop_all()
        shutil.rmtree(self.runtime, ignore_errors=True)


def main() -> int:
    """Smoke test (plan B2): build + start 13 nodes, check consensus."""
    import argparse
    import tempfile
    parser = argparse.ArgumentParser(description="fisco 13-node net smoke test")
    parser.add_argument("--nodes", type=int, default=13)
    args = parser.parse_args()
    runtime = Path(tempfile.mkdtemp(prefix="fisco13-net-", dir="/tmp"))
    net = FiscoNetwork(runtime, n_nodes=args.nodes)
    try:
        net.build()
        print("build OK, net_dir:", net.net_dir)
        ok = net.start_all()
        print(f"start_all={ok}")
        for i in range(args.nodes):
            print(f"node{i}: alive={net.alive(i)} "
                  f"view={net.pbft_view(i)} height={net.current_block_number(i)}")
        if ok:
            print("sealers on node0:", len(net.sealer_list(0)))
        return 0 if ok else 1
    finally:
        net.teardown()


if __name__ == "__main__":
    raise SystemExit(main())
