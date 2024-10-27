import multinode_config_fuzz
import rule_guided_config_fuzz
from multinode_config_fuzz import MultinodeFuzzer
import co_config_fuzz


if __name__ == '__main__':
    # multinodeFuzzer = MultinodeFuzzer()
    # multinodeFuzzer.fuzz()
    fuzzer = rule_guided_config_fuzz.MultinodeFuzzer()
    fuzzer.fuzz()

