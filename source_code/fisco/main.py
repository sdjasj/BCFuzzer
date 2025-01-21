import configparser

import new_rule_guided_multi_config_fuzzer
# fuzzer = rule_guided_multi_config_fuzzer.MultinodeFuzzer()
# fuzzer.fuzz()
# config = configparser.ConfigParser()
# config.read('config.ini')
# with open('new_config.ini', 'w', encoding='utf-8') as f:
#     config.write(f)
if __name__ == '__main__':
    fuzzer = new_rule_guided_multi_config_fuzzer.MultinodeFuzzer()
    fuzzer.fuzz()
