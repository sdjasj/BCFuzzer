# BCB #2: ChainMaker net.seeds peer-information-map race panic

> **Table 1 #2 (Peer Failure).**
> Normal peers panic because repeated `net.seeds` updates race accesses to
> the peer-information map.
> Original issue: <https://git.chainmaker.org.cn/chainmaker/issue/-/issues/1202> (paper ref [29]).

## Bug summary

| Item | Content |
|------|---------|
| Type | Inter-node BCB, Peer Failure (crash) |
| Config item | `net.seeds` (node-local peer seed list) |
| Trigger | A controlled node repeatedly rewrites its valid `net.seeds` list (reorder / subset / duplicate — all legal variants) while restarting, forcing normal peers to re-verify and update connections; unsynchronized access to the peer-information map panics other normal nodes. |
| Root cause | `ReVerifyPeers` and the peer add/remove handlers accessed the peer-info map without synchronization; concurrent writes panic the goroutine. |
| Inter-node impact | All normal nodes crash; only the controlled node survives. |

## BCFuzzer discovery path

The fuzzer exercises this bug via the `net.seeds` mutation item
(`bcfuzzer/item_catalog.py`, tagged `cm-02`, list-kind with `append_elem` /
`remove_elem` / `reorder` rules) overlapped with the `restart_cycle` and
`concurrent_workload` sequence primitives (`bcfuzzer/sequences.py`):
restart the controlled org with a rewritten `net.seeds` while normal orgs
drive concurrent `cmc` invokes, racing the peer-map update path.

## Reproduction

```
bash poc.sh
```

The script builds a 4-node TBFT network, then for 30 cycles stops the
controlled org, rewrites `net.seeds` (cycling reorder/duplicate/subset),
restarts it, while a background thread drives concurrent invokes.  It
prints `[POC PASS]` if a normal org's `panic.log` shows the concurrent-map
panic.

## Version dependence

ChainMaker v3.0.0 (@2b8f85a) ships net-libp2p v1.3.1, which protects the
peer-information maps with `RWMutex`.  If the race no longer panics on this
version, the script prints `[POC VERSION-GUARDED]` and points to the
original issue; the discovery path remains valid as a historical record.
