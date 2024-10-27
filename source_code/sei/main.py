import configparser

import config_fuzzer
import multinode_config_fuzzer
import rule_guided_config_fuzzer

# fuzzer = rule_guided_config_fuzzer.SingleNodeFuzzer("seid-3")
# for i in range(100000):
#     fuzzer.fuzz(i + 1)
if __name__ == '__main__':
    fuzzer = config_fuzzer.MultinodeFuzzer()
    fuzzer.fuzz()
