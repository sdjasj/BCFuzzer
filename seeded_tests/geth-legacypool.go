// Seeded mutation library for the legacy transaction pool.
// Each batch applies ONE mutation kind (cycled); parameters are driven by
// BCFUZZER_SEED. Kinds: price boundary, nonce gaps, nonce-dup replacements,
// value extremes, gas extremes, data sizes, tx types, wrong chain-id, zero
// signatures, duplicate batches, pool floods (eviction), tip-above-cap,
// underpriced, and a random mix.
package legacypool

import (
	"math/big"
	"math/rand"
	"os"
	"strconv"
	"testing"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/txpool"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
)

func bcMutSeed(t *testing.T) int64 {
	seed := int64(1)
	if v := os.Getenv("BCFUZZER_SEED"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			seed = n
		}
	}
	t.Logf("mutation seed: %d", seed)
	return seed
}

func TestBcMutLegacyTx(t *testing.T) {
	rnd := rand.New(rand.NewSource(bcMutSeed(t)))
	pool, key := setupPool()
	defer pool.Close()

	addr := crypto.PubkeyToAddress(key.PublicKey)
	testAddBalance(pool, addr, big.NewInt(0).Mul(big.NewInt(1e9), big.NewInt(1e18)))
	testSetNonce(pool, addr, 0)

	to := common.Address{0x42}
	signer := types.LatestSignerForChainID(big.NewInt(1))
	price := big.NewInt(2_000_000_000)

	const kinds = 14
	for batch := 0; batch < kinds*5; batch++ {
		kind := batch % kinds
		nonce := uint64(0)
		var txs []*types.Transaction
		switch kind {
		case 0:
			base := []int64{0, 1, 1_000_000_000, 3_000_000_000, 4_000_000_000}
			for i := 0; i < 60; i++ {
				nonce += uint64(rnd.Intn(2))
				txs = append(txs, pricedTransaction(nonce, 21000, big.NewInt(base[rnd.Intn(len(base))]), key))
			}
		case 1:
			gaps := []uint64{1, 2, 5, 10, 100}
			for i := 0; i < 40; i++ {
				nonce += gaps[rnd.Intn(len(gaps))]
				txs = append(txs, pricedTransaction(nonce, 21000, price, key))
			}
		case 2:
			for i := 0; i < 50; i++ {
				txs = append(txs, pricedTransaction(uint64(i%25), 21000, new(big.Int).Add(price, big.NewInt(int64(i)*1000)), key))
			}
		case 3:
			vals := []int64{0, 1, 1 << 40, 1 << 60, 1 << 62}
			for i := 0; i < 40; i++ {
				nonce += uint64(rnd.Intn(2))
				tx := types.NewTransaction(nonce, to, big.NewInt(vals[rnd.Intn(len(vals))]), 21000, price, nil)
				signed, _ := types.SignTx(tx, signer, key)
				txs = append(txs, signed)
			}
		case 4:
			gases := []uint64{0, 1, 21000, 21001, 1_000_000, 1_000_000_000, 1 << 30}
			for i := 0; i < 40; i++ {
				nonce += uint64(rnd.Intn(2))
				txs = append(txs, pricedTransaction(nonce, gases[rnd.Intn(len(gases))], price, key))
			}
		case 5:
			sizes := []int{0, 1, 32, 1024, 32768, 65536, 131072}
			for i := 0; i < 30; i++ {
				nonce += uint64(rnd.Intn(2))
				txs = append(txs, pricedDataTransaction(nonce, 21000, price, key, uint64(sizes[rnd.Intn(len(sizes))])))
			}
		case 6:
			for i := 0; i < 40; i++ {
				nonce += uint64(rnd.Intn(2))
				if rnd.Intn(2) == 0 {
					txs = append(txs, pricedTransaction(nonce, 21000, price, key))
				} else {
					txs = append(txs, dynamicFeeTx(nonce, 21000, price, big.NewInt(rnd.Int63n(1_000_000_000)), key))
				}
			}
		case 7:
			ids := []int64{0, 2, 99999}
			for i := 0; i < 40; i++ {
				nonce += uint64(rnd.Intn(2))
				tx := pricedTransaction(nonce, 21000, price, key)
				ws := types.NewEIP155Signer(big.NewInt(ids[rnd.Intn(len(ids))]))
				if signed, err := types.SignTx(tx, ws, key); err == nil {
					txs = append(txs, signed)
				}
			}
		case 8:
			for i := 0; i < 40; i++ {
				nonce += uint64(rnd.Intn(2))
				txs = append(txs, types.NewTx(&types.LegacyTx{
					Nonce: nonce, GasPrice: price, Gas: 21000, To: &to, Value: big.NewInt(1),
					V: big.NewInt(0), R: big.NewInt(0), S: big.NewInt(0),
				}))
			}
		case 9:
			for i := 0; i < 30; i++ {
				tx := pricedTransaction(uint64(i%10), 21000, new(big.Int).Add(price, big.NewInt(int64(i)*10)), key)
				txs = append(txs, tx, tx)
			}
		case 10:
			for i := 0; i < 2000; i++ {
				nonce += uint64(rnd.Intn(3))
				txs = append(txs, pricedTransaction(nonce, 21000, new(big.Int).Add(price, big.NewInt(int64(rnd.Intn(1000)))), key))
			}
		case 11:
			for i := 0; i < 40; i++ {
				nonce += uint64(rnd.Intn(2))
				txs = append(txs, dynamicFeeTx(nonce, 21000, big.NewInt(1), big.NewInt(3_000_000_000), key))
			}
		case 12:
			for i := 0; i < 40; i++ {
				nonce += uint64(rnd.Intn(2))
				txs = append(txs, pricedTransaction(nonce, 21000, big.NewInt(0), key))
			}
		case 13:
			for i := 0; i < 80; i++ {
				nonce += uint64(rnd.Intn(4))
				switch rnd.Intn(5) {
				case 0:
					txs = append(txs, pricedTransaction(nonce, 21000+uint64(rnd.Intn(1_000_000)), big.NewInt(rnd.Int63n(4_000_000_000)), key))
				case 1:
					txs = append(txs, pricedDataTransaction(nonce, 21000, big.NewInt(rnd.Int63n(4_000_000_000)), key, uint64(rnd.Intn(65536))))
				case 2:
					txs = append(txs, dynamicFeeTx(nonce, 21000, big.NewInt(rnd.Int63n(4_000_000_000)), big.NewInt(rnd.Int63n(1_000_000_000)), key))
				case 3:
					txs = append(txs, pricedTransaction(nonce, 21000, big.NewInt(0), key))
				case 4:
					txs = append(txs, types.NewTx(&types.LegacyTx{
						Nonce: nonce, GasPrice: big.NewInt(rnd.Int63n(4_000_000_000)), Gas: 21000,
						To: &to, Value: big.NewInt(rnd.Int63n(1 << 40)),
						V: big.NewInt(0), R: big.NewInt(0), S: big.NewInt(0),
					}))
				}
			}
		}
		pool.Add(txs, false)
		pool.Pending(txpool.PendingFilter{})
		pool.Content()
		pool.Stats()
	}
	pool.SetGasTip(big.NewInt(rnd.Int63n(4_000_000_000)))
	pool.Pending(txpool.PendingFilter{})
	pool.Content()
	pool.Stats()
}
