"""13-org ChainMaker TBFT network factory with a capability-instrumented binary.

Two one-time artifacts are prepared outside the fuzz loop:

  build_13org_release()
      Runs `prepare.sh 13 1 11301 12301 ... -c 1 -l INFO ...` +
      build_release.sh once (the script natively supports 13 orgs — verified),
      stashes the 13 per-org tarballs in /tmp/cm13-release and restores the
      prior 4-org build state so the coverage runner keeps working.

  ensure_capability_binary()
      Patches the chainmaker-go source with the three env-gated malicious
      switches extracted from the inter-node-bugs-final PoCs (verified
      anchors), builds once with goc (coverage佐证) and restores the files
      via git checkout.  The switches, enabled per round via
      CM_MALICIOUS_* env vars on a controlled org, are the M-corpus delivery
      channel for paper bugs #1-#3.

Every round untars the 13 tarballs into a fresh runtime and copies the
capability binary into each org's bin/ (same pattern as the coverage
runner's prepare_runtime).
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from live_node_chainmaker import (  # noqa: E402
    CMC, GOC_CENTER, RELEASE, chainmaker_env, cmc, cmc_capture,
    kill_chainmaker_processes, node_running, org_domain, release_name,
    write_sdk_config)

import os
ROOT = Path(os.environ.get("BCFZ_WORKSPACE", "/home/geth/tse")) / "chainmaker-go"
SCRIPTS = ROOT / "scripts"
CHAINMAKER_MAIN = ROOT / "main"
CM13_STASH = Path("/tmp/cm13-release")
CAP_BINARY = Path("/tmp/chainmaker-cap-cover")
# Same lock as the coverage runner's ensure_instrumented_binary: the two
# builds share one source tree, so a patched-tree capability build must
# never interleave with (or contaminate) a coverage build.
CAP_LOCK = Path("/tmp/chainmaker-goc-build.lock")
RELEASE_LOCK = Path("/tmp/chainmaker-13org-build.lock")
ORGS_13 = [f"wx-org{i}" for i in range(1, 14)]

# Verified PoC patch anchors + env-gated malicious blocks (PoC 06/07/08)
BLOCK_HELPER = ROOT / "module/core/common/block_helper.go"
PROPOSER_IMPL = ROOT / "module/core/syncmode/proposer/block_proposer_impl.go"
# Verified bc1.yml chainconfig edit anchors (PoC 07/08 — confirmed present
# in the 13-org release stash's bc1.yml)
BC1_GAS = (
    """account_config:
  # the flag to control if subtracting gas from transaction's origin account when sending tx.
  enable_gas: false""",
    """account_config:
  # the flag to control if subtracting gas from transaction's origin account when sending tx.
  enable_gas: true""")
BC1_OPT_GAS = (
    """  # Used for dynamic tuning the capacity of tx execution goroutine pool
  enable_conflicts_bit_window: true""",
    """  # Used for dynamic tuning the capacity of tx execution goroutine pool
  enable_conflicts_bit_window: true

  # enable optimized charge gas
  enable_optimize_charge_gas: true""")
BC1_TURBO = (
    """  # consensus_turbo_config:
    # If consensus message compression is enabled or not(solo could not use consensus message turbo).
    # consensus_message_turbo: false""",
    """  consensus_turbo_config:
    consensus_message_turbo: true
    retry_time: 500
    retry_interval: 20""")
BC1_PATCHES = {"turbo": [BC1_TURBO],
               "turbo_gas": [BC1_GAS, BC1_OPT_GAS, BC1_TURBO]}
PATCH_INDEX = ("\ttxBatchInfo.Index = indexes",
               """\t// [BCFuzzer capability] malicious proposer: corrupt index -> verifier OOB panic
\tif os.Getenv("CM_MALICIOUS_INDEX") == "1" && len(indexes) > 0 {
\t\tindexes[0] = 0xFFFFFFFF
\t}

\ttxBatchInfo.Index = indexes""")
PATCH_ANCHOR = ("""} else {
\t\tcutBlock = block
\t}

\treturn cutBlock
}""")
# Both proposer switches patch the same anchor in the same file, so they
# must be applied as ONE combined replacement — a second .replace on the
# same anchor would find nothing after the first patch consumed it.
PATCH_PROPOSER = PATCH_ANCHOR.replace(
    "\treturn cutBlock",
    """\t// [BCFuzzer capability] malicious proposer: TxCount out of bounds
\tif os.Getenv("CM_MALICIOUS_TXCOUNT") == "1" && len(cutBlock.Txs) > 0 {
\t\tcutBlock.Header.TxCount = uint32(len(cutBlock.Txs) + 100)
\t}

\t// [BCFuzzer capability] malicious proposer: nil payload to verifiers
\tif os.Getenv("CM_MALICIOUS_NILPAYLOAD") == "1" && len(cutBlock.Txs) > 1 {
\t\tcutBlock.Txs[1].Payload = nil
\t}

\treturn cutBlock""")


def _ensure_os_import(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if '"os"' in text:
        return
    if '"fmt"' in text:
        text = text.replace('"fmt"', '"fmt"\n\t"os"', 1)
    elif '"errors"' in text:
        text = text.replace('"errors"', '"errors"\n\t"os"', 1)
    path.write_text(text, encoding="utf-8")


def _patch_source() -> None:
    for path, (old, new) in (
            (BLOCK_HELPER, PATCH_INDEX),
            (PROPOSER_IMPL, (PATCH_ANCHOR, PATCH_PROPOSER)),
    ):
        _ensure_os_import(path)
        text = path.read_text(encoding="utf-8")
        if "BCFuzzer capability" in text:
            continue  # already patched (previous aborted build)
        assert old in text, f"patch anchor not found in {path}"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _restore_source() -> None:
    subprocess.run(
        ["git", "checkout", "--", "module/core/common/block_helper.go",
         "module/core/syncmode/proposer/block_proposer_impl.go"],
        cwd=ROOT, check=True, capture_output=True, timeout=120)


def ensure_capability_binary() -> Path:
    """Env-gated malicious-switch binary, built once under a file lock."""
    from goc_utils import ensure_goc_binary, goc_build_env

    with CAP_LOCK.open("w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        if CAP_BINARY.is_file() and os.access(CAP_BINARY, os.X_OK):
            return CAP_BINARY
        ensure_goc_binary()
        _patch_source()
        try:
            subprocess.run(
                ["/tmp/goc", "build", f"--center={GOC_CENTER}",
                 "--output", str(CAP_BINARY), "."],
                cwd=CHAINMAKER_MAIN, check=True, timeout=2400,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=goc_build_env())
        finally:
            _restore_source()
    return CAP_BINARY


def build_13org_release() -> Path:
    """Generate + stash the 13-org release tarballs; restore 4-org state."""
    with RELEASE_LOCK.open("w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        tarballs = sorted(CM13_STASH.glob("chainmaker-v3.0.0-wx-org*-x86_64.tar.gz"))
        if len(tarballs) >= 13:
            return CM13_STASH
        backup = Path("/tmp/cm-release-backup")
        if backup.exists():
            shutil.rmtree(backup)
        backup.mkdir(parents=True)
        for name in ("release", "crypto-config", "config"):
            src = ROOT / "build" / name
            if src.exists():
                shutil.copytree(src, backup / name)
        try:
            subprocess.run(
                ["bash", "prepare.sh", "13", "1", "11301", "12301",
                 "32351", "22351", "23351", "-c", "1", "-l", "INFO",
                 "-v", "false", "-j", "false", "--vlog=INFO", "--jlog=INFO"],
                cwd=SCRIPTS, check=True, timeout=900,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                ["bash", "build_release.sh"],
                cwd=SCRIPTS, check=True, timeout=2400,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            CM13_STASH.mkdir(parents=True, exist_ok=True)
            for tarball in RELEASE.glob("chainmaker-v3.0.0-wx-org*-x86_64.tar.gz"):
                shutil.move(str(tarball), CM13_STASH)
        finally:
            for name in ("release", "crypto-config", "config"):
                target = ROOT / "build" / name
                if target.exists():
                    shutil.rmtree(target)
                if (backup / name).exists():
                    shutil.copytree(backup / name, target)
    return CM13_STASH


def _cmc_ok(out: str) -> bool:
    """cmc exits 0 even when the rpc fails (error text goes to stdout via
    2>&1), so judge success by the result markers instead (PoC scripts
    grep for `"tx_id"` on create / `code:0` on sync invokes)."""
    return bool(out) and "Error:" not in out and (
        "code:0" in out.replace(" ", "") or '"tx_id"' in out)


_CENTER_PROC: subprocess.Popen | None = None


def ensure_center() -> None:
    """The goc-instrumented binary exits without a reachable coverage
    center; start one once per process (idempotent)."""
    global _CENTER_PROC
    if _CENTER_PROC is not None and _CENTER_PROC.poll() is None:
        return
    from goc_utils import start_goc_server
    _CENTER_PROC = start_goc_server(
        f"http://127.0.0.1:{GOC_CENTER.rsplit(':', 1)[1]}",
        Path("/tmp/bcfuzzer-goc-persistence"),
        Path("/tmp/bcfuzzer-goc-center.log"))


class ChainMakerNetwork:
    def __init__(self, runtime: Path, orgs: list[str] | None = None,
                 instrumented: bool = True) -> None:
        self.runtime = Path(runtime)
        self.orgs = orgs or ORGS_13
        self.instrumented = instrumented
        self.sdk_confs: dict[str, Path] = {}
        self._capability_env: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------- lifecycle

    def prepare(self) -> Path:
        if self.instrumented:
            ensure_center()
        cmc_binary = ROOT / "tools" / "cmc" / "cmc"
        if cmc_binary.is_file() and not CMC.is_file():
            shutil.copy2(cmc_binary, CMC)
        stash = build_13org_release()
        shutil.rmtree(self.runtime, ignore_errors=True)
        self.runtime.mkdir(parents=True)
        binary = (ensure_capability_binary() if self.instrumented
                  else RELEASE / release_name(self.orgs[0]) / "bin" / "chainmaker")
        for org in self.orgs:
            matches = sorted(stash.glob(f"{release_name(org)}-*-x86_64.tar.gz"))
            if not matches:
                raise FileNotFoundError(f"no 13-org tarball for {org} in {stash}")
            subprocess.run(["tar", "-xzf", str(matches[-1]), "-C", str(self.runtime)],
                           check=True, timeout=120)
            if self.instrumented:
                shutil.copy2(binary,
                             self.runtime / release_name(org) / "bin" / "chainmaker")
            sdk_conf = self.runtime / f"sdk-{org}.yml"
            write_sdk_config(self.runtime, sdk_conf, org=org)
            # write_sdk_config hardcodes chain_id "chainmaker", but the node
            # serves whatever chainId its chainmaker.yml declares (chain1 in
            # the 13-org release); bind the sdk to the node's real chain id
            # or every cmc call fails with "chain id chainmaker not found".
            node_cfg = (self.runtime / release_name(org) / "config"
                        / org_domain(org) / "chainmaker.yml")
            node_data = yaml.safe_load(node_cfg.read_text(encoding="utf-8")) or {}
            chains = node_data.get("blockchain") or []
            if chains:
                sdk_data = yaml.safe_load(
                    sdk_conf.read_text(encoding="utf-8")) or {}
                sdk_data.setdefault("chain_client", {})["chain_id"] = \
                    chains[0].get("chainId", "chainmaker")
                sdk_conf.write_text(
                    yaml.safe_dump(sdk_data, sort_keys=False),
                    encoding="utf-8")
            self.sdk_confs[org] = sdk_conf
        # arm the turbo+gas chainconfig that the CM_MALICIOUS_* capability
        # patches require to fire: the malicious cutBlock/index/TxCount
        # hooks live in the GetTurboBlock path, which only runs when
        # consensus_message_turbo=true AND enable_gas=true (PoC 07/08).
        # Without this the M-seeds restart the org with the env flag but
        # the corrupt-block code path is never reached — the stageG3
        # chainmaker leg ran 50 rounds with 0 panics for exactly this
        # reason (patch_bc1 was only wired into calibration mode).
        if self.instrumented:
            self.patch_bc1("turbo_gas")
        return self.runtime

    def org_bin_dir(self, org: str) -> Path:
        return self.runtime / release_name(org) / "bin"

    def start_all(self) -> None:
        for org in self.orgs:
            self.start_process(org, extra_env={})
        time.sleep(10)

    def start_process(self, org: str, extra_env: dict[str, str]) -> None:
        """Start one org with extra env (the M-corpus capability channel)."""
        bin_dir = self.org_bin_dir(org)
        log_path = bin_dir / "panic.log"
        log_path.unlink(missing_ok=True)
        env = chainmaker_env(self.runtime, org)
        env.update(extra_env)
        with log_path.open("ab") as log_fh:
            subprocess.Popen(
                ["./chainmaker", "start", "-c",
                 f"../config/{org_domain(org)}/chainmaker.yml"],
                cwd=bin_dir, stdout=log_fh, stderr=subprocess.STDOUT,
                env=env, start_new_session=True)

    def stop_all(self) -> None:
        kill_chainmaker_processes(self.runtime)

    def stop_org(self, org: str) -> None:
        kill_chainmaker_processes(self.runtime, org)

    def start_org(self, org: str, extra_env: dict[str, str] | None = None) -> bool:
        # re-apply any capability env armed by an M-seed this round so
        # that restart_cycle (which calls start_org with no env) does not
        # silently drop CM_MALICIOUS_* before the malicious org proposes
        # (stageG3 chainmaker smoke: round-3 restart_cycle wiped the flag
        # the round-1 M-seed had set, so the corrupt-block path never ran)
        env = dict(self._capability_env.get(org, {}))
        if extra_env:
            env.update(extra_env)
        self.start_process(org, env)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.alive(org):
                return True
            time.sleep(1)
        return False

    def restart_org_with_env(self, org: str, extra_env: dict[str, str]) -> bool:
        # record the capability flags so subsequent start_org calls (e.g.
        # restart_cycle) preserve them for the rest of the round
        self._capability_env[org] = dict(extra_env)
        self.stop_org(org)
        time.sleep(2)
        return self.start_org(org, extra_env)

    # ------------------------------------------------------------- observers

    def alive(self, org: str) -> bool:
        return node_running(self.runtime, org)

    def panic_log(self, org: str) -> str:
        path = self.org_bin_dir(org) / "panic.log"
        try:
            return path.read_text(errors="replace")
        except OSError:
            return ""

    def system_log(self, org: str) -> str:
        log_dir = self.runtime / release_name(org) / "log"
        text = ""
        for path in sorted(log_dir.glob("*.log")) if log_dir.is_dir() else []:
            try:
                text += path.read_text(errors="replace")
            except OSError:
                continue
        return text

    def cmc_capture_org(self, org: str, *args: str,
                        timeout: int = 30) -> tuple[bool, str]:
        return cmc_capture(self.sdk_confs[org], *args, timeout=timeout)

    def cmc_org(self, org: str, *args: str, timeout: int = 30) -> bool:
        return cmc(self.sdk_confs[org], *args, timeout=timeout)

    def height(self, org: str = "wx-org1") -> int:
        """Chain height via `cmc consensus height` (JSON output like
        {"Height": 1} — capital H)."""
        ok, out = self.cmc_capture_org(org, "consensus", "height")
        if not ok:
            return -1
        match = re.search(r'"Height"\s*:\s*"?(\d+)"?', out)
        return int(match.group(1)) if match else -1

    def current_proposer(self, org: str = "wx-org1") -> str | None:
        """Parse the proposer org id from `cmc consensus status`.

        Fallback: the latest block header records the proposer org id — the
        status output's shape varies across ChainMaker versions, and on the
        13-org TBFT release the primary parse kept returning None, which
        starved every M-corpus seed (their proposer precondition never
        matched in 115 rounds)."""
        ok, out = self.cmc_capture_org(org, "consensus", "status")
        if ok:
            for pattern in (r'"(?:proposer|leader)"\s*:\s*"?(wx-org\d+)[."]',
                            r"proposer\s*=\s*(wx-org\d+)",
                            r"(wx-org\d+\.chainmaker\.org)\s+.*(?:proposer|PROPOSING)"):
                match = re.search(pattern, out)
                if match:
                    return match.group(1).split(".")[0]
        height = self.height(org)
        if height > 0:
            ok2, out2 = self.cmc_capture_org(org, "query",
                                             "block-by-height", str(height))
            if ok2:
                match2 = re.search(r'"proposer"\s*:\s*"?([\w.-]+)"?', out2)
                if match2:
                    return match2.group(1).split(".")[0]
        return None

    def ensure_contracts(self, org: str) -> bool:
        """Deploy fact+counter once per runtime: the T-corpus invokes
        counter, and the genesis chainconfig ships no user contracts."""
        if getattr(self, "_contracts_deployed", False):
            return True
        from live_node_chainmaker import COUNTER_WASM, FACT_WASM

        def _create(name: str, wasm: Path) -> bool:
            ok, out = self.cmc_capture_org(
                org, "client", "contract", "user", "create",
                f"--contract-name={name}", "--runtime-type=WASMER",
                f"--byte-code-path={wasm}", "--version=1.0",
                "--sync-result=true", "--params={}", timeout=60)
            return _cmc_ok(out)

        self._contracts_deployed = (
            _create("fact", FACT_WASM) and _create("counter", COUNTER_WASM))
        return self._contracts_deployed

    def invoke(self, org: str, count: int, timeout: int = 30,
               gas_limit: int | None = None, sync: bool = True) -> int:
        if not self.ensure_contracts(org):
            return 0
        accepted = 0
        args = ["client", "contract", "user", "invoke",
                "--contract-name=counter", "--method=increase",
                "--params={}", f"--sync-result={str(sync).lower()}"]
        if gas_limit is not None:
            # PoC 07: on a gas-enabled chain every invoke must carry gas
            args.append(f"--gas-limit={gas_limit}")
        for _ in range(count):
            ok, out = self.cmc_capture_org(org, *args, timeout=timeout)
            if _cmc_ok(out):
                accepted += 1
            time.sleep(0.2)
        return accepted

    def set_pool_type(self, org: str, pool_type: str) -> None:
        """Per-org txpool.pool_type rewrite (PoC 06 pattern: batch victims)."""
        cfg = (self.runtime / release_name(org) / "config"
               / org_domain(org) / "chainmaker.yml")
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        data.setdefault("txpool", {})["pool_type"] = pool_type
        cfg.write_text(yaml.safe_dump(data, sort_keys=False),
                       encoding="utf-8")

    def patch_bc1(self, patch_name: str) -> None:
        """PoC-verified chainconfig patch on every org's bc1.yml
        (anchors confirmed present in the 13-org stash).

        "turbo"      = consensus message turbo only            (PoC 08)
        "turbo_gas"  = turbo + enable_gas + optimize_charge_gas (PoC 07)
        """
        for old, new in BC1_PATCHES[patch_name]:
            for org in self.orgs:
                cfg = (self.runtime / release_name(org) / "config"
                       / org_domain(org) / "chainconfig" / "bc1.yml")
                text = cfg.read_text(encoding="utf-8")
                assert old in text, \
                    f"bc1 anchor missing for {org}: {old[:60]!r}"
                cfg.write_text(text.replace(old, new, 1),
                               encoding="utf-8")

    def peers_connected(self, org: str) -> bool:
        return "all necessary peers connected" in self.system_log(org)

    def teardown(self) -> None:
        self.stop_all()
        shutil.rmtree(self.runtime, ignore_errors=True)


def main() -> int:
    """Smoke test (plan B3): 13-org release, start all, check height."""
    import argparse
    import tempfile
    parser = argparse.ArgumentParser(description="chainmaker 13-org smoke test")
    parser.add_argument("--skip-release", action="store_true",
                        help="assume /tmp/cm13-release already populated")
    args = parser.parse_args()
    if not args.skip_release:
        stash = build_13org_release()
        print(f"13-org tarballs stashed: {len(list(stash.glob('chainmaker-v3.0.0-wx-org*-x86_64.tar.gz')))}")
    runtime = Path(tempfile.mkdtemp(prefix="cm13-net-", dir="/tmp"))
    net = ChainMakerNetwork(runtime)
    try:
        net.prepare()
        print("runtime prepared")
        net.start_all()
        deadline = time.monotonic() + 120
        ready = False
        while time.monotonic() < deadline:
            if all(net.alive(o) for o in net.orgs):
                ready = True
                break
            time.sleep(2)
        print(f"all alive={ready}")
        if ready:
            time.sleep(20)
            print("height:", net.height())
            print("proposer:", net.current_proposer())
        return 0 if ready else 1
    finally:
        net.teardown()


if __name__ == "__main__":
    raise SystemExit(main())
