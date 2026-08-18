// Seeded mutation library for the chainmaker sync message handler.
// Kinds: heights (0/max/gap), corrupted payloads, empty payloads, oversized
// payloads, wrong message types, block-response variants, and a random mix.
package sync

import (
	"math/rand"
	"os"
	"strconv"
	"testing"

	netPb "chainmaker.org/chainmaker/pb-go/v3/net"
)

func bcMutSyncSeed(t *testing.T) int64 {
	seed := int64(1)
	if v := os.Getenv("BCFUZZER_SEED"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			seed = n
		}
	}
	t.Logf("mutation seed: %d", seed)
	return seed
}

func TestBcMutSyncMsg(t *testing.T) {
	rnd := rand.New(rand.NewSource(bcMutSyncSeed(t)))

	service, fn := initTestSync(t)
	defer fn()
	implSync := service.(*BlockChainSyncServer)

	const kinds = 8
	for round := 0; round < kinds*5; round++ {
		kind := round % kinds
		height := uint64(rnd.Int63n(200000))
		switch kind {
		case 0:
			_ = implSync.blockSyncMsgHandler("node1", getNodeStatusReq(t), netPb.NetMsg_SYNC_BLOCK_MSG)
		case 1:
			h := []uint64{0, 1 << 62, height}[rnd.Intn(3)]
			_ = implSync.blockSyncMsgHandler("node2", getNodeStatusResp(t, h), netPb.NetMsg_SYNC_BLOCK_MSG)
		case 2:
			_ = implSync.blockSyncMsgHandler("node3", getBlockReq(t, height, uint64(rnd.Intn(201))), netPb.NetMsg_SYNC_BLOCK_MSG)
		case 3:
			h := []uint64{0, 1 << 62, height}[rnd.Intn(3)]
			_ = implSync.blockSyncMsgHandler("node4", getBlockResp(t, h), netPb.NetMsg_SYNC_BLOCK_MSG)
		case 4:
			corrupt := make([]byte, rnd.Intn(4000))
			_, _ = rnd.Read(corrupt)
			_ = implSync.blockSyncMsgHandler("node5", corrupt, netPb.NetMsg_SYNC_BLOCK_MSG)
		case 5:
			_ = implSync.blockSyncMsgHandler("node6", nil, netPb.NetMsg_SYNC_BLOCK_MSG)
		case 6:
			big := make([]byte, 50_000)
			_, _ = rnd.Read(big)
			_ = implSync.blockSyncMsgHandler("node7", big, netPb.NetMsg_SYNC_BLOCK_MSG)
		case 7:
			_ = implSync.blockSyncMsgHandler("node8", getNodeStatusReq(t), netPb.NetMsg_MsgType(rnd.Intn(50)))
		}
	}
}
