# BCB #3: ChainMaker certificate reconfiguration + logger level-map race panic

> **Table 1 #3 (Peer Failure).**
> A normal peer panics when certificate reconfiguration races logger
> level-map writes during rejoining.
> Original issue: "Fatal error: concurrent map writes caused by node
> reconfiguration and restart under stress testing" (paper ref [30]).

## Bug summary

| Item | Content |
|------|---------|
| Type | Inter-node BCB, Peer Failure (crash) |
| Config items | certificate material (`certs/node/*.crt` / `*.key`) + `log.yml` log level |
| Trigger | A controlled node's certificate is re-issued/replaced and the node is restarted (rejoin) while normal peers concurrently reload their `log.yml` level configuration; the overlapping logger creation races the `logger.go` level-map access and panics a normal peer. |
| Root cause | `logger.go` level map was accessed without synchronization during concurrent logger creation / level reload triggered by rejoin and config-file updates. |
| Inter-node impact | A normal peer crashes; the network loses a quorum member. |

## BCFuzzer discovery path

The fuzzer targets this path through certificate-management mutations
overlapped with `restart_cycle` and `concurrent_workload` sequences
(`bcfuzzer/sequences.py`): the controlled org is restarted with renewed
certificate material while normal orgs drive concurrent log-level changes
and transaction invokes, racing the logger level-map update.

## Reproduction

```
bash poc.sh
```

The script builds a 4-node TBFT network, then for 20 cycles stops the
controlled org, rotates its signing certificate, and restarts it, while a
background thread alternates the normal org's `log.yml` level between INFO
and DEBUG and drives concurrent invokes.  It prints `[POC PASS]` if a
normal org's `panic.log` shows the logger / concurrent-map panic.

## Version dependence

ChainMaker v3.0.0 (@2b8f85a) may have hardened the `logger.go` level map
against concurrent access.  If the race no longer panics, the script prints
`[POC VERSION-GUARDED]` and points to the original issue; the discovery
path remains valid as a historical record.
