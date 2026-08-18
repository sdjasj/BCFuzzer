// Mutation test for the transaction ordering heap: builds seeded batches of
// transactions with mutated prices/nonces across many accounts and walks the
// price-and-nonce ordering (legacy and dynamic-fee variants).
package txorder

import (
	"crypto/ecdsa"
	"math/big"
	"math/rand"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/txpool"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/holiman/uint256"
)

func bcMutOrderSeed(t *testing.T) int64 {
	seed := int64(1)
	if v := os.Getenv("BCFUZZER_SEED"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			seed = n
		}
	}
	t.Logf("mutation seed: %d", seed)
	return seed
}

func TestBcMutTxOrder(t *testing.T) {
	rnd := rand.New(rand.NewSource(bcMutOrderSeed(t)))
	signer := types.LatestSignerForChainID(common.Big1)

	keys := make([]*ecdsa.PrivateKey, 20)
	for i := range keys {
		keys[i], _ = crypto.GenerateKey()
	}
	for round := 0; round < 10; round++ {
		baseFee := big.NewInt(rnd.Int63n(3000))
		groups := map[common.Address][]*txpool.LazyTransaction{}
		for _, key := range keys {
			addr := crypto.PubkeyToAddress(key.PublicKey)
			count := 5 + rnd.Intn(30)
			var list []*txpool.LazyTransaction
			for i := 0; i < count; i++ {
				var tx *types.Transaction
				price := rnd.Int63n(5000)
				if rnd.Intn(2) == 0 {
					tx = types.NewTx(&types.LegacyTx{
						Nonce:    uint64(i) + uint64(rnd.Intn(3)),
						To:       &common.Address{},
						Value:    big.NewInt(rnd.Int63n(1e18)),
						Gas:      100000,
						GasPrice: big.NewInt(price),
						Data:     make([]byte, rnd.Intn(500)),
					})
				} else {
					tx = types.NewTx(&types.DynamicFeeTx{
						Nonce:     uint64(i) + uint64(rnd.Intn(3)),
						To:        &common.Address{},
						Value:     big.NewInt(rnd.Int63n(1e18)),
						Gas:       100000,
						GasFeeCap: big.NewInt(price + rnd.Int63n(500)),
						GasTipCap: big.NewInt(rnd.Int63n(500)),
						Data:      make([]byte, rnd.Intn(500)),
					})
				}
				signed, err := types.SignTx(tx, signer, key)
				if err != nil {
					t.Fatalf("failed to sign tx: %v", err)
				}
				list = append(list, &txpool.LazyTransaction{
					Hash:      signed.Hash(),
					Tx:        signed,
					Time:      time.Unix(int64(round*1000+i), 0),
					GasFeeCap: uint256.MustFromBig(signed.GasFeeCap()),
					GasTipCap: uint256.MustFromBig(signed.GasTipCap()),
					Gas:       signed.Gas(),
					BlobGas:   signed.BlobGas(),
				})
			}
			groups[addr] = list
		}
		it := NewTransactionsByPriceAndNonce(signer, groups, baseFee)
		for {
			tx, _ := it.Peek()
			if tx == nil {
				break
			}
			it.Pop()
		}
	}
}
