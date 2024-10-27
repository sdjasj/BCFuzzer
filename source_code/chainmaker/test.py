import random

import yaml

import util

with open("origin_config.yaml", 'r', encoding='utf-8') as file:
    config = yaml.safe_load(file)

# yaml_str = yaml.dump(config, default_flow_style=False)
# with open("origin_config.yaml", 'w', encoding='utf-8') as file:
#     file.write(yaml_str)
keys = util.get_all_keys(config, {})
print(len(keys))
for ele in util.get_all_keys(config, {}):
    if ele.startswith("txpool"):
        print(ele)

# def compare_dicts(dict1, dict2):
#     diff_cnt = 0
#     for key in dict1:
#         if key not in dict2:
#             diff_cnt += 1
#         elif dict1[key] != dict2[key]:
#             if isinstance(dict1[key], dict) and isinstance(dict2[key], dict):
#                 diff_cnt += compare_dicts(dict1[key], dict2[key])
#             else:
#                 diff_cnt += 1
#     return diff_cnt
#
#
# with open('origin_config.yaml', 'r', encoding='utf-8') as file1, open('config.yaml', 'r', encoding='utf-8') as file2:
#     yml1 = yaml.safe_load(file1)
#     yml2 = yaml.safe_load(file2)
#
# diffs = compare_dicts(yml1, yml2)
# print(diffs)
# config_pool = [(0, 'config_a'), (5, 'config_b'), (10, 'config_c')]
#
# # 提取权重
# weights = [config[0] for config in config_pool]
# cur_config = random.choices(config_pool, weights=weights, k=1)[0]
# while cur_config[0] != 0:
#     cur_config = random.choices(config_pool, weights=weights, k=1)
# print("Selected config:", cur_config)
