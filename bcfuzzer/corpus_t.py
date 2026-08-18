"""Transaction corpus T (design §3.3).

Realistic workload seeds submitted through *normal* nodes by default;
seeds whose bug requires a controlled node to accept them (expired
block_limit=0 tx, bad-signature tx) carry role="controlled" and a
precondition.  Submission mechanics live in the per-target adapters
(submit_seed); this module only declares the corpus.

Seeds are the PoC-verified workload shapes:

  geth:       simple wave / replacement pair / data txs / blob txs /
              blob replacement pair (nonce-equal, fee x2.5 — paper #9)
  fisco:      signed transfer wave / expired block_limit=0 tx (#6) /
              65-zero-byte bad-signature tx (#5)
  chainmaker: cmc contract invoke wave
  aptos:      signed transfer wave / malformed BCS probes
"""

from __future__ import annotations

from .common import Seed


def geth_t_seeds() -> list[Seed]:
    return [
        Seed(seed_id="geth-t-simple", corpus="T", role="normal",
             target="geth",
             payload={"kind": "simple", "count": 30},
             bug_tags=[]),
        Seed(seed_id="geth-t-replacement", corpus="T", role="normal",
             target="geth",
             payload={"kind": "replacement", "count": 10},
             bug_tags=["ge-09"]),
        Seed(seed_id="geth-t-data", corpus="T", role="normal",
             target="geth",
             payload={"kind": "data", "count": 10},
             bug_tags=[]),
        Seed(seed_id="geth-t-blob", corpus="T", role="normal",
             target="geth",
             payload={"kind": "blob", "count": 4},
             bug_tags=["ge-09"]),
        Seed(seed_id="geth-t-blob-replacement", corpus="T", role="normal",
             target="geth",
             payload={"kind": "blob_replacement", "count": 2},
             bug_tags=["ge-09"]),
        # PoC #9 shape: same-nonce pair with a much higher fee replacing a
        # blob tx, driving the replacement through the price bump config.
        Seed(seed_id="geth-t-blob-pair", corpus="T", role="controlled",
             target="geth",
             payload={"kind": "blob_pair", "fee_multiplier": 2.5,
                      "count": 2},
             bug_tags=["ge-09"]),
    ]


def fisco_t_seeds() -> list[Seed]:
    return [
        Seed(seed_id="fisco-t-transfer-wave", corpus="T", role="normal",
             target="fisco",
             payload={"kind": "transfer_wave", "count": 30},
             bug_tags=[]),
        Seed(seed_id="fisco-t-expired", corpus="T", role="controlled",
             target="fisco",
             payload={"kind": "expired_tx"},
             preconditions={"config": "txpool.check_block_limit=false"},
             bug_tags=["fs-06"]),
        Seed(seed_id="fisco-t-bad-signature", corpus="T", role="controlled",
             target="fisco",
             payload={"kind": "bad_signature", "count": 2},
             preconditions={"config": "experimental.check_transaction_signature=false"},
             bug_tags=["fs-05"]),
        # one-block chain pressure: block_limit=1 admits exactly one tx per
        # block; a wave then litters pending + receipt churn (paper #7)
        Seed(seed_id="fisco-t-block-limit-1", corpus="T", role="controlled",
             target="fisco",
             payload={"kind": "transfer_wave", "count": 30},
             preconditions={"config": "chain.block_limit=1"},
             bug_tags=["fs-07"]),
    ]


def chainmaker_t_seeds() -> list[Seed]:
    return [
        Seed(seed_id="cm-t-invoke-wave", corpus="T", role="normal",
             target="chainmaker",
             payload={"kind": "invoke_wave", "count": 30},
             bug_tags=[]),
        # batch pool: push invokes fast enough that the batch creator packs
        # a turbo block whose TxCount outgrows the verifier's expectation
        Seed(seed_id="cm-t-batch-wave", corpus="T", role="controlled",
             target="chainmaker",
             payload={"kind": "invoke_wave", "count": 120},
             preconditions={"config": "txpool.pool_type=batch"},
             bug_tags=["cm-01"]),
        Seed(seed_id="cm-t-gov-query", corpus="T", role="normal",
             target="chainmaker",
             payload={"kind": "gov_query"},
             bug_tags=[]),
    ]


def aptos_t_seeds() -> list[Seed]:
    return [
        Seed(seed_id="aptos-t-transfer-wave", corpus="T", role="normal",
             target="aptos",
             payload={"kind": "transfer_wave", "count": 30, "varied": True},
             bug_tags=[]),
        Seed(seed_id="aptos-t-malformed", corpus="T", role="controlled",
             target="aptos",
             payload={"kind": "malformed_probes"},
             bug_tags=[]),
    ]


def corpus_t(target: str) -> list[Seed]:
    builders = {
        "geth": geth_t_seeds,
        "fisco": fisco_t_seeds,
        "chainmaker": chainmaker_t_seeds,
        "aptos": aptos_t_seeds,
    }
    if target not in builders:
        raise ValueError(f"unknown target: {target}")
    return builders[target]()


def all_corpora() -> dict[str, list[Seed]]:
    return {target: corpus_t(target)
            for target in ("geth", "fisco", "chainmaker", "aptos")}
