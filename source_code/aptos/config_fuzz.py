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


class SingleNodeFuzzer:
    def __init__(self, name, node_type, root_result_path=RESULT_PATH + "test_result_" + str(time.time()) + "/"):
        # name: 0/1/2/3
        self.name = name

        # 记录测试失败的目录
        self.current_result_path = root_result_path + name + "/"
        self.current_result_panic_path = self.current_result_path + "panic_error/"
        self.current_result_start_error_path = self.current_result_path + "start_error/"
        self.current_result_runtime_error_path = self.current_result_path + "runtime_error/"
        self.init_resource()
        self.lock = threading.Lock()

        # 初始化日志
        self.logger = init_log(self.name, self.current_result_path)
        self.node_type = node_type
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
            f.write("/root/aptos-core/target/debug/aptos-node -f {} > {} 2>&1 &".format(self.current_config_file, self.panic_log_path))
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

        self.load_config()
        self.logger.info("init singleNodeFuzzer {}.....".format(name))

    def init_resource(self):
        os.makedirs(self.current_result_path)
        os.makedirs(self.current_result_panic_path)
        os.makedirs(self.current_result_start_error_path)
        os.makedirs(self.current_result_runtime_error_path)

    def load_config(self):
        # copy the original config file to recover from bad config
        os.system("cp {} {}".format(self.current_config_file, self.origin_config_file))
        # 读取并解析 YAML 文件
        with open(self.current_config_file, 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file)
        self.config_pool.append(config)
        self.config_all_keys = util.get_all_keys(config, self.value_to_config_key, self.origin_key_to_value)
        self.config_all_values = util.get_all_values(config)
        for ele in self.config_all_values:
            self.type_to_values_map[type(ele)].append(ele)

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
        return new_value

    def generator_keys(self):
        random_key = random.choice(self.config_all_keys)
        while self.value_to_config_key[random_key] is list:
            random_key = random.choice(self.config_all_keys)
        return random_key

    def fuzz(self):
        with self.lock:
            self.fuzz_round += 1
            self.logger.info("it's the {} time of test".format(self.fuzz_round))
            cur_config = random.choice(self.config_pool)
            new_config = copy.deepcopy(cur_config)
            mode = random.choice(self.test_modes)
            random_key = self.generator_keys()
            if mode == "delete":
                new_value = "delete"
            if self.node_type == "f":
                if mode == "change" or mode == "delete" and check_failure_rule(random_key, "delete"):
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
            elif self.node_type == "s":
                if mode == "change" or mode == "delete" and check_failure_rule(random_key, "delete"):
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
            else:
                self.logger.info("error node type!!!!!")
                return
            add_success_rule(random_key, new_value)
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
        for i in range(1):
            self.selected_single_nodes.append(SingleNodeFuzzer("{}".format(i), "f", self.cur_result_path))
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
