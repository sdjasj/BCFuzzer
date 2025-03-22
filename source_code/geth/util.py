
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



def set_value_by_path(config, path, value):

    keys = path.split('_')


    if len(keys) >= 2:
        section = keys[0]
        key = '_'.join(keys[1:])


        if not config.has_section(section):
            config.add_section(section)


        config.set(section, key, str(value))
    else:
        print(f"Invalid path format: {path}")



def get_value_by_path(config, path):
    section, key = path.split('_')
    return config.get(section, key)



def get_all_values(config):
    values = []
    for section in config.sections():
        for _, value in config.items(section):
            values.append(value)
    return values



def delete_key(config, key_path):

    keys = key_path.split('_')

    if len(keys) == 2:
        section, key = keys
        if config.has_section(section) and config.has_option(section, key):
            config.remove_option(section, key)


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
