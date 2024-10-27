# 获取所有键的路径
import re


def get_all_keys(config, value_of_config_key, origin_key_to_value={}):
    keys = []
    for section, items in config.items():
        if isinstance(items, dict):
            for key, value in items.items():
                full_key = f"{section}.{key}"
                keys.append(full_key)
                value_of_config_key[full_key] = type(value)
                origin_key_to_value[full_key] = value
        else:
            # Handle top-level keys
            full_key = section
            keys.append(full_key)
            value_of_config_key[full_key] = type(items)
            origin_key_to_value[full_key] = items
    return keys



def set_value_by_path(config, path, value):
    keys = path.split('.')
    d = config
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def get_value_by_path(config, path):
    keys = path.split('.')
    d = config
    for key in keys:
        d = d[key]
    return d



def get_all_values(config):
    values = []
    for section, items in config.items():
        if isinstance(items, dict):
            for _, value in items.items():
                values.append(value)
        else:
            # Handle top-level keys
            values.append(items)
    return values



def delete_key(config, key_path):
    keys = key_path.split('.')
    d = config
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
