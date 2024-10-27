# 获取所有键的路径
import re


def get_all_keys(config, value_of_config_key, origin_key_to_value):
    keys = []
    for section in config.sections():
        for key, value in config.items(section):
            full_key = f"{section}_{key}"
            keys.append(full_key)
            value_of_config_key[full_key] = type(value)
            origin_key_to_value[full_key] = value
    return keys


# 根据路径设置 .ini 配置中的值
# 根据路径设置 .ini 配置中的值，支持多级路径
def set_value_by_path(config, path, value):
    # 分离路径中的 Section 和 Key
    keys = path.split('_')

    # 如果路径不止两级，说明需要逐级处理嵌套
    if len(keys) >= 2:
        section = keys[0]
        key = '_'.join(keys[1:])  # 将剩余部分作为 Key

        # 如果 section 不存在，则创建
        if not config.has_section(section):
            config.add_section(section)

        # 设置对应的值
        config.set(section, key, str(value))
    else:
        print(f"Invalid path format: {path}")


# 根据路径获取 .ini 配置中的值
def get_value_by_path(config, path):
    section, key = path.split('_')
    return config.get(section, key)


# 获取所有的值
def get_all_values(config):
    values = []
    for section in config.sections():
        for _, value in config.items(section):
            values.append(value)
    return values


# 删除 ini 配置中的键
def delete_key(config, key_path):
    """
    删除 ini 文件中的指定键，路径格式为 'section.key'。
    """
    keys = key_path.split('_')

    if len(keys) == 2:  # 确保路径格式为 'section.key'
        section, key = keys
        if config.has_section(section) and config.has_option(section, key):
            config.remove_option(section, key)


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
