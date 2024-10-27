import collections
import configparser
import copy
import logging
import os
import random
import subprocess
import threading
import time
from concurrent.futures import as_completed, ThreadPoolExecutor

import toml

import util

PROJECT_PATH = "/root/.sei/config/"
RESULT_PATH = "/root/test_evaluation2/test_result/"


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

import os
import time
import subprocess
import toml
import random
import collections
import threading
import copy

class SingleNodeFuzzer:
    def __init__(self, name, node_type, image_id, root_result_path=RESULT_PATH + "test_result_" + str(time.time()) + "/"):
        self.name = name
        self.node_type = node_type
        self.image_id = image_id
        os.makedirs(root_result_path)
        self.current_result_path = root_result_path + name + "/"
        self.current_result_panic_path = self.current_result_path + "panic_error/"
        self.current_result_start_error_path = self.current_result_path + "start_error/"
        self.current_result_runtime_error_path = self.current_result_path + "runtime_error/"
        self.init_resource()

        self.logger = init_log(self.name, self.current_result_path)

        self.CHECK_TIMES = 5
        self.RUN_TIME_FOR_CRASH = 20

        self.origin_config_file = "/tmp/origin_config.toml"  # Adjusted for container
        self.current_config_file = "/tmp/config.toml"  # Adjusted for container
        self.panic_log_path = "/sei-protocol/sei-chain/build/generated/logs/{}.log".format(self.name)
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
        self.fuzzing_round = 0

        self.load_config()
        # Copy stop and start scripts to the container
        self.copy_to_docker('./stop.sh', '/tmp/stop.sh')
        self.copy_to_docker('./start.sh', '/tmp/start.sh')
        # Execute stop and start scripts inside the container
        self.exec_docker_command('chmod +x /tmp/stop.sh')
        self.exec_docker_command('chmod +x /tmp/start.sh')
        self.logger.info("init singleNodeFuzzer {}.....".format(name))

    def init_resource(self):
        self.exec_docker_command(f"mkdir -p {self.current_result_path}")
        self.exec_docker_command(f"mkdir -p {self.current_result_panic_path}")
        self.exec_docker_command(f"mkdir -p {self.current_result_start_error_path}")
        self.exec_docker_command(f"mkdir -p {self.current_result_runtime_error_path}")

    def exec_docker_command(self, command):
        full_command = f"docker exec {self.image_id} {command}"
        subprocess.run(full_command, shell=True)

    def load_config(self):
        # Copy original config file to recover from bad configurations
        self.exec_docker_command(f"cp {self.current_config_file} {self.origin_config_file}")

        # Read and parse the config file
        config_data = subprocess.run(f"docker exec {self.image_id} cat {self.current_config_file}",
                                      shell=True, capture_output=True, text=True)
        config = toml.loads(config_data.stdout)

        self.config_pool.append(config)

        # Get all key-value pairs
        self.config_all_keys = util.get_all_keys(config, self.value_to_config_key, self.origin_key_to_value)
        self.config_all_values = util.get_all_values(config)

        for ele in self.config_all_values:
            self.type_to_values_map[type(ele)].append(ele)

    def check_alive(self):
        command = "ps -ef | grep seid | grep -v grep"
        result = subprocess.run(f"docker exec {self.image_id} {command}", shell=True, capture_output=True, text=True)
        return bool(result.stdout.strip())

    def copy_to_docker(self, src, dest):
        copy_command = f"docker cp {src} {self.image_id}:{dest}"
        subprocess.run(copy_command, shell=True)

    def restart_node(self):
        self.exec_docker_command('/tmp/stop.sh')
        time.sleep(3)
        self.exec_docker_command('/tmp/start.sh')

    def check_panic(self, config_ini):
        if not self.check_alive():
            cur_time = time.time()
            directory = self.current_result_panic_path + str(cur_time) + "/"
            self.exec_docker_command(f"mkdir -p {directory}")
            with open(f"{directory}panic_error.toml", 'w', encoding='utf-8') as f:
                toml.dump(config_ini, f)
            self.exec_docker_command(f'cp {self.panic_log_path} {directory}/')
            return True
        return False

    def write_config_for_analysis(self, d, new_config):
        if self.check_alive():
            self.logger.info("start successful")
            return True
        else:
            self.logger.info("maybe " + d)
            file_name = f"{d}.toml{str(time.time())}"
            error_dir = self.current_result_path + f"{d}/"
            self.exec_docker_command(f"mkdir -p {error_dir}")
            with open(f"{error_dir}{file_name}", 'w', encoding='utf-8') as f:
                toml.dump(new_config, f)
            self.exec_docker_command(f"cp {self.panic_log_path} {error_dir}")
        return False

    # Other methods remain unchanged...

    def generator_keys(self):
        random_key = random.choice(self.config_all_keys)
        while self.value_to_config_key[random_key] is list:
            random_key = random.choice(self.config_all_keys)
        return random_key

    def generate_value_by_key(self, random_key):
        if self.value_to_config_key[random_key] is int:
            new_value = random.choice([(1 << 31) - 1, -1, 0, 1 - (1 << 31) - 1, -(1 << 31),
                                       random.choice(self.type_to_values_map[self.value_to_config_key[random_key]])])
        elif self.value_to_config_key[random_key] is float:
            new_value = random.choice([random.uniform(0, 1), random.uniform(1, 100000000)])
        else:
            new_value = random.choice(self.type_to_values_map[self.value_to_config_key[random_key]])
            if random.random() < 0.1:
                new_value = random.choice(['www.baidu.com', '/awfawjfo/dawf', '~/awfawf'])
        return new_value

    def fuzz(self):
        with self.lock:
            self.logger.info("it's the {} time of test".format(self.fuzzing_round))
            self.fuzzing_round += 1
            cur_config = random.choice(self.config_pool)
            new_config = copy.deepcopy(cur_config)
            mode = random.choice(self.test_modes)
            random_key = self.generator_keys()

            if mode == "delete":
                new_value = "delete"

            if self.node_type == "f":
                if mode == "change" or (mode == "delete" and check_failure_rule(random_key, "delete")):
                    new_value = self.generate_value_by_key(random_key)
                    cnt = 0
                    while cnt < 10 and (
                            check_failure_rule(random_key, new_value) or check_success_rule(random_key, new_value)):
                        random_key = self.generator_keys()
                        new_value = self.generate_value_by_key(random_key)
                        cnt += 1
                    if cnt == 10:
                        new_value = self.origin_key_to_value[random_key]
                    self.logger.info(new_value)
                    util.set_value_by_path(new_config, random_key, new_value)

                    with open(self.current_config_file, 'w', encoding='utf-8') as file:
                        toml.dump(new_config, file)
                    self.restart_node()
                    time.sleep(10)

                    if self.check_panic(new_config):
                        add_failure_rule(random_key, new_value)
                        self.logger.info(
                            "it's the {} time of test, and it panic fail...........".format(self.fuzzing_round))
                        return

                    if not self.check_alive():
                        add_failure_rule(random_key, new_value)
                        self.logger.info(
                            "it's the {} time of test, and it fail but not panic...........".format(self.fuzzing_round))
                        return

                    for i in range(self.CHECK_TIMES):
                        time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                        res = self.write_config_for_analysis('runtime_error', new_config)
                        if not res:
                            add_failure_rule(random_key, new_value)
                            self.logger.info(
                                "it's the {} time of test, and it fail...........".format(self.fuzzing_round))
                            return
                elif mode == "delete":
                    random_key = random.choice(self.config_all_keys)
                    util.delete_key(new_config, random_key)
                    with open(self.current_config_file, 'w', encoding='utf-8') as file:
                        toml.dump(new_config, file)
                    self.restart_node()
                    time.sleep(10)
                    if self.check_panic(new_config):
                        add_failure_rule(random_key, "delete")
                        self.logger.info(
                            "it's the {} time of test, and it panic fail...........".format(self.fuzzing_round))
                        return

                    if not self.check_alive():
                        add_failure_rule(random_key, "delete")
                        self.logger.info(
                            "it's the {} time of test, and it fail but not panic...........".format(self.fuzzing_round))
                        return

                    for i in range(self.CHECK_TIMES):
                        time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                        res = self.write_config_for_analysis('runtime_error', new_config)
                        if not res:
                            add_failure_rule(random_key, "delete")
                            self.logger.info(
                                "it's the {} time of test, and it fail...........".format(self.fuzzing_round))
                            return
            elif self.node_type == "s":
                if mode == "change" or (mode == "delete" and check_failure_rule(random_key, "delete")):
                    new_value = self.generate_value_by_key(random_key)
                    cnt = 0
                    while cnt < 10 and (
                            check_failure_rule(random_key, new_value) or check_success_rule(random_key, new_value)):
                        if check_failure_rule(random_key, new_value):
                            random_key = self.generator_keys()
                        new_value = self.generate_value_by_key(random_key)
                        cnt += 1
                    if cnt == 10:
                        new_value = self.origin_key_to_value[random_key]
                    self.logger.info(new_value)
                    util.set_value_by_path(new_config, random_key, new_value)

                    with open(self.current_config_file, 'w', encoding='utf-8') as file:
                        toml.dump(new_config, file)
                    self.restart_node()
                    time.sleep(10)

                    if self.check_panic(new_config):
                        add_failure_rule(random_key, new_value)
                        self.logger.info(
                            "it's the {} time of test, and it panic fail...........".format(self.fuzzing_round))
                        return

                    if not self.check_alive():
                        add_failure_rule(random_key, new_value)
                        self.logger.info(
                            "it's the {} time of test, and it fail but not panic...........".format(self.fuzzing_round))
                        return

                    for i in range(self.CHECK_TIMES):
                        time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                        res = self.write_config_for_analysis('runtime_error', new_config)
                        if not res:
                            add_failure_rule(random_key, new_value)
                            self.logger.info(
                                "it's the {} time of test, and it fail...........".format(self.fuzzing_round))
                            return
                elif mode == "delete":
                    random_key = random.choice(self.config_all_keys)
                    util.delete_key(new_config, random_key)
                    with open(self.current_config_file, 'w', encoding='utf-8') as file:
                        toml.dump(new_config, file)
                    self.restart_node()
                    time.sleep(10)
                    if self.check_panic(new_config):
                        add_failure_rule(random_key, "delete")
                        self.logger.info(
                            "it's the {} time of test, and it panic fail...........".format(self.fuzzing_round))
                        return

                    if not self.check_alive():
                        add_failure_rule(random_key, "delete")
                        self.logger.info(
                            "it's the {} time of test, and it fail but not panic...........".format(self.fuzzing_round))
                        return

                    for i in range(self.CHECK_TIMES):
                        time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                        res = self.write_config_for_analysis('runtime_error', new_config)
                        if not res:
                            add_failure_rule(random_key, "delete")
                            self.logger.info(
                                "it's the {} time of test, and it fail...........".format(self.fuzzing_round))
                            return
            else:
                self.logger.info("error node type!!!!!")
                return
            add_success_rule(random_key, new_value)
            self.config_pool.append(new_config)
            self.logger.info("it's the {} time of test, and it success".format(self.fuzzing_round))

class MultinodeFuzzer:
    def __init__(self):

        # root path for single node fuzzer
        self.cur_result_path = RESULT_PATH + "test_result_" + str(time.time()) + "/"

        self.init_resource()


        # 用于同步的锁
        self.lock = threading.Lock()

        self.logger = init_log("multinodeFuzzer", self.cur_result_path)
        self.logger.info("init singleNodeFuzzer.....")
        self.selected_single_nodes = []
        self.selected_single_nodes.append(SingleNodeFuzzer("node0", "f", "1",self.cur_result_path))
        self.selected_single_nodes.append(SingleNodeFuzzer("node1", "f", "2", self.cur_result_path))
        self.selected_single_nodes.append(SingleNodeFuzzer("node2", "f", "3", self.cur_result_path))
        self.selected_single_nodes.append(SingleNodeFuzzer("node3", "f", "4", self.cur_result_path))
        self.executor = ThreadPoolExecutor(max_workers=len(self.selected_single_nodes))
        self.logger.info("init fuzzer of all nodes successfully")

    def init_resource(self):
        os.makedirs(self.cur_result_path)

    def fuzz(self):
        # 将所有节点的fuzz()函数提交给线程池
        futures = {self.executor.submit(obj.fuzz): obj for obj in self.selected_single_nodes}

        # 持续监控fuzz()的执行状态
        while True:
            for future in as_completed(futures):
                obj = futures[future]

                try:
                    # 检查当前fuzz()的返回状态（可选）
                    result = future.result()  # 若需要检查结果，可以捕获它

                    # 再次提交已完成的fuzz对象，继续执行下一轮测试
                    with self.lock:
                        futures[self.executor.submit(obj.fuzz)] = obj
                        self.logger.info(f"Resubmitting fuzz task for {obj.name}")

                except Exception as e:
                    # 捕获任务中的异常，避免线程池中断
                    self.logger.error(f"Error in fuzz execution for {obj.name}: {str(e)}")

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
