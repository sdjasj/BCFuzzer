// Seeded mutation library for the chainmaker transaction scheduler.
// Kinds: gas-limit extremes (0/max/overflow), empty/huge parameter maps,
// duplicate tx ids, batch sizes, sender variants, and a random mix.
package scheduler

import (
	"fmt"
	"math/rand"
	"os"
	"strconv"
	"testing"

	commonPb "chainmaker.org/chainmaker/pb-go/v3/common"
	"github.com/golang/mock/gomock"
)

func bcMutSchedSeed(t *testing.T) int64 {
	seed := int64(1)
	if v := os.Getenv("BCFUZZER_SEED"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			seed = n
		}
	}
	t.Logf("mutation seed: %d", seed)
	return seed
}

func TestBcMutSchedulerTx(t *testing.T) {
	rnd := rand.New(rand.NewSource(bcMutSchedSeed(t)))

	_, txRWSetTable, txTable, snapshot, scheduler, contractId, block := prepare(t, false, false, 2, true)

	snapshot.EXPECT().ApplyTxSimContext(gomock.Any(), gomock.Any(), gomock.Any(), gomock.Any()).Return(true, 2).AnyTimes()
	snapshot.EXPECT().IsSealed().AnyTimes().Return(false)
	snapshot.EXPECT().Seal().Return().AnyTimes()
	dag := &commonPb.DAG{Vertexes: []*commonPb.DAG_Neighbor{{}}}
	snapshot.EXPECT().BuildDAG(gomock.Any(), gomock.Any()).Return(dag).AnyTimes()

	const kinds = 7
	for round := 0; round < kinds*4; round++ {
		kind := round % kinds
		var txBatch []*commonPb.Transaction
		params := map[string]string{}
		gasLimit := uint64(100000)
		contract := contractId
		txID := fmt.Sprintf("a0000000000000000000000000%04d", round)

		switch kind {
		case 0:
			gasLimit = 0
		case 1:
			gasLimit = 1 << 40
		case 2:
			params = map[string]string{}
		case 3:
			for k := 0; k < 100; k++ {
				params[fmt.Sprintf("key%03d", k)] = fmt.Sprintf("value%01000d", k)
			}
		case 4:
			txID = fmt.Sprintf("a0000000000000000000000000%04d", round%2)
		case 5:
			txBatch = append(txBatch,
				newTxWithPubKeyAndGasLimit(fmt.Sprintf("c0000000000000000000000000%04d", round), contractId, map[string]string{"k": "v"}, uint64(rnd.Intn(100000))),
				newTx(fmt.Sprintf("d0000000000000000000000000%04d", round), contractId, map[string]string{"k2": "v2"}),
			)
		case 6:
			params = map[string]string{"k": fmt.Sprintf("v%d", rnd.Intn(1000))}
			gasLimit = uint64(rnd.Intn(1 << 30))
		}
		if len(txBatch) == 0 {
			tx0 := newTx(txID, contract, params)
			tx1 := newTxWithPubKeyAndGasLimit(fmt.Sprintf("b0000000000000000000000000%04d", round), contract, params, gasLimit)
			txBatch = []*commonPb.Transaction{tx0, tx1}
		}

		txTable[0] = txBatch[0]
		txTable[1] = txBatch[1]
		txRWSetTable[0] = &commonPb.TxRWSet{
			TxId:     txBatch[0].Payload.TxId,
			TxReads:  []*commonPb.TxRead{{ContractName: contractId.Name, Key: []byte("K"), Value: []byte("V")}},
			TxWrites: []*commonPb.TxWrite{{ContractName: contractId.Name, Key: []byte("K"), Value: []byte("V")}},
		}
		txRWSetTable[1] = &commonPb.TxRWSet{
			TxId:     txBatch[1].Payload.TxId,
			TxReads:  []*commonPb.TxRead{{ContractName: contractId.Name, Key: []byte("K2"), Value: []byte("V")}},
			TxWrites: []*commonPb.TxWrite{{ContractName: contractId.Name, Key: []byte("K2"), Value: []byte("V")}},
		}

		_, _, _ = scheduler.Schedule(block, txBatch, snapshot)
	}
}
