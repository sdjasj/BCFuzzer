"""Stage A smoke tests: mutation rules, rollback, dangerous_legal exemption."""

from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "bcfuzzer"))

from bcfuzzer.common import MutationOp  # noqa: E402
from bcfuzzer.item_catalog import (  # noqa: E402
    APTOS_ITEMS, CHAINMAKER_ITEMS, FISCO_ITEMS, GETH_ITEMS, item_by_path)
from bcfuzzer.mutator import ConfigEditor, exempt_key_set, mutate_one  # noqa: E402

GETH_TOML = """[Eth]
NetworkId = 1
SyncMode = "snap"

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
Datacap = 1073741824
PriceBump = 100
"""

FISCO_INI = """[p2p]
    listen_ip=0.0.0.0
    listen_port=30300

[txpool]
    limit=8000
    txs_expiration_time=300

[consensus]
    consensus_type=pbft
    min_seal_time=1000

[tx]
    gas_limit=3000000000
"""

CHAINMAKER_YAML = """txpool:
  pool_type: normal
  batch_max_size: 50
net:
  seeds:
    - /ip4/127.0.0.1/tcp/11301/p2p/QmNode1
    - /ip4/127.0.0.1/tcp/11302/p2p/QmNode2
rpc:
  ratelimit:
    enabled: false
"""

APTOS_YAML = """consensus:
  round_initial_timeout_ms: 500
  sync_only: false
safety_rules:
  service:
    type: local
mempool:
  capacity: 1000000
"""


def test_geth_int_and_exemption() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "geth.toml"
        cfg.write_text(GETH_TOML, encoding="utf-8")
        editor = ConfigEditor(Path(tmp))
        rng = random.Random(7)
        item = item_by_path("geth", "Eth.Miner.GasCeil")
        op = mutate_one(editor, cfg, item, "dangerous", rng, 0)
        assert isinstance(op, MutationOp)
        assert op.new_value in item.dangerous_legal, f"got {op.new_value}"
        assert op.exempt_sanitize, "dangerous_legal value must be exempt"
        assert exempt_key_set([op]) == {("geth.toml", "Eth.Miner.GasCeil")}
        assert f"GasCeil = {op.new_value}" in cfg.read_text(encoding="utf-8")
        editor.rollback()
        assert cfg.read_text(encoding="utf-8") == GETH_TOML, "rollback must restore bytes"


def test_geth_all_items_mutate() -> None:
    for item in GETH_ITEMS:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "geth.toml"
            cfg.write_text(GETH_TOML, encoding="utf-8")
            editor = ConfigEditor(Path(tmp))
            rng = random.Random(42)
            for rule in ("min", "max", "zero", "scale_10", "dangerous"):
                mutate_one(editor, cfg, item, rule, rng, 0)
                assert cfg.is_file(), f"{item.path} {rule} broke the file"
            editor.rollback()


def test_fisco_ini_insert_missing_section() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.ini"
        cfg.write_text(FISCO_INI, encoding="utf-8")
        editor = ConfigEditor(Path(tmp))
        rng = random.Random(1)
        # experimental section does not exist yet -> must be inserted
        item = item_by_path("fisco", "experimental.check_transaction_signature")
        op = mutate_one(editor, cfg, item, "set_false", rng, 0)
        text = cfg.read_text(encoding="utf-8")
        assert "[experimental]" in text and "check_transaction_signature = false" in text
        assert op.new_value is False
        editor.rollback()
        assert cfg.read_text(encoding="utf-8") == FISCO_INI


def test_chainmaker_list_ops_and_enum() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "chainmaker.yml"
        cfg.write_text(CHAINMAKER_YAML, encoding="utf-8")
        editor = ConfigEditor(Path(tmp))
        rng = random.Random(3)
        enum_item = item_by_path("chainmaker", "txpool.pool_type")
        op = mutate_one(editor, cfg, enum_item, "dangerous", rng, 0)
        assert op.new_value == "batch" and op.exempt_sanitize
        seeds_item = item_by_path("chainmaker", "net.seeds")
        mutate_one(editor, cfg, seeds_item, "append_elem", rng, 1)
        mutate_one(editor, cfg, seeds_item, "remove_elem", rng, 2)
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert data["txpool"]["pool_type"] == "batch"
        assert len(data["net"]["seeds"]) == 2
        editor.rollback()
        assert cfg.read_text(encoding="utf-8") == CHAINMAKER_YAML


def test_aptos_nested_ops() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "node.yaml"
        cfg.write_text(APTOS_YAML, encoding="utf-8")
        editor = ConfigEditor(Path(tmp))
        rng = random.Random(9)
        svc = item_by_path("aptos", "safety_rules.service")
        op = mutate_one(editor, cfg, svc, "dangerous", rng, 0)
        assert op.new_value["type"] == "process"
        assert op.exempt_sanitize
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert data["safety_rules"]["service"]["type"] == "process"
        # delete_member on the nested object
        mutate_one(editor, cfg, svc, "delete_member", rng, 1)
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert "service" not in data["safety_rules"]
        # zero dangerous value on consensus timeout
        timeout = item_by_path("aptos", "consensus.round_initial_timeout_ms")
        mutate_one(editor, cfg, timeout, "dangerous", rng, 2)
        editor.rollback()
        assert cfg.read_text(encoding="utf-8") == APTOS_YAML


def test_all_catalogs_wellformed() -> None:
    for items in (GETH_ITEMS, CHAINMAKER_ITEMS, FISCO_ITEMS, APTOS_ITEMS):
        for item in items:
            assert item.kind in ("int", "float", "bool", "string", "enum",
                                 "list", "nested"), item.path
            if item.kind in ("int", "float"):
                assert item.bounds is not None, item.path
            if item.kind == "enum":
                assert item.enum, item.path


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all mutator tests passed")
