// Seeded mutation library for the eth (devp2p) transaction-message path.
// Kinds: valid batches, wrong chain-id, zero gas price, huge data,
// dynamic-fee txs, empty batches, oversized batches, malformed mixes.
package eth

import (
	"math/big"
	"math/rand"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/eth/ethconfig"
	"github.com/ethereum/go-ethereum/eth/protocols/eth"
	"github.com/ethereum/go-ethereum/p2p"
	"github.com/ethereum/go-ethereum/p2p/enode"
	"github.com/ethereum/go-ethereum/params"
)

func bcMutMsgSeed(t *testing.T) int64 {
	seed := int64(1)
	if v := os.Getenv("BCFUZZER_SEED"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			seed = n
		}
	}
	t.Logf("mutation seed: %d", seed)
	return seed
}

func TestBcMutMessages(t *testing.T) {
	rnd := rand.New(rand.NewSource(bcMutMsgSeed(t)))

	handler := newTestHandler(ethconfig.FullSync)
	defer handler.close()
	handler.handler.synced.Store(true)

	txs := make(chan core.NewTxsEvent, 16384)
	sub := handler.txpool.SubscribeTransactions(txs, false)
	defer sub.Unsubscribe()

	p2pSrc, p2pSink := p2p.MsgPipe()
	defer p2pSrc.Close()
	defer p2pSink.Close()

	src := eth.NewPeer(69, p2p.NewPeerPipe(enode.ID{1}, "", nil, p2pSrc), p2pSrc, handler.txpool, handler.txpool, nil)
	defer src.Close()

	go handler.handler.runEthPeer(eth.NewPeer(69, p2p.NewPeerPipe(enode.ID{2}, "", nil, p2pSink), p2pSink, handler.txpool, handler.txpool, nil), func(peer *eth.Peer) error {
		return eth.Handle((*ethHandler)(handler.handler), peer)
	})
	head := handler.chain.CurrentBlock()
	if err := src.Handshake(1, handler.chain, eth.BlockRangeUpdatePacket{EarliestBlock: 0, LatestBlock: head.Number.Uint64(), LatestBlockHash: head.Hash()}); err != nil {
		t.Fatalf("failed to run protocol handshake: %v", err)
	}
	key, _ := crypto.GenerateKey()
	signer := types.LatestSignerForChainID(params.TestChainConfig.ChainID)

	build := func(kind int) []*types.Transaction {
		var batch []*types.Transaction
		nonce := uint64(0)
		count := 60
		switch kind {
		case 3:
			count = 0
		case 4:
			count = 500
		case 7:
			count = 40
		}
		for i := 0; i < count; i++ {
			nonce += uint64(rnd.Intn(3))
			gas := 21000 + uint64(rnd.Intn(500000))
			price := big.NewInt(rnd.Int63n(4000000000))
			switch kind {
			case 0, 1, 5, 6:
				if kind == 1 || (kind == 6 && rnd.Intn(2) == 0) {
					tx := types.NewTx(&types.DynamicFeeTx{
						Nonce: nonce, To: &common.Address{}, Value: big.NewInt(rnd.Int63n(1e18)),
						Gas: gas, GasFeeCap: price, GasTipCap: big.NewInt(rnd.Int63n(1000)),
					})
					signed, _ := types.SignTx(tx, signer, key)
					batch = append(batch, signed)
				} else {
					tx := types.NewTransaction(nonce, common.Address{}, big.NewInt(rnd.Int63n(1e18)), gas, price, nil)
					signed, _ := types.SignTx(tx, signer, key)
					batch = append(batch, signed)
				}
			case 2:
				tx := types.NewTransaction(nonce, common.Address{}, big.NewInt(rnd.Int63n(1e18)), gas, price, nil)
				signed, _ := types.SignTx(tx, types.NewEIP155Signer(big.NewInt(rnd.Int63n(100000)+2)), key)
				batch = append(batch, signed)
			case 3, 4:
				tx := types.NewTransaction(nonce, common.Address{}, big.NewInt(rnd.Int63n(1e18)), gas, price, nil)
				signed, _ := types.SignTx(tx, signer, key)
				batch = append(batch, signed)
			case 7:
				tx := types.NewTransaction(nonce, common.Address{}, big.NewInt(rnd.Int63n(1e18)), gas, price, make([]byte, rnd.Intn(20000)))
				signed, _ := types.SignTx(tx, signer, key)
				batch = append(batch, signed)
			case 8:
				tx := types.NewTransaction(nonce, common.Address{}, big.NewInt(rnd.Int63n(1e18)), gas, big.NewInt(0), nil)
				signed, _ := types.SignTx(tx, signer, key)
				batch = append(batch, signed)
			}
		}
		return batch
	}

	const kinds = 9
	for batch := 0; batch < kinds*4; batch++ {
		kind := batch % kinds
		batchTxs := build(kind)
		if err := src.SendTransactions(batchTxs); err != nil {
			t.Fatalf("failed to send transactions: %v", err)
		}
		timeout := time.After(800 * time.Millisecond)
	drain:
		for {
			select {
			case <-txs:
			case <-timeout:
				break drain
			}
		}
	}
}
