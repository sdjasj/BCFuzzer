import collections
import copy
import logging
import os
import random
import subprocess
import time
from multiprocessing import Pool

import yaml

import util

PROJECT_PATH = "/root/chainmaker-go/build/release/"
RESULT_PATH = "/root/test/co_test_result/"


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
    def __init__(self, name, root_result_path):
        # name: wx-org1
        self.name = name

        # 记录测试失败的目录
        self.current_result_path = root_result_path + name + "/"
        self.current_result_panic_path = self.current_result_path + "panic_error/"
        self.current_result_start_error_path = self.current_result_path + "start_error/"
        self.current_result_runtime_error_path = self.current_result_path + "runtime_error/"
        self.current_successful_config_path = self.current_result_path + "successful_config/"
        self.init_resource()
        self.unchanged_keys = [
            "storage.state_cache_config.max_entry_size",
            "storage.write_block_type"
        ]

        # 初始化日志
        self.logger = init_log(self.name, self.current_result_path)

        # 指定检查次数
        self.CHECK_TIMES = 5
        self.RUN_TIME_FOR_CRASH = 50

        self.node_path = PROJECT_PATH + "chainmaker-v2.3.4-{}.chainmaker.org/".format(self.name)
        self.config_path = self.node_path + "config/{}.chainmaker.org/".format(self.name)
        self.bin_path = self.node_path + "bin/"
        self.origin_config_file = self.config_path + "origin_chainmaker.yml"
        self.current_config_file = self.config_path + "chainmaker.yml"
        self.panic_log_path = self.bin_path + "panic.log"
        self.origin_config = None
        self.current_config = None
        self.config_pool = []
        self.config_all_keys = []
        self.config_all_values = []
        self.type_to_values_map = collections.defaultdict(list)
        self.test_modes = ["change", "delete"]
        self.parameter_num_change_per_time = 1
        self.value_to_config_key = dict()
        self.unique_panic_files = set()

        self.load_config()
        self.logger.info("init singleNodeFuzzer {}.....".format(name))
        self.tx_pool_related_configs = [
            "txpool.batch_create_timeout",
            "txpool.batch_max_size",
            "txpool.common_queue_num",
            "txpool.is_dump_txs_in_queue",
            "txpool.max_config_txpool_size",
            "txpool.max_txpool_size",
            "txpool.pool_type",
        ]

    def init_resource(self):
        os.makedirs(self.current_result_path)
        os.makedirs(self.current_result_panic_path)
        os.makedirs(self.current_result_start_error_path)
        os.makedirs(self.current_result_runtime_error_path)
        os.makedirs(self.current_successful_config_path)

    def load_config(self):
        # copy the original config file to recover from bad config
        os.system("cp {} {}".format(self.current_config_file, self.origin_config_file))
        # 读取并解析 YAML 文件
        with open(self.current_config_file, 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file)
        self.origin_config = config
        self.current_config = config
        self.config_pool.append((0, config))
        self.config_all_keys = util.get_all_keys(config, self.value_to_config_key)
        self.config_all_values = util.get_all_values(config)
        for ele in self.config_all_values:
            self.type_to_values_map[type(ele)].append(ele)

    def restart_node(self):
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

    def check_alive(self):
        command = "ps -ef | grep chainmaker | grep -v grep | grep {}.chainmaker.org".format(self.name)
        # 使用 subprocess.run 执行命令并捕获输出
        result = subprocess.run(command, shell=True, capture_output=True, text=True)

        if result.stdout.strip():
            return True
        return False


    def write_config_for_analysis(self, d, new_config):

        if self.check_alive():
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

    def choice_keys_from_all_config(self, only_int=True):
        if only_int:
            random_key = random.choice(self.config_all_keys)
            while (self.value_to_config_key[random_key] is not int
                   or random_key in self.unchanged_keys):
                random_key = random.choice(self.config_all_keys)
        else:
            random_key = random.choice(self.config_all_keys)
            while (self.value_to_config_key[random_key] is list
                   or random_key in self.unchanged_keys):
                random_key = random.choice(self.config_all_keys)
        return random_key

    def choice_keys_from_tx_config(self):
        random_key = random.choice(self.tx_pool_related_configs)
        return random_key

    def copy_panic_files(self):
        directory = self.current_result_panic_path + str(str(time.time())) + "/"
        os.mkdir(directory)
        os.system('cp {} {}'.format(self.current_config_file, directory + '/'))
        os.system('cp {} {}'.format(self.panic_log_path, directory + '/'))

    def get_new_value_by_key(self, random_key):
        if random_key == "txpool.pool_type":
            return random.choice(["single", "normal", "batch"])
        if self.value_to_config_key[random_key] == int:
            # new_value = random.choice([(1 << 31) - 1, -2, 0, 1 -(1 << 31) - 1, -(1 << 31),
            #                            random.choice(
            #                                self.type_to_values_map[self.value_to_config_key[random_key]])])
            new_value = random.choice([(1 << 31) - 1, -1, 0, 1 -(1 << 31) - 1, -(1 << 31)])
        elif self.value_to_config_key[random_key] is float:
            new_value = random.choice([random.uniform(0, 1), random.uniform(1, 100000000)])
            if random.random() > 0.5:
                new_value = -new_value
        else:
            new_value = random.choice(self.type_to_values_map[self.value_to_config_key[random_key]])
            if random.random() < 0.1:
                new_value = random.choice(['www.baidu.com', '/awfawjfo/dawf', '~/awfawf'])
        return new_value



    def change_random_config(self):
        self.current_config = random.choice(self.config_pool)
        yaml_str = yaml.dump(self.current_config, default_flow_style=False)
        with open(self.current_config_file, 'w', encoding='utf-8') as file:
            file.write(yaml_str)

    def fuzz(self, T):
        self.logger.info("it's the {} time of test".format(T))
        cur_config = random.choices(self.config_pool, weights=[config[0] + 1 for config in self.config_pool], k=1)[0][1]
        new_config = copy.deepcopy(cur_config)
        mode = random.choice(self.test_modes)
        if mode == "change":
            used_keys = set()
            for i in range(self.parameter_num_change_per_time):
                random_key = self.choice_keys_from_tx_config()
                if random_key in used_keys:
                    continue
                used_keys.add(random_key)
                self.logger.info(random_key)

                new_value = self.get_new_value_by_key(random_key)

                self.logger.info(new_value)
                util.set_value_by_path(new_config, random_key, new_value)
            yaml_str = yaml.dump(new_config, default_flow_style=False)
            with open(self.current_config_file, 'w', encoding='utf-8') as file:
                file.write(yaml_str)
            self.restart_node()
            time.sleep(10)
            # check panic
            if self.check_panic(yaml_str):
                self.logger.info("it's the {} time of test, and it panic fail...........".format(T))

            if not self.check_alive():
                self.logger.info("it's the {} time of test, and it fail but not panic...........".format(T))
                return False
            # if check_line_count('panic.log'):
            #     with open('panic_error/' + "panic_error" + ".yml" + str(time.time()), 'w', encoding='utf-8') as f:
            #         f.write(yaml_str)
            #     logging.info("it's the {} time of test, and it panic fail...........".format(T))
            # res = self.write_config_for_analysis('start_error', new_config)
            # if not res:
            #     self.logger.info("it's the {} time of test, and it fail...........".format(T))
            #     return

            for i in range(self.CHECK_TIMES):
                time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                res = self.write_config_for_analysis('runtime_error', new_config)
                if not res:
                    self.logger.info("it's the {} time of test, and it fail...........".format(T))
                    return False
        elif mode == "delete":
            random_key = self.choice_keys_from_tx_config()
            util.delete_key(new_config, random_key)
            yaml_str = yaml.dump(new_config, default_flow_style=False)
            with open(self.current_config_file, 'w', encoding='utf-8') as file:
                file.write(yaml_str)
            self.restart_node()
            time.sleep(10)

            # check panic
            if self.check_panic(yaml_str):
                self.logger.info("it's the {} time of test, and it panic fail...........".format(T))
            if not self.check_alive():
                self.logger.info("it's the {} time of test, and it fail but not panic...........".format(T))
                return False

            for i in range(self.CHECK_TIMES):
                time.sleep(self.RUN_TIME_FOR_CRASH // self.CHECK_TIMES)
                res = self.write_config_for_analysis('runtime_error', new_config)
                if not res:
                    self.logger.info("it's the {} time of test, and it fail...........".format(T))
                    return False

        # now single node start successfully
        self.current_config = new_config
        diff_cnt = util.compare_dicts(self.origin_config, new_config)
        self.config_pool.append((diff_cnt, new_config))
        self.logger.info("it's the {} time of test, and it success".format(T))

        # save successfully start config
        target_dir = self.current_successful_config_path + "/successful_chainmaker_{}.yml".format(time.time())
        os.system("cp {} {}".format(self.current_config_file, target_dir))

        return True


class MultinodeFuzzer:
    def __init__(self):

        self.try_reboot_times = 5
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
        self.selected_single_node_num = self.single_node_num // 3
        self.selected_single_node_names = random.sample(self.single_node_names, self.selected_single_node_num)
        self.all_singe_nodes = []
        self.selected_single_nodes = []
        for name in self.single_node_names:
            node = SingleNodeFuzzer(name=name, root_result_path=self.cur_result_path)
            self.all_singe_nodes.append(node)
            if name in self.selected_single_node_names:
                self.selected_single_nodes.append(node)
            self.logger.info("init fuzzer of node {} successfully".format(name))
        self.all_singe_nodes_config = []
        for node in self.all_singe_nodes:
            self.all_singe_nodes_config.append(node.current_config)
        self.logger.info("init fuzzer of all nodes successfully")



    def check_single_node_alive(self, name):
        command = "ps -ef | grep chainmaker | grep -v grep | grep {}.chainmaker.org".format(name)
        # 使用 subprocess.run 执行命令并捕获输出
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

    def check_all_nodes_alive(self):
        for node in self.all_singe_nodes:
            if not node.check_alive():
                self.logger.info("node {} panic!!!!!!!!!!!!!".format(node.name))
                return node
        return None

    def init_resource(self):
        os.makedirs(self.cur_result_path)

    def fuzz_node(self, args):
        node, i = args
        node.fuzz(i)

    def fuzz(self):
        for i in range(100000):
            selected_node = random.choice(self.all_singe_nodes)
            selected_node_name = selected_node.name
            try_times = 1
            while True:
                self.logger.info("fuzz {} the {} time".format(selected_node_name, try_times))
                res = selected_node.fuzz(i)
                if res:
                    self.logger.info("the {} time of fuzzing {} successfully !".format(try_times, selected_node_name))
                    break
                try_times += 1
            node = self.check_all_nodes_alive()
            is_node_alive = False
            if node is not None:
                node.copy_panic_files()
                self.logger.info("node {} is dead, try to reboot".format(node.name))
                for j in range(self.try_reboot_times):
                    self.logger.info("node {} try to reboot for the {}'s time....".format(node.name, j + 1))
                    node.restart_node()
                    time.sleep(20)
                    if node.check_alive():
                        self.logger.info("node {} is alive!!!!!! just continue to test.......".format(node.name))
                        is_node_alive = True
                        break
                if not is_node_alive:
                    self.logger.info("maybe something unexpected happen, try to use previous config file to recover "
                                     "node {}".format(node.name))
                    node.change_random_config()
                    node.restart_node()
                    time.sleep(10)
                    if node.check_alive():
                        self.logger.info("node restart ok, continue to test......")
                    else:
                        self.logger.info("recover fail, just exit.....")
                        return
            tot_diff_num = 0
            for node1 in self.all_singe_nodes:
                for node2 in self.all_singe_nodes:
                    tot_diff_num += util.compare_dicts(node1.current_config, node2.current_config)
            self.logger.info("the diff num of {}'s time fuzzing time is {}".format(i, tot_diff_num))
            # random.shuffle(self.selected_single_nodes)
            #
            # # 创建一个包含所有节点的 (node, i) 参数对的列表
            # tasks = [(node, i) for node in self.selected_single_nodes]
            # self.logger.info("fuzz all the selected nodes.......")
            # # 使用多进程池来并行处理 fuzz 操作
            # with Pool() as pool:
            #     pool.map(self.fuzz_node, tasks)  # 等待所有进程完成
            # time.sleep(20)
            # self.logger.info("finish multinode fuzz for {} times".format(i + 1))
            # if not self.check_normal_nodes_alive():
            #     return
