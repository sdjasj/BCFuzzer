"""Declarative item catalogs for the four targets (design §3.1).

Every entry mirrors a real key in the target's config file.  The
`dangerous_legal` values are the paper's Table 1 trigger values that the
existing live-admission sanitizers would clamp away (e.g. geth GasCeil 5000,
fisco min_seal_time 60000); the campaign exempts those exact keys so the
values reach the running node.

Bug tags refer to the paper's Table 1 BCBs: cm-01..03 (ChainMaker),
fs-04..07 (FISCO-BCOS), ge-08..09 (geth), ap-10..12 (Aptos).
"""

from __future__ import annotations

from .common import ItemSpec

# --------------------------------------------------------------------------
# geth — generated geth.toml (sections Eth.Miner / Eth.TxPool / Eth.BlobPool)
# --------------------------------------------------------------------------

GETH_ITEMS: list[ItemSpec] = [
    ItemSpec(path="Eth.Miner.GasCeil", kind="int",
             default=30_000_000, bounds=(0, 4_000_000_000),
             dangerous_legal=[5_000, 21_000, 100_000_000],
             bug_tags=["ge-08"],
             cross_constraint="miner.etherbase must be the clique signer"),
    ItemSpec(path="Eth.Miner.GasPrice", kind="int",
             default=1_000_000, bounds=(0, 10**18)),
    ItemSpec(path="Eth.Miner.Recommit", kind="int",
             default=2_000_000_000, bounds=(0, 600_000_000_000)),
    ItemSpec(path="Eth.TxPool.PriceLimit", kind="int",
             default=1, bounds=(0, 1_000_000_000)),
    ItemSpec(path="Eth.TxPool.PriceBump", kind="int",
             default=10, bounds=(0, 1_000)),
    ItemSpec(path="Eth.TxPool.GlobalSlots", kind="int",
             default=2048, bounds=(1, 1_000_000)),
    ItemSpec(path="Eth.TxPool.GlobalQueue", kind="int",
             default=1024, bounds=(1, 1_000_000)),
    ItemSpec(path="Eth.TxPool.Lifetime", kind="int",
             default=10_800_000_000_000, bounds=(1_000_000_000, 604_800_000_000_000)),
    ItemSpec(path="Eth.BlobPool.Datacap", kind="int",
             default=1_073_741_824, bounds=(1024, 1_099_511_627_776)),
    ItemSpec(path="Eth.BlobPool.PriceBump", kind="int",
             default=100, bounds=(0, 1_000),
             dangerous_legal=[1_000_000],
             bug_tags=["ge-09"],
             cross_constraint="interacts with blob replacement-pair workload (corpus T)"),
]

# --------------------------------------------------------------------------
# ChainMaker — chainmaker.yml per org
# --------------------------------------------------------------------------

CHAINMAKER_ITEMS: list[ItemSpec] = [
    ItemSpec(path="txpool.pool_type", kind="enum",
             default="normal", enum=["normal", "batch"],
             dangerous_legal=["batch"],
             bug_tags=["cm-01"],
             cross_constraint="batch pool + turbo block; TxCount grows per batch"),
    ItemSpec(path="txpool.batch_max_size", kind="int",
             default=50, bounds=(1, 10_000)),
    ItemSpec(path="txpool.batch_create_timeout", kind="int",
             default=50, bounds=(1, 600_000)),
    ItemSpec(path="txpool.max_txpool_size", kind="int",
             default=2048, bounds=(1, 100_000)),
    ItemSpec(path="txpool.max_config_txpool_size", kind="int",
             default=10, bounds=(1, 10_000)),
    ItemSpec(path="txpool.common_queue_num", kind="int",
             default=8, bounds=(1, 256)),
    ItemSpec(path="net.seeds", kind="list",
             default=[],
             bug_tags=["cm-02"],
             cross_constraint="seed list edited while consensus peers stay connected"),
    ItemSpec(path="rpc.ratelimit.enabled", kind="bool", default=False),
    ItemSpec(path="rpc.ratelimit.token_per_second", kind="int",
             default=-1, bounds=(-1, 1_000_000)),
]

# --------------------------------------------------------------------------
# FISCO-BCOS — config.ini (PBFT nodes)
# --------------------------------------------------------------------------

FISCO_ITEMS: list[ItemSpec] = [
    ItemSpec(path="consensus.min_seal_time", kind="int", file="config.ini",
             default=1000, bounds=(1, 600_000),
             dangerous_legal=[60_000, 600_000],
             bug_tags=["fs-04"],
             cross_constraint="legal range; leader waits seal_time before sealing"),
    ItemSpec(path="experimental.check_transaction_signature", kind="bool",
             file="config.ini", default=True,
             dangerous_legal=[False],
             bug_tags=["fs-05"],
             cross_constraint="bad-signature txs must be sent to the controlled node's RPC"),
    ItemSpec(path="txpool.check_block_limit", kind="bool", file="config.ini",
             default=True,
             dangerous_legal=[False],
             bug_tags=["fs-06"],
             cross_constraint="expired-tx (block_limit=0) must target controlled node"),
    ItemSpec(path="chain.block_limit", kind="int", file="config.genesis",
             default=1000, bounds=(1, 1_000_000),
             dangerous_legal=[1],
             bug_tags=["fs-07"],
             cross_constraint="genesis-consensus value; mismatch stalls sealing"),
    ItemSpec(path="txpool.limit", kind="int", file="config.ini",
             default=8000, bounds=(1, 1_000_000)),
    ItemSpec(path="txpool.txs_expiration_time", kind="int", file="config.ini",
             default=300, bounds=(1, 86_400)),
    ItemSpec(path="sync.tree_width", kind="int", file="config.ini",
             default=2, bounds=(1, 256)),
    ItemSpec(path="executor.enable_dag", kind="bool", file="config.ini",
             default=True),
]

# --------------------------------------------------------------------------
# Aptos — node.yaml per validator
# --------------------------------------------------------------------------

APTOS_ITEMS: list[ItemSpec] = [
    ItemSpec(path="consensus.round_initial_timeout_ms", kind="int", file="node.yaml",
             default=500, bounds=(1, 3_600_000),
             dangerous_legal=[0],
             bug_tags=["ap-10"],
             cross_constraint="zero disables timeout; liveness loss when quorum waits"),
    ItemSpec(path="consensus.sync_only", kind="bool", file="node.yaml",
             default=False,
             dangerous_legal=[True],
             bug_tags=["ap-11"],
             cross_constraint="sync_only validator never proposes"),
    ItemSpec(path="safety_rules.service", kind="nested", file="node.yaml",
             default={"type": "local"},
             nested_members=["type", "server_address"],
             enum=[{"type": "local"}, {"type": "thread"},
                   {"type": "process", "server_address": "/ip4/127.0.0.1/tcp/5555"},
                   {"type": "serializer"}],
             dangerous_legal=[{"type": "process",
                               "server_address": "/ip4/127.0.0.1/tcp/5555"}],
             bug_tags=["ap-12"],
             cross_constraint="process service requires external safety-rules server"),
    ItemSpec(path="mempool.capacity", kind="int", file="node.yaml",
             default=1_000_000, bounds=(1, 1_000_000_000)),
    ItemSpec(path="mempool.capacity_per_user", kind="int", file="node.yaml",
             default=1000, bounds=(1, 1_000_000)),
    ItemSpec(path="execution.concurrency_level", kind="int", file="node.yaml",
             default=4, bounds=(1, 128)),
]

# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

CATALOGS: dict[str, list[ItemSpec]] = {
    "geth": GETH_ITEMS,
    "chainmaker": CHAINMAKER_ITEMS,
    "fisco": FISCO_ITEMS,
    "aptos": APTOS_ITEMS,
}


def catalog_for(target: str) -> list[ItemSpec]:
    if target not in CATALOGS:
        raise KeyError(f"unknown target {target!r} (expected one of {sorted(CATALOGS)})")
    return CATALOGS[target]


def item_by_path(target: str, path: str) -> ItemSpec:
    for item in CATALOGS[target]:
        if item.path == path:
            return item
    raise KeyError(f"item {path!r} not in {target} catalog")


def bug_items(target: str, bug_tag: str) -> list[ItemSpec]:
    return [item for item in CATALOGS[target] if bug_tag in item.bug_tags]
