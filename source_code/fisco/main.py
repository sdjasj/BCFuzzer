import configparser

import config_fuzzer
import rule_guided_config_fuzzer
import multinode_config_fuzzer
import rule_guided_multi_config_fuzzer

# fuzzer = rule_guided_multi_config_fuzzer.MultinodeFuzzer()
# fuzzer.fuzz()
# config = configparser.ConfigParser()
# config.read('config.ini')
# with open('new_config.ini', 'w', encoding='utf-8') as f:
#     config.write(f)
if __name__ == '__main__':
    fuzzer = config_fuzzer.MultinodeFuzzer()
    fuzzer.fuzz()
