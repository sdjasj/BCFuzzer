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



def delete_key(d, key_path):
    keys = key_path.split('.')
    for key in keys[:-1]:
        d = d.get(key, {})
    if keys[-1] in d:
        del d[keys[-1]]


def remove_0x_and_plus_offset(content):
    # with open(file_path, 'r', encoding='utf-8') as file:
    #     content = file.read()


    content = re.sub(r'0x[0-9a-fA-F]+', '', content)


    content = re.sub(r'\+0x[0-9a-fA-F]+', '', content)

    return content


def extract_panic_section(file_path):
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as file:
        content = file.read()


    panic_index = content.find("panic")

    if panic_index != -1:

        panic_section = content[panic_index:]
        return panic_section
    else:
        return None


def check_sum_of_panic(content):
    unique_key = remove_0x_and_plus_offset(content)
    return unique_key
