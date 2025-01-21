import configparser
import new_rule_guided_multi_config_fuzzer

# fuzzer = no_ruleset_config_fuzzer.SingleNodeFuzzer("node7")
# for i in range(100000):
#     fuzzer.fuzz(i + 1)
if __name__ == '__main__':
    fuzzer = new_rule_guided_multi_config_fuzzer.MultinodeFuzzer()
    fuzzer.fuzz()

