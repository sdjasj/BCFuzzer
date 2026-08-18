"""Message corpus M (design §3.4).

Protocol-message-level seeds targeting the controlled node directly:

  chainmaker: capability-flag seeds — the controlled org is restarted with
              CM_MALICIOUS_* env switches (compiled into the capability
              binary), i.e. a malicious proposer injecting corrupt batches /
              out-of-bounds TxCount / nil payloads (paper #1-#3).  Gated on
              precondition {"role": "proposer"} so placement only fires
              when the controlled org currently proposes.
  fisco:      Tars-layer variants delivered straight to the controlled
              node's RPC (zero-field / oversized / truncated structs) —
              the M channel exercised by the tars PoC payloads.
  geth:       fake-beacon drive variants (mode / rounds / period) against
              the controlled node's engine port.
  aptos:      malformed BCS transactions against the controlled API.
"""

from __future__ import annotations

from .common import Seed


def chainmaker_m_seeds() -> list[Seed]:
    proposer_precondition = {"role": "proposer"}
    return [
        # BCB #1 trigger family (pool_type=batch + turbo TxCount OOB):
        # txcount is the primary; index/nilpayload are variants.
        Seed(seed_id="cm-m-malicious-txcount", corpus="M", role="proposer",
             target="chainmaker",
             payload={"kind": "malicious",
                      "flags": {"CM_MALICIOUS_TXCOUNT": "1"}},
             preconditions=proposer_precondition,
             bug_tags=["cm-01"]),
        Seed(seed_id="cm-m-malicious-index", corpus="M", role="proposer",
             target="chainmaker",
             payload={"kind": "malicious",
                      "flags": {"CM_MALICIOUS_INDEX": "1"}},
             preconditions=proposer_precondition,
             bug_tags=["cm-01"]),
        Seed(seed_id="cm-m-malicious-nilpayload", corpus="M", role="proposer",
             target="chainmaker",
             payload={"kind": "malicious",
                      "flags": {"CM_MALICIOUS_NILPAYLOAD": "1"}},
             preconditions=proposer_precondition,
             bug_tags=["cm-01"]),
        # BCB #2 (net.seeds peer-info-map race): restart_cycle ×
        # concurrent_workload races the peer-map update.  May not reproduce
        # on v3.0.0 (RWMutex-hardened); the oracle still records the path.
        Seed(seed_id="cm-m-net-seeds-race", corpus="M", role="controlled",
             target="chainmaker",
             payload={"kind": "net_seeds_race"},
             bug_tags=["cm-02"]),
        # BCB #3 (cert reconfiguration + logger level-map race): same
        # restart/rejoin × concurrent logger-change mechanism.
        Seed(seed_id="cm-m-cert-logger-race", corpus="M", role="controlled",
             target="chainmaker",
             payload={"kind": "cert_logger_race"},
             bug_tags=["cm-03"]),
    ]


def fisco_m_seeds() -> list[Seed]:
    return [
        Seed(seed_id="fisco-m-tars-empty", corpus="M", role="controlled",
             target="fisco",
             payload={"kind": "tars_empty"},
             bug_tags=[]),
        Seed(seed_id="fisco-m-tars-oversized", corpus="M", role="controlled",
             target="fisco",
             payload={"kind": "tars_oversized"},
             bug_tags=[]),
        Seed(seed_id="fisco-m-tars-truncated", corpus="M", role="controlled",
             target="fisco",
             payload={"kind": "tars_truncated"},
             bug_tags=[]),
    ]


def geth_m_seeds() -> list[Seed]:
    return [
        Seed(seed_id="geth-m-beacon-slow", corpus="M", role="engine",
             target="geth",
             payload={"kind": "beacon_drive", "mode": "update",
                      "rounds": 3, "period": 3},
             bug_tags=[]),
        Seed(seed_id="geth-m-beacon-fast", corpus="M", role="engine",
             target="geth",
             payload={"kind": "beacon_drive", "mode": "update",
                      "rounds": 20, "period": 1},
             bug_tags=[]),
        Seed(seed_id="geth-m-beacon-new", corpus="M", role="engine",
             target="geth",
             payload={"kind": "beacon_drive", "mode": "new",
                      "rounds": 6, "period": 1},
             bug_tags=[]),
        # engine-level payload stress on the controlled producer: rapid
        # single-round drives exercise the payload build path under the
        # mutated miner/blobpool config of the round
        Seed(seed_id="geth-m-engine-rapid", corpus="M", role="engine",
             target="geth",
             payload={"kind": "engine_rapid", "blocks": 50, "period": 0.15},
             bug_tags=["ge-08", "ge-09"]),
    ]


def aptos_m_seeds() -> list[Seed]:
    return [
        Seed(seed_id="aptos-m-malformed-bcs", corpus="M", role="controlled",
             target="aptos",
             payload={"kind": "malformed_probes"},
             bug_tags=[]),
        Seed(seed_id="aptos-m-ledger-poll", corpus="M", role="controlled",
             target="aptos",
             payload={"kind": "ledger_poll"},
             bug_tags=[]),
    ]


def corpus_m(target: str) -> list[Seed]:
    builders = {
        "geth": geth_m_seeds,
        "fisco": fisco_m_seeds,
        "chainmaker": chainmaker_m_seeds,
        "aptos": aptos_m_seeds,
    }
    if target not in builders:
        raise ValueError(f"unknown target: {target}")
    return builders[target]()


def all_corpora_m() -> dict[str, list[Seed]]:
    return {target: corpus_m(target)
            for target in ("geth", "fisco", "chainmaker", "aptos")}
