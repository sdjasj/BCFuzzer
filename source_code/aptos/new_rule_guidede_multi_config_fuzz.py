import collections
import copy
import logging
import os
import random
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import Pool

import yaml

import util
from ConfigItem import ConfigType, ConfigItem

PROJECT_PATH = "/tmp/"
RESULT_PATH = "/root/test_evaluation3/test_result/"


def init_log(name, path):
    # log define
    # 为每个对象创建一个独立的 logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # 为每个对象创建一个独立的文件处理器
    handler = logging.FileHandler(path + '{}.log'.format(name))
    handler.setLevel(logging.DEBUG)

    # 定义日志格式，包含自定义时间格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'  # 自定义时间格式
    )
    handler.setFormatter(formatter)

    # 将处理器添加到 logger 中
    logger.addHandler(handler)
    return logger


failure_set = collections.defaultdict(set)
success_set = collections.defaultdict(set)
lock = threading.Lock()

def add_failure_rule(random_key, new_value):
    with lock:  # 确保线程安全
        failure_set[random_key].add(new_value)
        failure_count[random_key] += 1
        if failure_count[random_key] >= consistent_threshold:
            consistent_items_set.add(random_key)
        print(f"new failure rule {random_key} : {new_value}")

def add_success_rule(random_key, new_value):
    with lock:  # 确保线程安全
        success_set[random_key].add(new_value)
        print(f"new success rule {random_key} : {new_value}")

def check_failure_rule(random_key, new_value):
    with lock:  # 确保线程安全
        return new_value in failure_set[random_key]

def check_success_rule(random_key, new_value):
    with lock:  # 确保线程安全
        return new_value in success_set[random_key]



consistent_items_set = set()  # 存储导致节点无法启动超过10次的配置项
inconsistent_items_set = set()  # 存储允许节点重新启动的配置项
failure_count = collections.defaultdict(int)  # 记录每个配置项失败的次数
consistent_threshold = 10  # 失败阈值


class SingleNodeFuzzer:
    def __init__(self, name, root_result_path=RESULT_PATH + "test_result_" + str(time.time()) + "/", node_type="fuzzing"):
        # name: 0/1/2/3
        self.name = name
        self.node_type = node_type

        # 记录测试失败的目录
        self.current_result_path = root_result_path + name + "/"
        self.current_result_panic_path = self.current_result_path + "panic_error/"
        self.current_result_start_error_path = self.current_result_path + "start_error/"
        self.current_result_runtime_error_path = self.current_result_path + "runtime_error/"
        self.init_resource()
        self.lock = threading.Lock()

        # 初始化日志
        self.logger = init_log(self.name, self.current_result_path)

        # 指定检查次数
        self.CHECK_TIMES = 5
        self.RUN_TIME_FOR_CRASH = 20

        tmp_dirs = [os.path.join(PROJECT_PATH, d) for d in os.listdir(PROJECT_PATH) if
                    d.startswith('.tmp') and os.path.isdir(os.path.join(PROJECT_PATH, d))]
        if tmp_dirs:
            self.tmp_dir = max(tmp_dirs, key=os.path.getctime)
            self.node_path = self.tmp_dir + "/{}/".format(self.name)
            print("node_path is {}".format(self.node_path))
        else:
            print("not find node_path...., exit")
            exit(0)
        self.origin_config_file = self.node_path + "origin_node.yaml"
        self.current_config_file = self.node_path + "node.yaml"
        self.panic_log_path = self.node_path + "log"
        os.makedirs(self.name)
        ### generate script
        with open("./{}/start.sh".format(self.name), 'w', encoding='utf-8') as f:
            f.write("/root/aptos-core/target/debug/aptos-node -f {} > {} 2>&1 &".format(self.current_config_file,
                                                                                        self.panic_log_path))
        os.system('chmod 777 ./{}/start.sh'.format(self.name))
        with open("./{}/stop.sh".format(self.name), 'w', encoding='utf-8') as f:
            f.write("""pid=`ps -ef | grep "{}" | grep -v grep |  awk  '{{print $2}}'`
        if [ ! -z ${{pid}} ];
        then
            kill -9 $pid
            echo "aptos is stopping..."
        fi""".format(self.node_path))
        os.system('chmod 777 ./{}/stop.sh'.format(self.name))
        self.fuzz_round = 0

        self.config_pool = []
        self.config_all_keys = []
        self.config_all_values = []
        self.type_to_values_map = collections.defaultdict(list)
        self.test_modes = ["change", "delete"]
        self.parameter_num_change_per_time = 2
        self.value_to_config_key = dict()
        self.unique_panic_files = set()
        self.origin_key_to_value = dict()
        self.config_items = []
        self.load_config()
        self.logger.info("init singleNodeFuzzer {}.....".format(name))

    def init_resource(self):
        os.makedirs(self.current_result_path)
        os.makedirs(self.current_result_panic_path)
        os.makedirs(self.current_result_start_error_path)
        os.makedirs(self.current_result_runtime_error_path)

    def load_config(self):
        # 备份原始配置文件以便恢复
        os.system("cp {} {}".format(self.current_config_file, self.origin_config_file))

        # 读取并解析 YAML 文件
        with open(self.current_config_file, 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file)

        self.config_pool.append(config)

        # 获取所有键和值
        self.config_all_keys = util.get_all_keys(config, self.value_to_config_key, self.origin_key_to_value)
        self.config_all_values = util.get_all_values(config)

        # 按类型组织值
        for ele in self.config_all_values:
            self.type_to_values_map[type(ele)].append(ele)

        # 根据键值对初始化 ConfigItem
        for key, value in self._flatten_config(config).items():
            config_type = self._infer_config_type(key, value)  # 推断配置类型
            self.config_items.append(ConfigItem(key=key, value=value, config_type=config_type))

    def _flatten_config(self, config, parent_key='', separator='.'):
        """
        将嵌套的配置字典展平为单层字典。

        :param config: 嵌套的字典配置。
        :param parent_key: 父键名（用于递归）。
        :param separator: 键之间的分隔符。
        :return: 展平后的字典。
        """
        items = {}
        for k, v in config.items():
            new_key = f"{parent_key}{separator}{k}" if parent_key else k
            if isinstance(v, dict):
                items.update(self._flatten_config(v, new_key, separator))
            else:
                items[new_key] = v
        return items

    def _infer_config_type(self, key, config_type_map_file):
        """
        根据键从 JSON 文件中获取配置类型。

        :param key: 配置项的键。
        :param config_type_map_file: 包含配置类型映射的 JSON 文件路径。
        :return: ConfigType 枚举值或 ConfigType.Other。
        """
        import json

        # 加载 JSON 文件
        with open(config_type_map_file, 'r', encoding='utf-8') as file:
            config_type_map = json.load(file)

        # 从 JSON 中查找匹配的类型
        for pattern, config_type in config_type_map.items():
            if pattern.lower() in key.lower():
                return ConfigType(config_type)

        # 默认返回 Other
        return ConfigType.Other
    def restart_node(self):
        os.system('./{}/stop.sh'.format(self.name))
        time.sleep(3)
        os.system('./{}/start.sh'.format(self.name))

    def check_alive(self):
        command = "ps -ef | grep {} | grep -v grep".format(self.current_config_file)
        # 使用 subprocess.run 执行命令并捕获输出
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.stdout.strip():
            return True
        return False

    def check_panic(self, yaml_str):
        if not self.check_alive():
            if yaml_str not in self.unique_panic_files:
                self.unique_panic_files.add(yaml_str)
                cur_time = time.time()
                directory = self.current_result_panic_path + str(cur_time) + "/"
                os.mkdir(directory)
                with open(directory + "panic_error" + ".yaml", 'w', encoding='utf-8') as f:
                    f.write(yaml_str)
                os.system('cp {} {}'.format(self.panic_log_path, directory + '/'))
            return True
        return False

    def write_config_for_analysis(self, d, new_config):
        command = "ps -ef | grep {} | grep -v grep".format(self.current_config_file)
        # 使用 subprocess.run 执行命令并捕获输出
        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        if result.stdout.strip():
            self.logger.info("start successful")
            return True
        else:
            self.logger.info("maybe " + d)
            file_name = d + ".yaml" + str(time.time())
            yaml_str = yaml.dump(new_config, default_flow_style=False)
            error_dir = self.current_result_path + d + '/{}/'.format(str(time.time()))
            os.makedirs(error_dir)
            with open(error_dir + file_name, 'w', encoding='utf-8') as f:
                f.write(yaml_str)
            os.system("cp {} {}".format(self.panic_log_path, error_dir))
        return False

    def generate_value_by_key(self, random_key):
        """
        根据随机键找出对应的 ConfigItem，并通过变异生成新值。

        :param random_key: 随机选择的键。
        :return: 变异后的新值。
        """
        # 在配置项列表中查找匹配的 ConfigItem
        matching_item = next((item for item in self.config_items if item.key == random_key), None)

        if matching_item is None:
            raise KeyError(f"Key '{random_key}' 不存在于配置项中。")

        # 对匹配的 ConfigItem 进行变异并返回新值
        matching_item.mutate()
        return matching_item.value

    def generator_keys(self):
        random_key = random.choice(self.config_all_keys)
        while self.value_to_config_key[random_key] is list:
            random_key = random.choice(self.config_all_keys)
        return random_key

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
                    random_key = random.choice(self.config_all_keys)
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
                    random_key = random.choice(self.config_all_keys)
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

        # root path for single node fuzzer
        self.cur_result_path = RESULT_PATH + "test_result_" + str(time.time()) + "/"

        self.init_resource()
        self.lock = threading.Lock()

        self.logger = init_log("multinodeFuzzer", self.cur_result_path)
        self.logger.info("init singleNodeFuzzer.....")
        self.selected_single_nodes = []
        for i in range(4):
            if i == 0:
                self.selected_single_nodes.append(SingleNodeFuzzer("{}".format(i), self.cur_result_path, "exploration"))
            else:
                self.selected_single_nodes.append(SingleNodeFuzzer("{}".format(i), self.cur_result_path, "fuzzing"))
        self.executor = ThreadPoolExecutor(max_workers=len(self.selected_single_nodes))
        self.logger.info("init fuzzer of all nodes successfully")

        self.fuzz_count = 0
        self.check_interval = 20

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
        # 将所有节点的fuzz()函数提交给线程池
        futures = {self.executor.submit(obj.fuzz): obj for obj in self.selected_single_nodes}
        # 持续监控fuzz()的执行状态
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

# class MultinodeFuzzer:
#     def __init__(self):
#         self.clean_flag = False
#
#         # root path for single node fuzzer
#         self.cur_result_path = RESULT_PATH + "test_result_" + str(time.time()) + "/"
#         self.init_resource()
#
#         self.logger = init_log("multinodeFuzzer", self.cur_result_path)
#         self.single_node_num = 13
#         self.single_node_names = []
#         for i in range(1, self.single_node_num + 1):
#             self.single_node_names.append("wx-org" + str(i))
#         # init singleNodeFuzzer
#         # 33% for Byzantine
#         self.selected_single_node_num = self.single_node_num // 3
#         self.selected_single_node_names = random.sample(self.single_node_names, self.selected_single_node_num)
#         self.selected_single_nodes = []
#         self.logger.info("init singleNodeFuzzer.....")
#         for name in self.selected_single_node_names:
#             self.selected_single_nodes.append(SingleNodeFuzzer(name=name, root_result_path=self.cur_result_path))
#             self.logger.info("init fuzzer of node {} successfully".format(name))
#         self.logger.info("init fuzzer of all nodes successfully")
#
#
#     def check_single_node_alive(self, name):
#         command = "ps -ef | grep chainmaker | grep -v grep | grep {}.chainmaker.org".format(name)
#         # 使用 subprocess.run 执行命令并捕获输出
#         result = subprocess.run(command, shell=True, capture_output=True, text=True)
#         if result.stdout.strip():
#             return True
#         return False
#
#     def check_normal_nodes_alive(self):
#         for node in self.single_node_names:
#             if node not in self.selected_single_node_names:
#                 if not self.check_single_node_alive(node):
#                     self.logger.info("normal node {} panic!!!!!!!!!!!!!".format(node))
#                     return False
#         return True
#
#     def init_resource(self):
#         os.makedirs(self.cur_result_path)
#
#     def fuzz_node(self, args):
#         node, i = args
#         node.fuzz(i)
#
#     def fuzz(self):
#         for i in range(100000):
#             random.shuffle(self.selected_single_nodes)
#
#             # 创建一个包含所有节点的 (node, i) 参数对的列表
#             tasks = [(node, i) for node in self.selected_single_nodes]
#             self.logger.info("fuzz all the selected nodes.......")
#             # 使用多进程池来并行处理 fuzz 操作
#             with Pool() as pool:
#                 pool.map(self.fuzz_node, tasks)  # 等待所有进程完成
#             time.sleep(20)
#             self.logger.info("finish multinode fuzz for {} times".format(i + 1))
#             if not self.check_normal_nodes_alive():
#                 return
