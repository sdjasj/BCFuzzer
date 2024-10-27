import collections
import configparser
import copy
import logging
import os
import random
import subprocess
import time

import util

PROJECT_PATH = "/root/nodes/"
RESULT_PATH = "/root/test_evaluation3/compare_test_result/"


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


class SingleNodeFuzzer:
    def __init__(self, name, root_result_path=RESULT_PATH + "test_result_" + str(time.time()) + "/"):
        # name: seid-3
        self.name = name
        os.makedirs(root_result_path)
        # 记录测试失败的目录
        self.current_result_path = root_result_path + name + "/"
        self.current_result_panic_path = self.current_result_path + "panic_error/"
        self.current_result_start_error_path = self.current_result_path + "start_error/"
        self.current_result_runtime_error_path = self.current_result_path + "runtime_error/"
        self.init_resource()

        # 初始化日志
        self.logger = init_log(self.name, self.current_result_path)

        # 指定检查次数
        self.CHECK_TIMES = 5
        self.RUN_TIME_FOR_CRASH = 10

        self.origin_config_file = PROJECT_PATH + self.name + "/" + "origin_config.ini"
        self.current_config_file = PROJECT_PATH + self.name + "/" + "config.ini"
        self.panic_log_path = PROJECT_PATH + self.name + "/" + "{}.log".format(self.name)
        self.config_pool = []
        self.config_all_keys = []
        self.config_all_values = []
        self.type_to_values_map = collections.defaultdict(list)
        self.test_modes = ["change", "delete"]
        self.parameter_num_change_per_time = 2
        self.value_to_config_key = dict()
        self.unique_panic_files = set()
        self.origin_key_to_value = dict()
        self.failure_set = collections.defaultdict(set)
        os.makedirs("{}".format(self.name), exist_ok=True)
        with open("./{}/start.sh".format(self.name), 'w', encoding='utf-8') as f:
            f.write("geth --config /root/nodes/{}/config.ini > {} 2>&1 &".format(self.name, self.panic_log_path))
        os.system('chmod 777 ./{}/start.sh'.format(self.name))
        with open("./{}/stop.sh".format(self.name), 'w', encoding='utf-8') as f:
            f.write("""pid=`ps -ef | grep "{}" | grep -v grep |  awk  '{{print $2}}'`
        if [ ! -z "${{pid}}" ];
        then
            kill -9 $pid
            echo "aptos is stopping..."
        fi""".format(self.name))
        os.system('chmod 777 ./{}/stop.sh'.format(self.name))

        self.load_config()
        self.logger.info("init singleNodeFuzzer {}.....".format(name))

    def init_resource(self):
        os.makedirs(self.current_result_path)
        os.makedirs(self.current_result_panic_path)
        os.makedirs(self.current_result_start_error_path)
        os.makedirs(self.current_result_runtime_error_path)

    def load_config(self):
        # 复制原始配置文件以便从错误配置中恢复
        os.system("cp {} {}".format(self.current_config_file, self.origin_config_file))

        # 读取并解析 INI 文件
        config = configparser.RawConfigParser()
        config.optionxform = str  # 保留原始的大小写
        config.read(self.current_config_file)

        # 将解析结果加入到 config_pool
        self.config_pool.append(config)

        # 获取所有键值对
        self.config_all_keys = util.get_all_keys(config, self.value_to_config_key, self.origin_key_to_value)
        self.config_all_values = util.get_all_values(config)

        # 根据值的类型分类存储
        for ele in self.config_all_values:
            self.type_to_values_map[type(ele)].append(ele)

    def check_alive(self):
        command = "ps -ef | grep {} | grep -v grep".format(self.name)
        # 使用 subprocess.run 执行命令并捕获输出
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.stdout.strip():
            return True
        return False

    def restart_node(self):
        os.system('./{}/stop.sh'.format(self.name))
        time.sleep(3)
        os.system('./{}/start.sh'.format(self.name))

    def check_panic(self, config_ini):
        if not self.check_alive():
            cur_time = time.time()
            directory = self.current_result_panic_path + str(cur_time) + "/"
            os.mkdir(directory)
            with open(directory + "panic_error" + ".ini", 'w', encoding='utf-8') as f:
                config_ini.write(f)
            os.system('cp {} {}'.format(self.panic_log_path, directory + '/'))
            return True
        return False


    def write_config_for_analysis(self, d, new_config):
        command = "ps -ef | grep {} | grep -v grep".format(self.name)
        # 使用 subprocess.run 执行命令并捕获输出
        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        if result.stdout.strip():
            self.logger.info("start successful")
            return True
        else:
            self.logger.info("maybe " + d)
            file_name = d + ".ini" + str(time.time())
            error_dir = self.current_result_path + d + '/{}/'.format(str(time.time()))
            os.makedirs(error_dir)
            with open(error_dir + file_name, 'w', encoding='utf-8') as f:
                new_config.write(f)
            os.system("cp {} {}".format(self.panic_log_path, error_dir))
        return False

    def generator_keys(self):
        random_key = random.choice(self.config_all_keys)
        while self.value_to_config_key[random_key] is list:
            random_key = random.choice(self.config_all_keys)
        return random_key

    def generate_value_by_key(self, random_key):
        if self.value_to_config_key[random_key] is int:
            new_value = random.choice([(1 << 31) - 1, -1, 0, 1 - (1 << 31) - 1, -(1 << 31),
                                       random.choice(
                                           self.type_to_values_map[self.value_to_config_key[random_key]])])
        elif self.value_to_config_key[random_key] is float:
            new_value = random.choice([random.uniform(0, 1), random.uniform(1, 100000000)])
        else:
            # new_value = random.choice(self.type_to_values_map[self.value_to_config_key[random_key]])
            # if random.random() < 0.1:
            #     new_value = random.choice(['www.baidu.com', '/awfawjfo/dawf', '~/awfawf'])
            new_value = random.choice(['www.baidu.com', '/awfawjfo/dawf', '~/awfawf'])
        return new_value


    def fuzz(self, T):
        self.logger.info("it's the {} time of test".format(T))
        cur_config = random.choice(self.config_pool)
        new_config = copy.deepcopy(cur_config)
        mode = random.choice(self.test_modes)
        random_key = self.generator_keys()
        if mode == "change" or mode == "delete" and "delete" in self.failure_set[random_key]:
            new_value = self.generate_value_by_key(random_key)
            cnt = 0
            while cnt < 10 and new_value in self.failure_set[random_key]:
                new_value = self.generate_value_by_key(random_key)
                cnt += 1
            if cnt == 10:
                new_value = self.origin_key_to_value[random_key]
            self.logger.info(new_value)
            util.set_value_by_path(new_config, random_key, new_value)

            with open(self.current_config_file, 'w', encoding='utf-8') as file:
                new_config.write(file)
            self.restart_node()
            time.sleep(5)

            if self.check_panic(new_config):
                self.failure_set[random_key].add(new_value)
                self.logger.info("it's the {} time of test, and it panic fail...........".format(T))
                return

            if not self.check_alive():
                self.failure_set[random_key].add(new_value)
                self.logger.info("it's the {} time of test, and it fail but not panic...........".format(T))
                return

            for i in range(self.CHECK_TIMES):
                time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                res = self.write_config_for_analysis('runtime_error', new_config)
                if not res:
                    self.failure_set[random_key].add(new_value)
                    self.logger.info("it's the {} time of test, and it fail...........".format(T))
                    return
        elif mode == "delete":
            random_key = random.choice(self.config_all_keys)
            util.delete_key(new_config, random_key)
            with open(self.current_config_file, 'w', encoding='utf-8') as file:
                new_config.write(file)
            self.restart_node()
            time.sleep(5)
            if self.check_panic(new_config):
                self.failure_set[random_key].add("delete")
                self.logger.info("it's the {} time of test, and it panic fail...........".format(T))
                return

            if not self.check_alive():
                self.failure_set[random_key].add("delete")
                self.logger.info("it's the {} time of test, and it fail but not panic...........".format(T))
                return

            for i in range(self.CHECK_TIMES):
                time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                res = self.write_config_for_analysis('runtime_error', new_config)
                if not res:
                    self.failure_set[random_key].add("delete")
                    self.logger.info("it's the {} time of test, and it fail...........".format(T))
                    return
        self.config_pool.append(new_config)
        self.logger.info("it's the {} time of test, and it success".format(T))


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
