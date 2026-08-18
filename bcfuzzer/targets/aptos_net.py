"""13-validator Aptos forge swarm factory.

Generalizes live_node_aptos.launch_swarm from 2 to n validators:
`forge --suite run_forever --num-validators N test local-swarm --swarmdir
<runtime> --aptos-node-binary <peer-node>`, wait until every validator's
API reports ledger_version > 0, then detach Forge (validators keep
running) and restart any validator that did not survive detachment via
`aptos-node -f <node.yaml>` — the exact PoC #12 restart pattern, which is
also the per-round admission path for mutated node.yaml files.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from live_node_aptos import (  # noqa: E402
    FORGE, PEER_NODE, api_url, kill_processes_under, ledger_version,
    pids_for_config, stop_config_processes, wait_for_network)


class AptosNetwork:
    def __init__(self, runtime: Path, n_validators: int = 13) -> None:
        self.runtime = Path(runtime)
        self.n = n_validators
        self.root_key = ""

    # ------------------------------------------------------------- lifecycle

    def config_of(self, index: int) -> Path:
        return self.runtime / f"{index}" / "node.yaml"

    def api_of(self, index: int) -> str:
        return api_url(self.config_of(index))

    def launch(self, timeout: int = 420) -> str:
        """Forge-launch the swarm, detach, return the root key."""
        command = [
            str(FORGE), "--suite", "run_forever",
            "--num-validators", str(self.n),
            "test", "local-swarm", "--swarmdir", str(self.runtime),
            "--aptos-node-binary", str(PEER_NODE),
        ]
        configs = [self.config_of(i) for i in range(self.n)]
        errors: list[str] = []
        log_path = self.runtime / "forge-launch.log"
        for attempt in range(1, 4):
            shutil.rmtree(self.runtime, ignore_errors=True)
            self.runtime.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n=== launch attempt {attempt} ===\n")
                log.flush()
                forge = subprocess.Popen(
                    command, cwd=str(PEER_NODE.parent.parent),
                    stdout=log, stderr=subprocess.STDOUT)
                launch_error = None
                try:
                    if not wait_for_network(configs, timeout=timeout):
                        launch_error = (
                            f"forge did not produce a live {self.n}-validator swarm")
                    else:
                        time.sleep(2)
                        forge.terminate()
                        try:
                            forge.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            forge.kill()
                            forge.wait()
                        for index, cfg in enumerate(configs):
                            if pids_for_config(cfg):
                                continue
                            peer_log = (self.runtime / f"{index}"
                                        / "peer-restart.log").open("a", encoding="utf-8")
                            subprocess.Popen(
                                [str(PEER_NODE), "-f", str(cfg)], cwd=cfg.parent,
                                stdout=peer_log, stderr=subprocess.STDOUT,
                                start_new_session=True)
                        if not wait_for_network(configs, timeout=180):
                            launch_error = "detached swarm lost validators"
                    if launch_error is None:
                        self.root_key = (self.runtime / "root_key").read_text().strip()
                        return self.root_key
                finally:
                    if forge.poll() is None:
                        forge.terminate()
                        try:
                            forge.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            forge.kill()
                            forge.wait()
                errors.append(f"attempt {attempt}: {launch_error or 'unknown launch error'}")
            kill_processes_under(self.runtime)
        raise RuntimeError("; ".join(errors) if errors else "forge did not produce a live swarm")

    def start_node(self, index: int, timeout: int = 120) -> bool:
        cfg = self.config_of(index)
        if pids_for_config(cfg):
            return True
        log_fh = (self.runtime / f"{index}" / "peer-restart.log").open("a", encoding="utf-8")
        proc = subprocess.Popen([str(PEER_NODE), "-f", str(cfg)], cwd=cfg.parent,
                                stdout=log_fh, stderr=subprocess.STDOUT,
                                start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ledger_version(cfg) is not None:
                return True
            if proc.poll() is not None:
                return False  # mutated node.yaml rejected: fail fast
            time.sleep(2)
        return ledger_version(cfg) is not None

    def stop_node(self, index: int) -> None:
        stop_config_processes(self.config_of(index))

    def stop_all(self) -> None:
        for index in range(self.n):
            self.stop_node(index)
        kill_processes_under(self.runtime)

    # ------------------------------------------------------------- observers

    def alive(self, index: int) -> bool:
        return bool(pids_for_config(self.config_of(index)))

    def ledger(self, index: int) -> int | None:
        return ledger_version(self.config_of(index))

    def log_text(self, index: int) -> str:
        text = ""
        log_dir = self.runtime / f"{index}"
        for path in sorted(log_dir.glob("*.log")):
            try:
                text += path.read_text(errors="replace")
            except OSError:
                continue
        return text

    def teardown(self) -> None:
        self.stop_all()
        shutil.rmtree(self.runtime, ignore_errors=True)


def main() -> int:
    """Smoke test (plan B4): forge 13 validators, all ledger_version > 0."""
    import argparse
    import tempfile
    parser = argparse.ArgumentParser(description="aptos 13-validator smoke test")
    parser.add_argument("--validators", type=int, default=13)
    args = parser.parse_args()
    runtime = Path(tempfile.mkdtemp(prefix="aptos13-net-", dir="/tmp"))
    net = AptosNetwork(runtime, n_validators=args.validators)
    try:
        root_key = net.launch()
        print(f"launch OK, root_key={root_key[:12]}...")
        for i in range(args.validators):
            print(f"val{i}: alive={net.alive(i)} ledger={net.ledger(i)} "
                  f"api={net.api_of(i)}")
        return 0
    finally:
        net.teardown()


if __name__ == "__main__":
    raise SystemExit(main())
