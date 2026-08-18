// Seeded mutation library for the blob transaction pool.
// Kinds: blob fee boundaries (0/1/max), blob counts (1-6), gas extremes,
// nonce gaps/duplicates, tip-above-cap, fee-cap-below-base, pool floods
// (eviction), and a random mix.
package blobpool

import (
	"math/rand"
	"os"
	"path/filepath"
	"strconv"
	"testing"

	"github.com/ethereum/go-ethereum/core/state"
	"github.com/ethereum/go-ethereum/core/tracing"
	"github.com/ethereum/go-ethereum/core/txpool"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/params"
	"github.com/holiman/billy"
	"github.com/holiman/uint256"
)

func bcMutBlobSeed(t *testing.T) int64 {
	seed := int64(1)
	if v := os.Getenv("BCFUZZER_SEED"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			seed = n
		}
	}
	t.Logf("mutation seed: %d", seed)
	return seed
}

func TestBcMutBlobTx(t *testing.T) {
	rnd := rand.New(rand.NewSource(bcMutBlobSeed(t)))

	storage := t.TempDir()
	os.MkdirAll(filepath.Join(storage, pendingTransactionStore), 0700)
	store, _ := billy.Open(billy.Options{Path: filepath.Join(storage, pendingTransactionStore)}, newSlotterEIP7594(testMaxBlobsPerBlock), nil)
	defer store.Close()

	statedb, _ := state.New(types.EmptyRootHash, state.NewDatabaseForTesting())
	key, _ := crypto.GenerateKey()
	addr := crypto.PubkeyToAddress(key.PublicKey)
	statedb.AddBalance(addr, uint256.NewInt(100000000000), tracing.BalanceChangeUnspecified)
	statedb.Commit(0, true, false)

	chain := &testBlockChain{
		config:  params.MainnetChainConfig,
		basefee: uint256.NewInt(params.InitialBaseFee),
		blobfee: uint256.NewInt(params.BlobTxMinBlobGasprice),
		statedb: statedb,
	}
	pool := New(Config{Datadir: storage}, chain, nil)
	if err := pool.Init(1, chain.CurrentBlock(), newReserver()); err != nil {
		t.Fatalf("failed to create blob pool: %v", err)
	}
	defer pool.Close()

	const kinds = 9
	for batch := 0; batch < kinds*3; batch++ {
		kind := batch % kinds
		nonce := uint64(0)
		var txs []*types.Transaction
		switch kind {
		case 0:
			blobFees := []uint64{0, 1, params.BlobTxMinBlobGasprice, params.BlobTxMinBlobGasprice + 1, 1_000_000}
			for i := 0; i < 25; i++ {
				nonce += uint64(rnd.Intn(3))
				txs = append(txs, makeTx(nonce, 10, 1000, blobFees[rnd.Intn(len(blobFees))], key))
			}
		case 1:
			for i := 0; i < 15; i++ {
				nonce += uint64(rnd.Intn(2))
				txs = append(txs, makeMultiBlobTx(nonce, 10, 1000, 100, 1+rnd.Intn(6), rnd.Intn(6), key))
			}
		case 2:
			for i := 0; i < 20; i++ {
				nonce += uint64(rnd.Intn(2))
				tx := makeTx(nonce, 10, 1000, 100, key)
				_ = tx.BlobTxSidecar()
				txs = append(txs, makeMultiBlobTx(nonce, 10, 1000, 100, 1, rnd.Intn(6), key))
			}
		case 3:
			gaps := []uint64{1, 2, 5, 10}
			for i := 0; i < 25; i++ {
				nonce += gaps[rnd.Intn(len(gaps))]
				txs = append(txs, makeTx(nonce, 10, 1000, 100, key))
			}
		case 4:
			for i := 0; i < 20; i++ {
				nonce += uint64(rnd.Intn(2))
				txs = append(txs, makeTx(nonce, 1_000_000, 1, 1, key))
			}
		case 5:
			for i := 0; i < 20; i++ {
				nonce += uint64(rnd.Intn(2))
				txs = append(txs, makeTx(nonce, 1, 1, 100, key))
			}
		case 6:
			for i := 0; i < 25; i++ {
				txs = append(txs, makeMultiBlobTx(uint64(i%10), uint64(10+i), uint64(1000+i*100), uint64(100+i*10), 1, rnd.Intn(6), key))
			}
		case 7:
			for i := 0; i < 800; i++ {
				nonce += uint64(rnd.Intn(3))
				txs = append(txs, makeTx(nonce, uint64(rnd.Intn(50)), uint64(500+rnd.Intn(500)), uint64(50+rnd.Intn(50)), key))
			}
		case 8:
			for i := 0; i < 30; i++ {
				nonce += uint64(rnd.Intn(3))
				switch rnd.Intn(4) {
				case 0:
					txs = append(txs, makeTx(nonce, uint64(rnd.Intn(100)), uint64(rnd.Intn(5000)), uint64(rnd.Intn(5000)), key))
				case 1:
					txs = append(txs, makeMultiBlobTx(nonce, uint64(rnd.Intn(100)), uint64(rnd.Intn(5000)), uint64(rnd.Intn(5000)), 1+rnd.Intn(3), rnd.Intn(6), key))
				case 2:
					txs = append(txs, makeTx(nonce, uint64(rnd.Intn(100)), uint64(rnd.Intn(5000)), 0, key))
				case 3:
					txs = append(txs, makeMultiBlobTx(nonce, uint64(rnd.Intn(100)), uint64(rnd.Intn(5000)), uint64(rnd.Intn(5000)), 1, rnd.Intn(6), key))
				}
			}
		}
		pool.Add(txs, false)
		pool.Pending(txpool.PendingFilter{})
		pool.Content()
		pool.Stats()
	}
	pool.Content()
}
