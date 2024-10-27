import re


def get_all_keys(d, value_of_config_key, origin_key_to_value, parent_key=''):
    keys = []
    for k, v in d.items():
        full_key = f"{parent_key}.{k}" if parent_key else k
        if not isinstance(v, dict):
            keys.append(full_key)
            value_of_config_key[full_key] = type(v)
            origin_key_to_value[full_key] = v
        if isinstance(v, dict):
            keys.extend(get_all_keys(v, value_of_config_key, origin_key_to_value, full_key))
    return keys


# 函数根据路径设置字典中对应的值
def set_value_by_path(d, path, value):
    keys = path.split('.')
    for key in keys[:-1]:
        d = d[key]
    d[keys[-1]] = value


def get_value_by_path(d, path):
    keys = path.split('.')
    for key in keys[:-1]:
        d = d[key]
    return d[keys[-1]]


def get_all_values(d):
    values = []
    if isinstance(d, dict):
        for v in d.values():
            values.extend(get_all_values(v))
    elif isinstance(d, list):
        for item in d:
            values.extend(get_all_values(item))
    else:
        values.append(d)
    return values


# 递归函数，删除嵌套字典中的键
def delete_key(d, key_path):
    keys = key_path.split('.')
    for key in keys[:-1]:
        d = d.get(key, {})
    if keys[-1] in d:
        del d[keys[-1]]


def remove_0x_and_plus_offset(content):
    # with open(file_path, 'r', encoding='utf-8') as file:
    #     content = file.read()

    # 移除 '0x' 后面紧跟的数字
    content = re.sub(r'0x[0-9a-fA-F]+', '', content)

    # 移除 '+0x' 后面紧跟的数字
    content = re.sub(r'\+0x[0-9a-fA-F]+', '', content)

    return content


def extract_panic_section(file_path):
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as file:
        content = file.read()

    # 查找 "panic" 字符串的起始位置
    panic_index = content.find("panic")

    if panic_index != -1:
        # 截取从 "panic" 开始到文件末尾的内容
        panic_section = content[panic_index:]
        return panic_section
    else:
        return None


def check_sum_of_panic(content):
    unique_key = remove_0x_and_plus_offset(content)
    return unique_key


def compare_dicts(dict1, dict2):
    diff_cnt = 0
    for key in dict1:
        if key not in dict2:
            diff_cnt += 1
        elif dict1[key] != dict2[key]:
            if isinstance(dict1[key], dict) and isinstance(dict2[key], dict):
                diff_cnt += compare_dicts(dict1[key], dict2[key])
            else:
                diff_cnt += 1
    return diff_cnt
