"""Target adapters for BCFuzzer configuration-interaction testing.

The adapters generate valid, type-preserving configuration variants together
with both normal and strong transaction / inter-node-message interactions.
Unit-mode interactions stay repository-local and coverage-oriented.  Live-mode
interactions lift selected strong operators into real private-cluster runs so
the full BCFuzzer can exercise the audited inter-node bug corpus.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from live_profiles import has_live_profile, run_live_profile

try:
    import yaml
except ImportError:  # pragma: no cover - BCFuzzer's original code already depends on PyYAML.
    yaml = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
SEEDED_TEST_ROOT = Path(__file__).resolve().with_name("seeded_tests")

GETH_PACKAGES = [
    "./core",
    "./miner",
    "./eth",
    "./eth/fetcher",
    "./core/txpool",
    "./core/txpool/blobpool",
    "./core/txpool/legacypool",
    "./core/txpool/txorder",
]
GETH_COVERPKG = "./..."
CHAINMAKER_PACKAGES = ["./module/core/common/scheduler", "./module/sync"]
CHAINMAKER_COVERPKG = "./module/..."


@dataclass(frozen=True)
class ConfigCase:
    name: str
    description: str
    source: str | None
    output_name: str
    format: str
    patches: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Interaction:
    name: str
    kind: str
    description: str


def adapter_cli() -> str:
    return str(Path(__file__).resolve().with_name("adapter_cli.py"))


def default_roots(workspace_root: Path = WORKSPACE_ROOT) -> dict[str, Path]:
    return {
        "geth": workspace_root / "go-ethereum",
        "chainmaker": workspace_root / "chainmaker-go",
        "fisco": workspace_root / "FISCO-BCOS",
        "aptos": workspace_root / "aptos-core",
    }


def command(name: str, argv: Sequence[str], timeout: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "argv": list(argv)}
    if timeout is not None:
        item["timeout_seconds"] = timeout
    return item


def common_env() -> dict[str, str]:
    return {
        "PYTHONHASHSEED": "{seed}",
        "BCFUZZER_RUN_DIR": "{run_dir}",
        "BCFUZZER_COVERAGE_DIR": "{coverage_dir}",
    }


def apply_config_command(target: str, target_root: Path, case: ConfigCase) -> dict[str, Any]:
    return command(
        "generate-valid-config",
        [
            sys.executable,
            adapter_cli(),
            "apply-config",
            "--target",
            target,
            "--target-root",
            str(target_root),
            "--case",
            case.name,
            "--run-dir",
            "{run_dir}",
            "--seed",
            "{seed}",
        ],
    )


def run_interaction_command(target: str, target_root: Path, interaction: Interaction,
                            execution_mode: str = "unit") -> dict[str, Any]:
    return command(
        f"run-{interaction.kind}-interaction",
        [
            sys.executable,
            adapter_cli(),
            "run-interaction",
            "--target",
            target,
            "--target-root",
            str(target_root),
            "--interaction",
            interaction.name,
            "--execution-mode",
            execution_mode,
            "--run-dir",
            "{run_dir}",
            "--coverage-dir",
            "{coverage_dir}",
            "--seed",
            "{seed}",
        ],
        timeout=180,
    )


class TargetAdapter:
    name = ""

    def __init__(self, target_root: Path):
        self.target_root = target_root.resolve()

    def config_cases(self) -> list[ConfigCase]:
        raise NotImplementedError

    def fixed_interaction(self) -> Interaction:
        raise NotImplementedError

    def varied_interactions(self) -> list[Interaction]:
        raise NotImplementedError

    def coverage_patterns(self) -> list[str]:
        return ["{coverage_dir}/**/*"]

    def manifest_target(self, execution_mode: str = "unit") -> dict[str, Any]:
        fixed = self.fixed_interaction()
        varied = self.varied_interactions()
        return {
            "name": self.name,
            "cwd": str(self.target_root),
            "env": common_env(),
            "common_commands": [
                command(
                    "check-target-root",
                    [
                        sys.executable,
                        adapter_cli(),
                        "check-target",
                        "--target",
                        self.name,
                        "--target-root",
                        str(self.target_root),
                    ],
                )
            ],
            "config_cases": [
                {
                    "name": case.name,
                    "commands": [apply_config_command(self.name, self.target_root, case)],
                }
                for case in self.config_cases()
            ],
            "fixed_interaction": {
                "name": fixed.name,
                "commands": [run_interaction_command(self.name, self.target_root, fixed, execution_mode)],
            },
            "varied_interactions": [
                {
                    "name": item.name,
                    "commands": [run_interaction_command(self.name, self.target_root, item, execution_mode)],
                }
                for item in varied
            ],
            "coverage": {
                "globs": self.coverage_patterns(),
                "metrics_file": "{coverage_dir}/summary.json",
            },
        }


class GethAdapter(TargetAdapter):
    name = "geth"

    def config_cases(self) -> list[ConfigCase]:
        return [
            ConfigCase(
                "miner-txpool-balanced",
                "Conservative miner and txpool limits for normal local private-chain traffic.",
                None,
                "geth.toml",
                "toml",
                (
                    {"path": "Eth.Miner.GasCeil", "value": 30_000_000},
                    {"path": "Eth.Miner.GasPrice", "value": 1_000_000},
                    {"path": "Eth.TxPool.GlobalSlots", "value": 2048},
                    {"path": "Node.P2P.NoDiscovery", "value": True},
                ),
            ),
            ConfigCase(
                "blobpool-constrained",
                "Valid blobpool capacity and price-bump settings that still accept ordinary blobs.",
                None,
                "geth.toml",
                "toml",
                (
                    {"path": "Eth.BlobPool.Datacap", "value": 1_073_741_824},
                    {"path": "Eth.BlobPool.PriceBump", "value": 100},
                    {"path": "Eth.TxPool.PriceBump", "value": 10},
                    {"path": "Node.P2P.MaxPeers", "value": 8},
                ),
            ),
        ]

    def fixed_interaction(self) -> Interaction:
        return Interaction("geth-fixed-recv-tx", "transaction", "One valid eth/69 transaction receive path.")

    def varied_interactions(self) -> list[Interaction]:
        return [
            Interaction("geth-send-tx", "transaction", "Outbound valid eth/69 transaction propagation."),
            Interaction("geth-legacypool-tx", "transaction", "Legacy pool admission, repricing, eviction, and nonce-gap paths."),
            Interaction("geth-txorder-tx", "transaction", "Price/nonce heap ordering across legacy and dynamic-fee transactions."),
            Interaction("geth-blobpool-tx", "transaction", "Blobpool replacement, capacity, and blob-count boundary paths."),
            Interaction("geth-validate-tx", "transaction", "Transaction validation boundaries for fee and nonce constraints."),
            Interaction("geth-propagate-tx", "message", "Multi-peer transaction propagation path."),
            Interaction("geth-recv-tx68", "message", "Legacy eth/68 transaction receive/send message path."),
            Interaction("geth-build-payload", "message", "Payload-building message path through miner."),
            Interaction("geth-blob-fetch", "message", "Blob fetch and delivery path with valid metadata."),
            Interaction("geth-blob-network", "message", "Blob request/response network transport path."),
            Interaction("geth-mut-legacypool", "transaction", "Seeded strong legacy-tx mutation: price, nonce, size, signature, and pool flood."),
            Interaction("geth-mut-txorder", "transaction", "Seeded strong ordering mutation across many senders."),
            Interaction("geth-mut-ethmsg", "message", "Seeded strong devp2p transaction-message mutation."),
            Interaction("geth-mut-blobpool", "transaction", "Seeded strong blob-tx mutation across fee, count, and replacement boundaries."),
            Interaction("geth-engine-gaslimit-collapse", "message", "Fake-consensus payload sequence that drives inter-node gas-limit drift."),
            Interaction("geth-blob-replacement-stickiness", "transaction", "Strong blob replacement mutation against a malicious blobpool price-bump policy."),
        ]


class ChainMakerAdapter(TargetAdapter):
    name = "chainmaker"

    def config_cases(self) -> list[ConfigCase]:
        source = find_first(
            self.target_root,
            [
                "config/wx-org1/chainmaker.yml",
                "build/backup/backup_config/*/node1/chainmaker.yml",
            ],
        )
        return [
            ConfigCase(
                "txpool-normal-small-batch",
                "Normal txpool with smaller valid batches to exercise transaction admission and sync.",
                source,
                "chainmaker.yml",
                "yaml",
                (
                    {"path": "txpool.pool_type", "value": "normal"},
                    {"path": "txpool.max_txpool_size", "value": 2048},
                    {"path": "txpool.common_queue_num", "value": 8},
                    {"path": "txpool.batch_max_size", "value": 50},
                    {"path": "net.listen_addr", "value": "/ip4/127.0.0.1/tcp/11301"},
                ),
            ),
            ConfigCase(
                "rpc-ratelimit-enabled",
                "Valid RPC token-bucket limits while keeping localhost-only node communication.",
                source,
                "chainmaker.yml",
                "yaml",
                (
                    {"path": "rpc.ratelimit.enabled", "value": True},
                    {"path": "rpc.ratelimit.token_per_second", "value": 1000},
                    {"path": "rpc.ratelimit.token_bucket_size", "value": 2000},
                    {"path": "rpc.check_chain_conf_trust_roots_change_interval", "value": 30},
                    {"path": "net.listen_addr", "value": "/ip4/127.0.0.1/tcp/11301"},
                ),
            ),
        ]

    def fixed_interaction(self) -> Interaction:
        return Interaction("chainmaker-fixed-invoke-status", "transaction", "Invoke transaction plus node-status sync message.")

    def varied_interactions(self) -> list[Interaction]:
        return [
            Interaction("chainmaker-install-contract", "transaction", "Valid install contract transaction gas path."),
            Interaction("chainmaker-multisign", "transaction", "Valid multisign transaction gas path."),
            Interaction("chainmaker-scheduler-tx", "transaction", "Scheduler execution and DAG/gas bookkeeping path."),
            Interaction("chainmaker-sync-block-req", "message", "Structured block-sync request message path."),
            Interaction("chainmaker-sync-node-status-resp", "message", "Structured node-status response path."),
            Interaction("chainmaker-sync-msg", "message", "Structured sync-service message family across liveness and scheduler branches."),
            Interaction("chainmaker-mut-scheduler", "transaction", "Seeded strong scheduler mutation across gas, params, tx ids, and mixed batches."),
            Interaction("chainmaker-mut-sync", "message", "Seeded strong sync-message mutation across heights, corrupt payloads, and wrong message kinds."),
            Interaction("chainmaker-governance-tbft-timeout", "transaction", "Governance mutation that sets an extreme TBFT propose timeout."),
            Interaction("chainmaker-governance-tbft-timeout-negative", "transaction", "Governance mutation that sets a negative TBFT propose timeout."),
            Interaction("chainmaker-governance-maxbft-timeout", "transaction", "Governance mutation that drives a MaxBFT timeout storm."),
            Interaction("chainmaker-governance-tbft-delta-negative", "transaction", "Governance mutation that sets a negative TBFT propose delta timeout."),
            Interaction("chainmaker-malicious-batch-index", "message", "Malicious proposer mutation that corrupts batch metadata indices."),
            Interaction("chainmaker-malicious-txcount", "message", "Malicious proposer mutation that inflates TxCount across peers."),
            Interaction("chainmaker-malicious-nil-payload", "message", "Malicious proposer mutation that injects nil-payload transactions."),
            Interaction("chainmaker-batch-turbo-crash", "message", "One-node batch-pool skew plus turbo/gas that crashes verifiers."),
            Interaction("chainmaker-gas-enable-paralysis", "transaction", "Governance-enabled gas path that paralyzes user transactions."),
        ]


class FiscoAdapter(TargetAdapter):
    name = "fisco"

    def config_cases(self) -> list[ConfigCase]:
        source = find_first(
            self.target_root,
            [
                "nodes/127.0.0.1/node0/config.ini",
                "tools/BcosAirBuilder/src/tpl/config.ini",
            ],
        )
        genesis = find_first(
            self.target_root,
            [
                "nodes/127.0.0.1/node0/config.genesis",
                "tools/BcosAirBuilder/src/tpl/config.genesis",
            ],
        )
        return [
            ConfigCase(
                "txpool-sync-tree",
                "Valid txpool capacity with tree-based transaction and block sync enabled.",
                source,
                "config.ini",
                "ini",
                (
                    {"path": "p2p.listen_ip", "value": "127.0.0.1"},
                    {"path": "rpc.listen_ip", "value": "127.0.0.1"},
                    {"path": "txpool.limit", "value": 8000},
                    {"path": "txpool.txs_expiration_time", "value": 300},
                    {"path": "sync.send_txs_by_tree", "value": True},
                    {"path": "sync.sync_block_by_tree", "value": True},
                    {"path": "sync.tree_width", "value": 2},
                ),
            ),
            ConfigCase(
                "consensus-executor-balanced",
                "Valid consensus and executor settings for normal PBFT transaction handling.",
                genesis,
                "config.genesis",
                "ini",
                (
                    {"path": "consensus.block_tx_count_limit", "value": 500},
                    {"path": "consensus.consensus_timeout", "value": 4000},
                    {"path": "consensus.leader_period", "value": 1},
                    {"path": "executor.is_serial_execute", "value": True},
                    {"path": "tx.gas_limit", "value": 1_000_000_000},
                ),
            ),
        ]

    def fixed_interaction(self) -> Interaction:
        return Interaction("fisco-fixed-front-message", "message", "One valid front-service message serialization path.")

    def varied_interactions(self) -> list[Interaction]:
        return [
            Interaction("fisco-txpool-transaction", "transaction", "Valid txpool transaction admission path."),
            Interaction("fisco-scheduler-transaction", "transaction", "Valid scheduler transaction execution path."),
            Interaction("fisco-executor-transaction", "transaction", "Transaction executor and receipt generation paths."),
            Interaction("fisco-ledger-tx", "transaction", "Ledger, cache, and storage transaction paths."),
            Interaction("fisco-gateway-message", "message", "Valid gateway message encoding and routing path."),
            Interaction("fisco-pbft-message", "message", "PBFT/rPBFT consensus message path."),
            Interaction("fisco-sync-message", "message", "Transaction/block sync message path."),
            Interaction("fisco-front-service", "message", "Front-service encode/decode and routing path."),
            Interaction("fisco-rpc-message", "message", "RPC/Web3 message and validator path."),
            Interaction("fisco-codec-crypto", "transaction", "ABI/codec/crypto data-path exercised by transaction materials."),
            Interaction("fisco-table-storage", "transaction", "Table/state storage update paths."),
            Interaction("fisco-utilities-tool", "message", "Utilities/rate-limit/protocol tooling paths."),
            Interaction("fisco-mut-front-message", "message", "Seeded strong front-message mutation across size, corruption, and field extremes."),
            Interaction("fisco-min-seal-time-drift", "message", "One-node min_seal_time skew that degrades global consensus progress."),
            Interaction("fisco-invalid-signature-acceptance", "transaction", "Strong invalid-signature transaction injection against relaxed validation."),
            Interaction("fisco-expired-blocklimit-acceptance", "transaction", "Strong expired block-limit transaction injection against relaxed validation."),
            Interaction("fisco-chain-block-limit-collapse", "message", "Genesis block-limit skew that starves a future leader."),
        ]


class AptosAdapter(TargetAdapter):
    name = "aptos"

    def config_cases(self) -> list[ConfigCase]:
        source = find_first(
            self.target_root,
            [
                "config/src/config/test_data/validator.yaml",
                "docker/compose/aptos-node/validator.yaml",
            ],
        )
        return [
            ConfigCase(
                "mempool-validator-local",
                "Validator network config with localhost listeners and bounded mempool message size.",
                source,
                "validator.yaml",
                "yaml",
                (
                    {"path": "validator_network.listen_address", "value": "/ip4/127.0.0.1/tcp/6180"},
                    {"path": "validator_network.max_frame_size", "value": 4_194_304},
                    {"path": "validator_network.mutual_authentication", "value": True},
                    {"path": "api.enabled", "value": True},
                ),
            ),
            ConfigCase(
                "fullnode-forwarding-local",
                "Validator-fullnode network settings for local transaction forwarding tests.",
                source,
                "validator.yaml",
                "yaml",
                (
                    {"path": "full_node_networks.0.listen_address", "value": "/ip4/127.0.0.1/tcp/6181"},
                    {"path": "full_node_networks.0.max_outbound_connections", "value": 1},
                    {"path": "validator_network.listen_address", "value": "/ip4/127.0.0.1/tcp/6180"},
                    {"path": "api.enabled", "value": True},
                ),
            ),
        ]

    def fixed_interaction(self) -> Interaction:
        return Interaction("aptos-fixed-broadcast-self", "transaction", "One valid mempool self-broadcast transaction path.")

    def varied_interactions(self) -> list[Interaction]:
        return [
            Interaction("aptos-validator-forward", "transaction", "Validator-to-validator transaction forwarding path."),
            Interaction("aptos-inbound-tx", "transaction", "Inbound validator transaction handling path."),
            Interaction("aptos-vfn-forward", "transaction", "Validator fullnode transaction forwarding path."),
            Interaction("aptos-ready-tx", "transaction", "Ready-queue transaction handling path."),
            Interaction("aptos-gas-price", "transaction", "Gas-price update and prioritization path."),
            Interaction("aptos-to-val", "transaction", "Fullnode-to-validator forwarding path."),
            Interaction("aptos-ack-retry", "message", "Mempool acknowledgement and retry message path."),
            Interaction("aptos-sync-interrupt", "message", "Interrupted mempool sync inbound path."),
            Interaction("aptos-rebroadcast", "message", "Rebroadcast and retry-empty message path."),
            Interaction("aptos-commit-removal", "message", "Commit notification and mempool removal path."),
            Interaction("aptos-parking-lot", "message", "Parking-lot inspection/removal path."),
            Interaction("aptos-mut-txs", "transaction", "Seeded strong transaction mutation across sequence, gas price, and script size."),
            Interaction("aptos-round-timeout-zero", "message", "Consensus timeout-zero liveness mutation profile."),
            Interaction("aptos-sync-only", "message", "Consensus sync-only liveness mutation profile."),
            Interaction("aptos-safety-rules-dead", "message", "Consensus safety-rules process misconfiguration profile."),
        ]


ADAPTERS: dict[str, type[TargetAdapter]] = {
    "geth": GethAdapter,
    "chainmaker": ChainMakerAdapter,
    "fisco": FiscoAdapter,
    "aptos": AptosAdapter,
}


def get_adapter(target: str, target_root: Path | None = None) -> TargetAdapter:
    if target not in ADAPTERS:
        raise KeyError(f"unknown target: {target}")
    root = target_root if target_root is not None else default_roots()[target]
    return ADAPTERS[target](root)


def build_manifest(targets: Iterable[str], workspace_root: Path, repeats: int,
                   seed: int, timeout_seconds: int,
                   execution_mode: str = "unit") -> dict[str, Any]:
    roots = default_roots(workspace_root)
    selected = []
    for target in targets:
        adapter = get_adapter(target, roots[target])
        selected.append(adapter.manifest_target(execution_mode))
    return {
        "version": 1,
        "seed": seed,
        "repeats": repeats,
        "timeout_seconds": timeout_seconds,
        "targets": selected,
    }


def find_first(root: Path, patterns: Sequence[str]) -> str | None:
    for pattern in patterns:
        matches = sorted(root.glob(pattern))
        if matches:
            return str(matches[-1])
    return None


def apply_case(target: str, target_root: Path, case_name: str, run_dir: Path,
               seed: int) -> dict[str, Any]:
    adapter = get_adapter(target, target_root)
    cases = {case.name: case for case in adapter.config_cases()}
    if case_name not in cases:
        raise ValueError(f"unknown config case {case_name!r} for target {target!r}")
    case = cases[case_name]
    config_dir = run_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    output_path = config_dir / case.output_name
    if case.source is not None and Path(case.source).is_file():
        source_path = Path(case.source)
        shutil.copy2(source_path, output_path)
    else:
        source_path = None
        write_default_config(target, case.format, output_path)
    apply_patches(output_path, case.format, case.patches)
    metadata = {
        "target": target,
        "case": case.name,
        "description": case.description,
        "seed": seed,
        "source": str(source_path) if source_path else None,
        "output": str(output_path),
        "format": case.format,
        "patches": list(case.patches),
    }
    (run_dir / "config_patch.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def write_default_config(target: str, fmt: str, output_path: Path) -> None:
    if fmt == "toml":
        output_path.write_text(default_geth_toml(), encoding="utf-8")
    elif fmt == "ini":
        parser = ConfigParser()
        if target == "fisco":
            parser["p2p"] = {"listen_ip": "127.0.0.1", "listen_port": "30300"}
            parser["rpc"] = {"listen_ip": "127.0.0.1", "listen_port": "20200", "enable_ssl": "false"}
            parser["txpool"] = {"limit": "15000", "txs_expiration_time": "600"}
            parser["sync"] = {"send_txs_by_tree": "false", "sync_block_by_tree": "false", "tree_width": "3"}
            parser["consensus"] = {"block_tx_count_limit": "1000", "consensus_timeout": "3000", "leader_period": "1"}
            parser["executor"] = {"is_serial_execute": "true"}
            parser["tx"] = {"gas_limit": "3000000000"}
        with output_path.open("w", encoding="utf-8") as handle:
            parser.write(handle)
    elif fmt == "yaml":
        if yaml is None:
            raise RuntimeError("PyYAML is required for YAML configuration generation")
        output_path.write_text("{}\n", encoding="utf-8")
    else:
        raise ValueError(f"unsupported config format: {fmt}")


def default_geth_toml() -> str:
    return """[Eth]
NetworkId = 1337
SyncMode = "full"

[Eth.Miner]
GasCeil = 30000000
GasPrice = 1000000
Recommit = 2000000000

[Eth.TxPool]
PriceLimit = 1
PriceBump = 10
GlobalSlots = 2048
GlobalQueue = 1024
Lifetime = 10800000000000

[Eth.BlobPool]
Datadir = "blobpool"
Datacap = 1073741824
PriceBump = 100

[Node]
DataDir = "data"
HTTPHost = "127.0.0.1"
HTTPPort = 8545
AuthAddr = "127.0.0.1"
AuthPort = 8551

[Node.P2P]
MaxPeers = 8
NoDiscovery = true
ListenAddr = "127.0.0.1:30303"
"""


def apply_patches(path: Path, fmt: str, patches: Sequence[Mapping[str, Any]]) -> None:
    if fmt == "yaml":
        if yaml is None:
            raise RuntimeError("PyYAML is required for YAML configuration generation")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for patch in patches:
            set_nested_value(data, str(patch["path"]).split("."), patch["value"])
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    elif fmt == "ini":
        parser = ConfigParser()
        parser.optionxform = str
        parser.read(path, encoding="utf-8")
        for patch in patches:
            section, option = str(patch["path"]).split(".", 1)
            if not parser.has_section(section):
                parser.add_section(section)
            parser.set(section, option, ini_value(patch["value"]))
        with path.open("w", encoding="utf-8") as handle:
            parser.write(handle)
    elif fmt == "toml":
        rendered = path.read_text(encoding="utf-8")
        for patch in patches:
            rendered = set_toml_scalar(rendered, str(patch["path"]), patch["value"])
        path.write_text(rendered, encoding="utf-8")
    else:
        raise ValueError(f"unsupported config format: {fmt}")


def set_nested_value(data: Any, parts: Sequence[str], value: Any) -> None:
    current = data
    for part in parts[:-1]:
        if part.isdigit():
            current = current[int(part)]
            continue
        if not isinstance(current, dict):
            raise ValueError(f"cannot descend through non-dict path component {part!r}")
        current = current.setdefault(part, {})
    leaf = parts[-1]
    if leaf.isdigit():
        current[int(leaf)] = value
    else:
        current[leaf] = value


def ini_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def set_toml_scalar(text: str, dotted_path: str, value: Any) -> str:
    parts = dotted_path.split(".")
    section, key = ".".join(parts[:-1]), parts[-1]
    section_header = f"[{section}]"
    lines = text.splitlines()
    current_section = ""
    insert_at = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current_section == section:
                insert_at = index
            current_section = stripped[1:-1]
            continue
        if current_section == section and re.match(rf"^\s*{re.escape(key)}\s*=", line):
            lines[index] = f"{key} = {toml_value(value)}"
            return "\n".join(lines) + "\n"
    if section_header not in [line.strip() for line in lines]:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([section_header, f"{key} = {toml_value(value)}"])
    else:
        lines.insert(insert_at, f"{key} = {toml_value(value)}")
    return "\n".join(lines) + "\n"


def interaction_names(target: str) -> set[str]:
    adapter = get_adapter(target)
    return {adapter.fixed_interaction().name, *(item.name for item in adapter.varied_interactions())}


def seeded_overlay_mapping(target: str) -> dict[str, str]:
    if target == "geth":
        root = default_roots()["geth"]
        return {
            str(root / "core/txpool/legacypool/zz_bcmut_test.go"): str(SEEDED_TEST_ROOT / "geth-legacypool.go"),
            str(root / "core/txpool/blobpool/zz_bcmut_test.go"): str(SEEDED_TEST_ROOT / "geth-blobpool.go"),
            str(root / "core/txpool/txorder/zz_bcmut_test.go"): str(SEEDED_TEST_ROOT / "geth-txorder.go"),
            str(root / "eth/zz_bcmut_test.go"): str(SEEDED_TEST_ROOT / "geth-ethmsg.go"),
        }
    if target == "chainmaker":
        root = default_roots()["chainmaker"]
        return {
            str(root / "module/core/common/scheduler/zz_bcmut_test.go"): str(SEEDED_TEST_ROOT / "chainmaker-scheduler.go"),
            str(root / "module/sync/zz_bcmut_test.go"): str(SEEDED_TEST_ROOT / "chainmaker-sync.go"),
        }
    raise KeyError(f"no seeded overlay mapping for target {target!r}")


def ensure_seeded_overlay(target: str, coverage_dir: Path) -> Path:
    overlay_path = coverage_dir / f"{target}-overlay.json"
    overlay_path.write_text(
        json.dumps({"Replace": seeded_overlay_mapping(target)}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return overlay_path


def go_binary_for(target: str) -> str:
    if target == "geth":
        cached = Path("/home/geth/go/pkg/mod/golang.org/toolchain@v0.0.1-go1.24.0.linux-amd64/bin/go")
        if cached.is_file():
            return str(cached)
    return "go"


def run_interaction(target: str, target_root: Path, interaction: str, run_dir: Path,
                    coverage_dir: Path, seed: int, execution_mode: str = "unit") -> dict[str, Any]:
    coverage_dir.mkdir(parents=True, exist_ok=True)
    if execution_mode == "live" and has_live_profile(interaction):
        result = run_live_profile(interaction, run_dir, coverage_dir, seed)
    elif target == "geth":
        result = run_geth_coverage(target_root, interaction, coverage_dir)
    elif target == "chainmaker":
        result = run_chainmaker_coverage(target_root, interaction, coverage_dir)
    elif target == "fisco":
        result = run_fisco_coverage(target_root, interaction, coverage_dir)
    elif target == "aptos":
        result = run_aptos_coverage(target_root, interaction, coverage_dir)
    else:
        raise ValueError(f"unknown target: {target}")
    result.update({
        "target": target,
        "interaction": interaction,
        "seed": seed,
        "run_dir": str(run_dir),
        "execution_mode": execution_mode,
    })
    (coverage_dir / "summary.json").write_text(
        json.dumps(result.get("metrics", {}), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (coverage_dir / "interaction_result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def run_geth_coverage(target_root: Path, interaction: str, coverage_dir: Path) -> dict[str, Any]:
    tests = {
        "geth-fixed-recv-tx": "^(TestCalcGasLimit|TestBuildPayload|TestRecvTransactions69)$",
        "geth-send-tx": "^(TestCalcGasLimit|TestBuildPayload|TestRecvTransactions69|TestSendTransactions69)$",
        "geth-legacypool-tx": "^(TestQueue|TestQueue2|TestRepricing|TestReplacement|TestReplacementDynamicFee|TestInvalidTransactions|TestMissingNonce|TestDropping|TestDeduplication|TestDoubleNonce|TestNegativeValue|TestNonceRecovery|TestUnderpricing|TestTipAboveFeeCap|TestMinGasPriceEnforced|TestPendingGlobalLimiting|TestPendingMinimumAllowance|TestGapFilling|TestSetCodeTransactions|TestSetCodeTransactionsReorg|TestVeryHighValues|TestStateChangeDuringReset|TestStableUnderpricing|TestSlotCount|TestAllowedTxSize|TestChainFork|TestPostponing)$",
        "geth-txorder-tx": "^(TestTransactionPriceNonceSortLegacy|TestTransactionPriceNonceSort1559|TestTransactionTimeSort|TestCalcGasLimit|TestBuildPayload|TestRecvTransactions69)$",
        "geth-blobpool-tx": "^(TestAdd|TestOpenCap|TestOpenHeap|TestOpenIndex|TestDualHeapEviction|TestNewSlotterEIP7594|TestChangingSlotterSize|TestBlobCountLimit|TestCapClearsFromAll|TestPendingGlobalLimiting|TestPendingLimiting|TestQueueGlobalLimiting|TestQueueAccountLimiting|TestQueueTimeLimiting|TestPriorityCalculation|TestPriceHeapSorting|TestMissingNonce|TestRepricing|TestRepricingDynamicFee)$",
        "geth-validate-tx": "^(TestValidateTransactionEIP2681|TestCalcGasLimit|TestBuildPayload|TestRecvTransactions69)$",
        "geth-propagate-tx": "^(TestCalcGasLimit|TestBuildPayload|TestRecvTransactions69|TestTransactionPropagation69)$",
        "geth-recv-tx68": "^(TestRecvTransactions68|TestSendTransactions68|TestForkIDSplit69|TestCalcGasLimit|TestBuildPayload)$",
        "geth-build-payload": "^(TestCalcGasLimit|TestBuildPayload|TestRecvTransactions69|TestBuildPayloadAmsterdamTransition)$",
        "geth-blob-fetch": "^(TestCalcGasLimit|TestBuildPayload|TestRecvTransactions69|TestBlobFetcherFullDelivery|TestBlobFetcherPartialDelivery|TestBlobFetcherFullFetch)$",
        "geth-blob-network": "^(TestBroadcastChoice|TestCacheGetBlobs|TestEncodeForNetwork|TestGetBlobs|TestCalcGasLimit|TestBuildPayload|TestRecvTransactions69)$",
        "geth-mut-legacypool": ("^TestBcMutLegacyTx$", "geth", False, 600),
        "geth-mut-txorder": ("^TestBcMutTxOrder$", "geth", False, 600),
        "geth-mut-ethmsg": ("^TestBcMutMessages$", "geth", False, 600),
        "geth-mut-blobpool": ("^TestBcMutBlobTx$", "geth", False, 900),
        "geth-engine-gaslimit-collapse": ("^TestBcMutMessages$", "geth", False, 600),
        "geth-blob-replacement-stickiness": ("^TestBcMutBlobTx$", "geth", False, 900),
    }
    spec = tests[interaction]
    if isinstance(spec, tuple):
        regex, overlay_target, vet_off, timeout = spec
        overlay = ensure_seeded_overlay(str(overlay_target), coverage_dir)
        return run_go_coverage("geth", target_root, interaction, regex, GETH_PACKAGES, GETH_COVERPKG,
                               coverage_dir, overlay=overlay, vet_off=vet_off, timeout=timeout)
    return run_go_coverage("geth", target_root, interaction, spec, GETH_PACKAGES, GETH_COVERPKG, coverage_dir)


def run_chainmaker_coverage(target_root: Path, interaction: str, coverage_dir: Path) -> dict[str, Any]:
    tests = {
        "chainmaker-fixed-invoke-status": "^(TestCalcInvokeTxGasUsed|TestSyncMsg_NODE_STATUS_REQ)$",
        "chainmaker-install-contract": "^(TestCalcInvokeTxGasUsed|TestCalcInstallTxGasUsed|TestSyncMsg_NODE_STATUS_REQ)$",
        "chainmaker-multisign": "^(TestCalcInvokeTxGasUsed|TestCalcMultiSignTxGasUsed|TestSyncMsg_NODE_STATUS_REQ)$",
        "chainmaker-scheduler-tx": "^(TestSchedule|TestSchedule2|TestSchedule3|TestSchedule4|TestSchedule5|TestSimulateWithDag|TestMarshalDag|TestCheckCycleExists|TestNewSenderGroup|TestTxScheduler_chargeGasLimit|TestTxScheduler_verifyExecOrderTxType|TestVerifyOptimizeChargeGasTx_OK|TestVerifyOptimizeChargeGasTx_WithWrongGas|TestVerifyOptimizeChargeGasTx_WithWrongAccountAddress|TestUint64Overflow|Test_errResult|Test_getSenderHashKey|Test_getSenderTxsMap|TestConflictsBitWindow_Enqueue|TestNewConflictsBitWindow|TestPublicKeyToAddress|Test_publicKeyFromCert|Test_wholeCertInfo|TestIsNativeContract|TestTxScheduler_dumpDAG|TestTxScheduler_checkGasEnable|TestTxScheduler_refundGas|TestTxScheduler_getPayerPk|TestTxScheduler_getAccountMgrContractAndPk)$",
        "chainmaker-sync-block-req": "^(TestCalcInvokeTxGasUsed|TestSyncBlock_Req|TestSyncMsg_NODE_STATUS_REQ)$",
        "chainmaker-sync-node-status-resp": "^(TestCalcInvokeTxGasUsed|TestSyncMsg_NODE_STATUS_RESP|TestSyncMsg_NODE_STATUS_REQ)$",
        "chainmaker-sync-msg": "^(TestLivenessMsg|TestNodeStatusMsg|TestSyncedBlockMsg|TestProcessorProcessBlockMsg|TestProcessorReceivedBlocks|TestProcessedBlockResp|TestAddPendingBlocksAndUpdatePendingHeight|TestNextHeightToReq|TestDataDetection|TestAddTask|TestPriority|TestIsNeedSync|TestStopSyncBlock|TestBlockChainSyncServer_Start|TestSchedulerMsg|TestSchedulerFlow|TestCalcInvokeTxGasUsed|TestSyncMsg_NODE_STATUS_REQ)$",
        "chainmaker-mut-scheduler": ("^TestBcMutSchedulerTx$", "chainmaker", True, 600),
        "chainmaker-mut-sync": ("^TestBcMutSyncMsg$", "chainmaker", True, 600),
        "chainmaker-governance-tbft-timeout": ("^TestBcMutSchedulerTx$", "chainmaker", True, 600),
        "chainmaker-governance-tbft-timeout-negative": ("^TestBcMutSchedulerTx$", "chainmaker", True, 600),
        "chainmaker-governance-maxbft-timeout": ("^TestBcMutSyncMsg$", "chainmaker", True, 600),
        "chainmaker-governance-tbft-delta-negative": ("^TestBcMutSchedulerTx$", "chainmaker", True, 600),
        "chainmaker-malicious-batch-index": ("^TestBcMutSyncMsg$", "chainmaker", True, 600),
        "chainmaker-malicious-txcount": ("^TestBcMutSyncMsg$", "chainmaker", True, 600),
        "chainmaker-malicious-nil-payload": ("^TestBcMutSyncMsg$", "chainmaker", True, 600),
        "chainmaker-batch-turbo-crash": ("^TestBcMutSyncMsg$", "chainmaker", True, 600),
        "chainmaker-gas-enable-paralysis": ("^TestBcMutSchedulerTx$", "chainmaker", True, 600),
    }
    spec = tests[interaction]
    if isinstance(spec, tuple):
        regex, overlay_target, vet_off, timeout = spec
        overlay = ensure_seeded_overlay(str(overlay_target), coverage_dir)
        return run_go_coverage("chainmaker", target_root, interaction, regex, CHAINMAKER_PACKAGES,
                               CHAINMAKER_COVERPKG, coverage_dir,
                               overlay=overlay, vet_off=vet_off, timeout=timeout)
    return run_go_coverage("chainmaker", target_root, interaction, spec,
                           CHAINMAKER_PACKAGES, CHAINMAKER_COVERPKG, coverage_dir)


def run_go_coverage(target: str, target_root: Path, interaction: str, test_regex: str,
                    packages: Sequence[str], coverpkg: str, coverage_dir: Path,
                    overlay: Path | None = None, vet_off: bool = False,
                    timeout: int = 180, seed: int | None = None) -> dict[str, Any]:
    profile = coverage_dir / f"{interaction}.cover"
    cache_dir = coverage_dir / "gocache"
    tmp_dir = coverage_dir / "tmp"
    cache_dir.mkdir(exist_ok=True)
    tmp_dir.mkdir(exist_ok=True)
    argv = [
        go_binary_for(target),
        "test",
        "-count=1",
        "-run",
        test_regex,
        "-covermode=count",
        f"-coverpkg={coverpkg}",
        f"-coverprofile={profile}",
    ]
    if overlay is not None:
        argv.extend(["-overlay", str(overlay)])
    if vet_off:
        argv.append("-vet=off")
    argv.extend(packages)
    env = os.environ.copy()
    env.update({
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOCACHE": str(cache_dir),
        "TMPDIR": str(tmp_dir),
    })
    if seed is not None:
        env["BCFUZZER_SEED"] = str(seed)
    completed = subprocess.run(
        argv,
        cwd=target_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    (coverage_dir / "go-test.stdout.log").write_text(completed.stdout, encoding="utf-8", errors="replace")
    (coverage_dir / "go-test.stderr.log").write_text(completed.stderr, encoding="utf-8", errors="replace")
    result: dict[str, Any] = {
        "status": "success" if completed.returncode == 0 else "failed",
        "argv": argv,
        "returncode": completed.returncode,
        "metrics": {},
    }
    if completed.returncode != 0:
        return result
    metrics = parse_go_cover_profile(profile)
    func_metrics = parse_go_func_metrics(go_binary_for(target), target_root, profile, coverage_dir)
    metrics.update(func_metrics)
    result["metrics"] = metrics
    return result


def parse_go_cover_profile(profile: Path) -> dict[str, float]:
    segments: dict[str, tuple[int, bool]] = {}
    for line in profile.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("mode:"):
            continue
        fields = line.split()
        if len(fields) != 3:
            continue
        location = fields[0]
        statements = int(fields[1])
        count = int(fields[2])
        old_statements, old_covered = segments.get(location, (statements, False))
        segments[location] = (old_statements, old_covered or count > 0)
    total = sum(statements for statements, _ in segments.values())
    covered = sum(statements for statements, is_covered in segments.values() if is_covered)
    pct = (covered / total * 100.0) if total else 0.0
    return {
        "covered_statements": float(covered),
        "total_statements": float(total),
        "statement_coverage_pct": pct,
    }


def parse_go_func_metrics(go_bin: str, target_root: Path, profile: Path, coverage_dir: Path) -> dict[str, float]:
    argv = [go_bin, "tool", "cover", f"-func={profile}"]
    completed = subprocess.run(
        argv,
        cwd=target_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    (coverage_dir / "go-cover-func.stdout.log").write_text(completed.stdout, encoding="utf-8", errors="replace")
    (coverage_dir / "go-cover-func.stderr.log").write_text(completed.stderr, encoding="utf-8", errors="replace")
    total_functions = 0
    covered_functions = 0
    for line in completed.stdout.splitlines():
        if not line or line.startswith("total:"):
            continue
        percent = line.rsplit(None, 1)[-1]
        if percent.endswith("%"):
            total_functions += 1
            try:
                if float(percent[:-1]) > 0:
                    covered_functions += 1
            except ValueError:
                pass
    pct = (covered_functions / total_functions * 100.0) if total_functions else 0.0
    return {
        "covered_functions": float(covered_functions),
        "total_functions": float(total_functions),
        "function_coverage_pct": pct,
    }


def run_fisco_coverage(target_root: Path, interaction: str, coverage_dir: Path) -> dict[str, Any]:
    build_dir = discover_fisco_build_dir(target_root)
    result: dict[str, Any] = {
        "status": "not_executed",
        "reason": "FISCO coverage build or executable test target is unavailable",
        "build_dir": str(build_dir) if build_dir else None,
        "metrics": {},
    }
    if build_dir is None:
        return result
    regexes = {
        "fisco-fixed-front-message": "FrontMessageTest/.*",
        "fisco-gateway-message": "GatewayMessageTest/.*|GatewayConfigTest/.*",
        "fisco-txpool-transaction": "TxPool.*|Transaction.*",
        "fisco-scheduler-transaction": "Scheduler.*|BlockExecutive.*",
        "fisco-executor-transaction": "TestTransactionExecutor/.*|TransactionExecutorImpl/.*|testTransactionExecutive/.*|Web3TransactionsTest/.*",
        "fisco-ledger-tx": "LedgerTest/.*|LedgerImplTest/.*|LedgerCacheTest/.*",
        "fisco-pbft-message": "PBFTMessageTest/.*|PBFTEngineTest/.*|PBFTConfigTest/.*|PBFTViewChangeTest/.*|test-rpbft",
        "fisco-sync-message": "TxsSyncMsgTest/.*|txsSyncTest/.*|test-sync|TestShardingSyncStorageWrapper/.*",
        "fisco-front-service": "FrontServiceTest/.*",
        "fisco-rpc-message": "testRPC/.*|testRpcValidator/.*|testWeb3RPC/.*|testWeb3Subscribe/.*|testWeb3Type/.*|Web3NonceTest/.*|Web3TransactionsTest/.*|EventSubTest/.*",
        "fisco-codec-crypto": "ContractABICodecTest/.*|ContractABITypeCodecTest/.*|ContractABITypeTest/.*|ContractABIDefinitionTest/.*|Base64/.*|CommonDataTests/.*|FixedBytes/.*|VectorRefTests/.*",
        "fisco-table-storage": "KeyPageStorageTest/.*|precompiledTableTest/.*|precompiledKVTableTest/.*|StateStorageTest/.*|TestMemoryStorage/.*|TestRocksDBStorage/.*|LegacyStorageTest/.*|TestRollbackableStorage/.*|AccountPrecompiledTest/.*|EntryTest/.*|ContractShardUtilsTest/.*",
        "fisco-utilities-tool": "UtilitiesTest/.*|TimerTest/.*|TokenBucketRateLimiterTest/.*|ZstdCompressTests/.*|ObjectCounter/.*|DataEntryptionTest/.*|AwsKmsWrapperTest/.*|test-tars-protocol",
        "fisco-mut-front-message": "BcMutMessageTest/.*",
        "fisco-min-seal-time-drift": "BcMutMessageTest/.*",
        "fisco-invalid-signature-acceptance": "TxPool.*|Transaction.*",
        "fisco-expired-blocklimit-acceptance": "TxPool.*|Transaction.*",
        "fisco-chain-block-limit-collapse": "PBFT.*|RPBFT.*|Consensus.*|TxsSyncMsgTest/.*",
    }
    test_binaries = [
        path for path in build_dir.rglob("test-bcos-*")
        if path.is_file() and os.access(path, os.X_OK)
    ]
    if not test_binaries:
        return result
    argv = ["ctest", "--output-on-failure", "-R", regexes[interaction]]
    completed = subprocess.run(
        argv,
        cwd=build_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
    )
    (coverage_dir / "ctest.stdout.log").write_text(completed.stdout, encoding="utf-8", errors="replace")
    (coverage_dir / "ctest.stderr.log").write_text(completed.stderr, encoding="utf-8", errors="replace")
    status = "success" if completed.returncode == 0 else "failed"
    if completed.returncode != 0 and "Could not find executable" in completed.stdout:
        status = "not_executed"
        result["reason"] = "CTest entries exist, but the referenced FISCO test executables are not built"
    result.update({"status": status, "argv": argv, "returncode": completed.returncode})
    gcda_count = sum(1 for _ in build_dir.rglob("*.gcda"))
    result["metrics"] = {"gcda_files": float(gcda_count)}
    return result


def discover_fisco_build_dir(target_root: Path) -> Path | None:
    candidates = [target_root / "build", Path("/tmp/fisco-coverage-build.tbqgKy")]
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "CTestTestfile.cmake").is_file():
            return candidate
    return None


def run_aptos_coverage(target_root: Path, interaction: str, coverage_dir: Path) -> dict[str, Any]:
    if shutil.which("cargo-llvm-cov") is None or shutil.which("llvm-profdata") is None or shutil.which("llvm-cov") is None:
        return {
            "status": "not_executed",
            "reason": "matching Rust/LLVM coverage tools are unavailable",
            "metrics": {},
        }
    tests = {
        "aptos-fixed-broadcast-self": ["test_broadcast_self_txns"],
        "aptos-validator-forward": ["single_outbound_node_test"],
        "aptos-inbound-tx": ["single_inbound_node_test"],
        "aptos-vfn-forward": ["vfn_middle_man_test"],
        "aptos-ready-tx": ["test_ready_txns"],
        "aptos-gas-price": ["test_update_gas_price"],
        "aptos-to-val": ["fn_to_val_test"],
        "aptos-ack-retry": ["test_skip_ack_rebroadcast"],
        "aptos-sync-interrupt": ["test_interrupt_in_sync_inbound"],
        "aptos-rebroadcast": ["test_mempool_full_rebroadcast", "test_rebroadcast_retry_is_empty"],
        "aptos-commit-removal": ["test_mempool_notify_committed_txns", "test_consensus_events_rejected_txns"],
        "aptos-parking-lot": ["test_get_all_addresses_from_parking_lot"],
        "aptos-mut-txs": ["bc_mut_txs"],
        "aptos-round-timeout-zero": ["bc_mut_txs"],
        "aptos-sync-only": ["bc_mut_txs"],
        "aptos-safety-rules-dead": ["bc_mut_txs"],
    }
    output = coverage_dir / "lcov.info"
    argv = [
        "cargo",
        "llvm-cov",
        "-p",
        "aptos-mempool",
        "--lcov",
        "--output-path",
        str(output),
        "--",
        *tests[interaction],
    ]
    completed = subprocess.run(
        argv,
        cwd=target_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
    )
    (coverage_dir / "cargo-llvm-cov.stdout.log").write_text(completed.stdout, encoding="utf-8", errors="replace")
    (coverage_dir / "cargo-llvm-cov.stderr.log").write_text(completed.stderr, encoding="utf-8", errors="replace")
    result = {"status": "success" if completed.returncode == 0 else "failed", "argv": argv, "returncode": completed.returncode, "metrics": {}}
    if completed.returncode == 0 and output.is_file():
        result["metrics"] = parse_lcov(output)
    return result


def parse_lcov(path: Path) -> dict[str, float]:
    found = 0
    hit = 0
    functions_found = 0
    functions_hit = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("LF:"):
            found += int(line[3:])
        elif line.startswith("LH:"):
            hit += int(line[3:])
        elif line.startswith("FNF:"):
            functions_found += int(line[4:])
        elif line.startswith("FNH:"):
            functions_hit += int(line[4:])
    return {
        "covered_statements": float(hit),
        "total_statements": float(found),
        "statement_coverage_pct": (hit / found * 100.0) if found else 0.0,
        "covered_functions": float(functions_hit),
        "total_functions": float(functions_found),
        "function_coverage_pct": (functions_hit / functions_found * 100.0) if functions_found else 0.0,
    }
