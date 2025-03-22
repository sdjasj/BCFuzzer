import collections
import copy
import logging
import os
import random
import subprocess
import threading
import time
from multiprocessing import Pool
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

import util
from ConfigItem import ConfigType, ConfigItem

PROJECT_PATH = "/root/chainmaker-go/build/release/"
RESULT_PATH = "/root/test_evaluation3/compare_test_result/"
failure_set = collections.defaultdict(set)
success_set = collections.defaultdict(set)
lock = threading.Lock()

def add_failure_rule(random_key, new_value):
    with lock:
        failure_set[random_key].add(new_value)
        failure_count[random_key] += 1
        if failure_count[random_key] >= consistent_threshold:
            consistent_items_set.add(random_key)
        print(f"new failure rule {random_key} : {new_value}")

def add_success_rule(random_key, new_value):
    with lock:
        success_set[random_key].add(new_value)
        print(f"new success rule {random_key} : {new_value}")

def check_failure_rule(random_key, new_value):
    with lock:
        return new_value in failure_set[random_key]

def check_success_rule(random_key, new_value):
    with lock:
        return new_value in success_set[random_key]



consistent_items_set = set()
inconsistent_items_set = set()
failure_count = collections.defaultdict(int)
consistent_threshold = 10




def init_log(name, path):
    # log define

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)


    handler = logging.FileHandler(path + '{}.log'.format(name))
    handler.setLevel(logging.DEBUG)


    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)


    logger.addHandler(handler)
    return logger


class SingleNodeFuzzer:
    def __init__(self, name, root_result_path, node_type="fuzzing"):
        # name: wx-org1
        self.name = name
        # fuzzing or exploration
        self.node_type = node_type


        self.current_result_path = root_result_path + name + "/"
        self.current_result_panic_path = self.current_result_path + "panic_error/"
        self.current_result_start_error_path = self.current_result_path + "start_error/"
        self.current_result_runtime_error_path = self.current_result_path + "runtime_error/"
        self.init_resource()


        self.logger = init_log(self.name, self.current_result_path)


        self.CHECK_TIMES = 5
        self.RUN_TIME_FOR_CRASH = 20
        self.config_items = []
        self.node_path = PROJECT_PATH + "chainmaker-v2.3.4-{}.chainmaker.org/".format(self.name)
        self.config_path = self.node_path + "config/{}.chainmaker.org/".format(self.name)
        self.bin_path = self.node_path + "bin/"
        self.origin_config_file = self.config_path + "origin_chainmaker.yml"
        self.current_config_file = self.config_path + "chainmaker.yml"
        self.panic_log_path = self.bin_path + "panic.log"
        self.config_pool = []
        self.config_all_keys = []
        self.config_all_values = []
        self.type_to_values_map = collections.defaultdict(list)
        self.test_modes = ["change", "delete"]
        self.parameter_num_change_per_time = 2
        self.value_to_config_key = dict()
        self.unique_panic_files = set()
        self.origin_key_to_value = dict()
        self.lock = threading.Lock()

        self.load_config()
        self.logger.info("init singleNodeFuzzer {}.....".format(name))
        self.fuzz_round = 0

    def init_resource(self):
        os.makedirs(self.current_result_path)
        os.makedirs(self.current_result_panic_path)
        os.makedirs(self.current_result_start_error_path)
        os.makedirs(self.current_result_runtime_error_path)

    def load_config(self):

        os.system("cp {} {}".format(self.current_config_file, self.origin_config_file))


        with open(self.current_config_file, 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file)

        self.config_pool.append(config)


        self.config_all_keys = util.get_all_keys(config, self.value_to_config_key, self.origin_key_to_value)
        self.config_all_values = util.get_all_values(config)


        for ele in self.config_all_values:
            self.type_to_values_map[type(ele)].append(ele)


        for key, value in self._flatten_config(config).items():
            config_type = self._infer_config_type(key, value)
            self.config_items.append(ConfigItem(key=key, value=value, config_type=config_type))

    def _flatten_config(self, config, parent_key='', separator='.'):

        items = {}
        for k, v in config.items():
            new_key = f"{parent_key}{separator}{k}" if parent_key else k
            if isinstance(v, dict):
                items.update(self._flatten_config(v, new_key, separator))
            else:
                items[new_key] = v
        return items

    def _infer_config_type(self, key, config_type_map_file):

        import json


        with open(config_type_map_file, 'r', encoding='utf-8') as file:
            config_type_map = json.load(file)


        for pattern, config_type in config_type_map.items():
            if pattern.lower() in key.lower():
                return ConfigType(config_type)


        return ConfigType.Other

    def restart_node(self):
        with lock:
            os.chdir(self.bin_path)
            os.system('./restart.sh')

    def check_panic(self, yaml_str):
        content = util.extract_panic_section(self.panic_log_path)
        if content is None:
            return False
        unique_key = util.check_sum_of_panic(content)
        if unique_key not in self.unique_panic_files:
            self.unique_panic_files.add(unique_key)
            cur_time = time.time()
            directory = self.current_result_panic_path + str(cur_time) + "/"
            os.mkdir(directory)
            with open(directory + "panic_error" + ".yml", 'w', encoding='utf-8') as f:
                f.write(yaml_str)
            os.system('cp {} {}'.format(self.panic_log_path, directory + '/'))
        return True


    def write_config_for_analysis(self, d, new_config):
        command = "ps -ef | grep chainmaker | grep -v grep | grep {}.chainmaker.org".format(self.name)

        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        if result.stdout.strip():
            self.logger.info("start successful")
            return True
        else:
            self.logger.info("maybe " + d)
            file_name = d + ".yml" + str(time.time())
            yaml_str = yaml.dump(new_config, default_flow_style=False)
            error_dir = self.current_result_path + d + '/{}/'.format(str(time.time()))
            os.makedirs(error_dir)
            with open(error_dir + file_name, 'w', encoding='utf-8') as f:
                f.write(yaml_str)
            os.system("cp {} {}".format(self.panic_log_path, error_dir))
        return False

    def generator_items(self):
        random_key = random.choice(self.config_all_keys)
        while self.value_to_config_key[random_key] is list:
            random_key = random.choice(self.config_all_keys)
        self.logger.info(random_key)
        if self.value_to_config_key[random_key] is int:
            new_value = random.choice([(1 << 31) - 1, -1, 0, 1 - (1 << 31) - 1, -(1 << 31),
                                       random.choice(
                                           self.type_to_values_map[self.value_to_config_key[random_key]])])
        elif self.value_to_config_key[random_key] is float:
            new_value = random.choice([random.uniform(0, 1), random.uniform(1, 100000000)])
        else:
            new_value = random.choice(self.type_to_values_map[self.value_to_config_key[random_key]])
            if random.random() < 0.1:
                new_value = random.choice(['www.baidu.com', '/awfawjfo/dawf', '~/awfawf'])
        return random_key, new_value

    def generate_value_by_key(self, random_key):

        matching_item = next((item for item in self.config_items if item.key == random_key), None)

        if matching_item is None:
            raise KeyError(f"Key '{random_key}' ")


        matching_item.mutate()
        return matching_item.value

    def generator_keys(self):
        random_key = random.choice(self.config_all_keys)
        while self.value_to_config_key[random_key] is list:
            random_key = random.choice(self.config_all_keys)
        return random_key

    def check_alive(self):
        command = "ps -ef | grep chainmaker | grep -v grep | grep {}.chainmaker.org".format(self.name)

        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        if result.stdout.strip():
            return True
        return False


    def fuzz(self):
        with self.lock:
            if self.node_type == "fuzzing":
                self.fuzz_round += 1
                self.logger.info("it's the {} time of test".format(self.fuzz_round))
                cur_config = random.choice(self.config_pool)
                new_config = copy.deepcopy(cur_config)
                mode = random.choice(self.test_modes)
                random_key = self.generator_keys()

                while random_key in consistent_items_set and random.random() > 0.1:
                    random_key = self.generator_keys()
                    self.logger.info(f"Skipping consistent failure item: {random_key}")

                if mode == "change" or mode == "delete" and check_failure_rule(random_key, "delete"):
                    new_value = self.generate_value_by_key(random_key)
                    cnt = 0
                    while cnt < 5 and check_failure_rule(random_key, new_value):
                        new_value = self.generate_value_by_key(random_key)
                        cnt += 1
                    if cnt == 5:
                        new_value = self.origin_key_to_value[random_key]
                    self.logger.info(new_value)
                    util.set_value_by_path(new_config, random_key, new_value)

                    yaml_str = yaml.dump(new_config, default_flow_style=False)
                    with open(self.current_config_file, 'w', encoding='utf-8') as file:
                        file.write(yaml_str)
                    self.restart_node()
                    time.sleep(6)

                    if self.check_panic(yaml_str):
                        add_failure_rule(random_key, new_value)
                        self.logger.info(
                            "it's the {} time of test, and it panic fail...........".format(self.fuzz_round))
                        return

                    if not self.check_alive():
                        add_failure_rule(random_key, new_value)
                        self.logger.info(
                            "it's the {} time of test, and it fail but not panic...........".format(self.fuzz_round))
                        return

                    for i in range(self.CHECK_TIMES):
                        time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                        res = self.write_config_for_analysis('runtime_error', new_config)
                        if not res:
                            add_failure_rule(random_key, new_value)
                            self.logger.info("it's the {} time of test, and it fail...........".format(self.fuzz_round))
                            return
                elif mode == "delete":
                    util.delete_key(new_config, random_key)
                    yaml_str = yaml.dump(new_config, default_flow_style=False)
                    with open(self.current_config_file, 'w', encoding='utf-8') as file:
                        file.write(yaml_str)
                    self.restart_node()
                    time.sleep(10)
                    if self.check_panic(yaml_str):
                        add_failure_rule(random_key, "delete")
                        self.logger.info(
                            "it's the {} time of test, and it panic fail...........".format(self.fuzz_round))
                        return

                    if not self.check_alive():
                        add_failure_rule(random_key, "delete")
                        self.logger.info(
                            "it's the {} time of test, and it fail but not panic...........".format(self.fuzz_round))
                        return

                    for i in range(self.CHECK_TIMES):
                        time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                        res = self.write_config_for_analysis('runtime_error', new_config)
                        if not res:
                            add_failure_rule(random_key, "delete")
                            self.logger.info("it's the {} time of test, and it fail...........".format(self.fuzz_round))
                            return
                inconsistent_items_set.add(random_key)
                consistent_items_set.remove(random_key)
                self.config_pool.append(new_config)
                self.logger.info("it's the {} time of test, and it success".format(self.fuzz_round))
            elif self.node_type == "exploration":
                self.fuzz_round += 1
                self.logger.info("it's the {} time of test".format(self.fuzz_round))
                cur_config = random.choice(self.config_pool)
                new_config = copy.deepcopy(cur_config)
                mode = random.choice(self.test_modes)
                random_key = self.generator_keys()

                if mode == "change" or mode == "delete" and check_failure_rule(random_key, "delete"):
                    new_value = self.generate_value_by_key(random_key)
                    cnt = 0
                    while cnt < 5 and (check_failure_rule(random_key, new_value) or check_success_rule(random_key, new_value)):
                        new_value = self.generate_value_by_key(random_key)
                        cnt += 1
                    if cnt == 5:
                        new_value = self.origin_key_to_value[random_key]
                    self.logger.info(new_value)
                    util.set_value_by_path(new_config, random_key, new_value)

                    yaml_str = yaml.dump(new_config, default_flow_style=False)
                    with open(self.current_config_file, 'w', encoding='utf-8') as file:
                        file.write(yaml_str)
                    self.restart_node()
                    time.sleep(6)

                    if self.check_panic(yaml_str):
                        add_failure_rule(random_key, new_value)
                        self.logger.info(
                            "it's the {} time of test, and it panic fail...........".format(self.fuzz_round))
                        return

                    if not self.check_alive():
                        add_failure_rule(random_key, new_value)
                        self.logger.info(
                            "it's the {} time of test, and it fail but not panic...........".format(self.fuzz_round))
                        return

                    for i in range(self.CHECK_TIMES):
                        time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                        res = self.write_config_for_analysis('runtime_error', new_config)
                        if not res:
                            add_failure_rule(random_key, new_value)
                            self.logger.info("it's the {} time of test, and it fail...........".format(self.fuzz_round))
                            return
                elif mode == "delete":
                    util.delete_key(new_config, random_key)
                    yaml_str = yaml.dump(new_config, default_flow_style=False)
                    with open(self.current_config_file, 'w', encoding='utf-8') as file:
                        file.write(yaml_str)
                    self.restart_node()
                    time.sleep(10)
                    if self.check_panic(yaml_str):
                        add_failure_rule(random_key, "delete")
                        self.logger.info(
                            "it's the {} time of test, and it panic fail...........".format(self.fuzz_round))
                        return

                    if not self.check_alive():
                        add_failure_rule(random_key, "delete")
                        self.logger.info(
                            "it's the {} time of test, and it fail but not panic...........".format(self.fuzz_round))
                        return

                    for i in range(self.CHECK_TIMES):
                        time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                        res = self.write_config_for_analysis('runtime_error', new_config)
                        if not res:
                            add_failure_rule(random_key, "delete")
                            self.logger.info("it's the {} time of test, and it fail...........".format(self.fuzz_round))
                            return
                inconsistent_items_set.add(random_key)
                consistent_items_set.remove(random_key)
                self.config_pool.append(new_config)
                self.logger.info("it's the {} time of test, and it success".format(self.fuzz_round))


class MultinodeFuzzer:
    def __init__(self):
        self.clean_flag = False

        # root path for single node fuzzer
        self.cur_result_path = RESULT_PATH + "test_result_" + str(time.time()) + "/"
        self.init_resource()

        self.logger = init_log("multinodeFuzzer", self.cur_result_path)
        self.single_node_num = 13
        self.single_node_names = []
        for i in range(1, self.single_node_num + 1):
            self.single_node_names.append("wx-org" + str(i))
        # init singleNodeFuzzer
        # 33% for Byzantine
        self.selected_single_node_num = 4
        self.selected_single_node_names = random.sample(self.single_node_names, self.selected_single_node_num)
        self.selected_single_nodes = []
        self.lock = threading.Lock()
        self.logger.info("init singleNodeFuzzer.....")
        cnt = 0
        for name in self.selected_single_node_names:
            if cnt != 0:
                self.selected_single_nodes.append(SingleNodeFuzzer(name=name, root_result_path=self.cur_result_path, node_type="fuzzing"))
            else:
                self.selected_single_nodes.append(
                    SingleNodeFuzzer(name=name, root_result_path=self.cur_result_path, node_type="exploration"))
            self.logger.info("init fuzzer of node {} successfully".format(name))
            cnt += 1
        self.executor = ThreadPoolExecutor(max_workers=len(self.selected_single_nodes))
        self.logger.info("init fuzzer of all nodes successfully")

        self.fuzz_count = 0
        self.check_interval = 20

    def check_single_node_alive(self, name):
        command = "ps -ef | grep chainmaker | grep -v grep | grep {}.chainmaker.org".format(name)

        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.stdout.strip():
            return True
        return False

    def check_normal_nodes_alive(self):
        for node in self.single_node_names:
            if node not in self.selected_single_node_names:
                if not self.check_single_node_alive(node):
                    self.logger.info("normal node {} panic!!!!!!!!!!!!!".format(node))
                    return False
        return True

    def init_resource(self):
        os.makedirs(self.cur_result_path)

    def format_set_content(self, title, items_set, include_count=True):
        """Format the output content of sets"""
        output = [
            "=" * 70,
            f"{title} {'(' + str(len(items_set)) + ' items)' if include_count else ''}",
            "=" * 70,
        ]
        
        if not items_set:
            output.append("(Empty)")
        else:
            for item in sorted(items_set):
                if item in failure_count:
                    output.append(f"- {item:<40} [Failure Count: {failure_count[item]}]")
                else:
                    output.append(f"- {item}")
                    
        output.append("\n")
        return "\n".join(output)

    def fuzz(self):

        futures = {self.executor.submit(obj.fuzz): obj for obj in self.selected_single_nodes}

        while True:
            for future in as_completed(futures):
                obj = futures[future]
                
                try:
                    result = future.result()
                    
                    with self.lock:
                        self.fuzz_count += 1
                        
                        if self.fuzz_count % self.check_interval == 0:
                            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                            result_file = os.path.join(self.cur_result_path, f'sets_status_{self.fuzz_count}.txt')
                            
                            with open(result_file, 'w', encoding='utf-8') as f:
                                # Write title and statistics
                                header = [
                                    "#" * 70,
                                    f"Configuration Item Consistency Test Status Report",
                                    f"Generated Time: {timestamp}",
                                    f"Total Test Count: {self.fuzz_count}",
                                    "#" * 70,
                                    "\n"
                                ]
                                f.write("\n".join(header))
                                
                                # Write consistent configuration items set
                                f.write(self.format_set_content(
                                    "Must-be-consistent Configuration Items (Modifications cause node failure)",
                                    consistent_items_set
                                ))
                                
                                # Write inconsistent configuration items set
                                f.write(self.format_set_content(
                                    "Can-be-inconsistent Configuration Items (Node works normally after modifications)",
                                    inconsistent_items_set
                                ))
                                
                                # Write summary statistics
                                summary = [
                                    "-" * 70,
                                    "Summary Statistics:",
                                    f"- Number of must-be-consistent items: {len(consistent_items_set)}",
                                    f"- Number of can-be-inconsistent items: {len(inconsistent_items_set)}",
                                    f"- Failure threshold setting: {consistent_threshold} (Items are marked as must-be-consistent after reaching this threshold)",
                                    "-" * 70
                                ]
                                f.write("\n".join(summary))
                            
                            self.logger.info(f"Consistency test status report generated: {result_file}")
                        
                        futures[self.executor.submit(obj.fuzz)] = obj
                        self.logger.info(f"Re-submit test task for node {obj.name}")
                
                except Exception as e:
                    self.logger.error(f"Node {obj.name} test execution error: {str(e)}")

