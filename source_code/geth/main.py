import configparser

import config_fuzzer
import no_ruleset_config_fuzzer
import rule_guided_config_fuzzer

# fuzzer = no_ruleset_config_fuzzer.SingleNodeFuzzer("node7")
# for i in range(100000):
#     fuzzer.fuzz(i + 1)
if __name__ == '__main__':
    fuzzer = config_fuzzer.MultinodeFuzzer()
    fuzzer.fuzz()

