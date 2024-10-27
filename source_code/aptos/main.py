import rule_guidede_config_fuzz
import config_fuzz

if __name__ == '__main__':
    fuzzer = config_fuzz.MultinodeFuzzer()
    fuzzer.fuzz()
