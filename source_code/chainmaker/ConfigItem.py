from enum import Enum
import random


class ConfigType(Enum):
    """
    定义一个配置类型的枚举类。
    """
    Consensus = "consensus"
    Network = "network"
    Storage = "storage"
    Transaction = "transaction"
    Other = "other"


class ConfigItem:
    def __init__(self, key, value, config_type=None):
        """
        Initializes a ConfigItem instance.

        :param key: The unique identifier for the configuration item.
        :param value: The value associated with the configuration item.
        :param config_type: The type of the configuration item (ConfigType).
        """
        self.key = key
        self.value = value
        self.config_type = config_type

    def mutate(self):
        """
        根据配置类型应用变异规则。
        """
        if self.config_type == ConfigType.Consensus:
            self.value = self._mutate_consensus()
        elif self.config_type == ConfigType.Network:
            self.value = self._mutate_network()
        elif self.config_type == ConfigType.Transaction:
            self.value = self._mutate_transaction()
        elif self.config_type == ConfigType.Storage:
            self.value = self._mutate_storage()
        elif self.config_type == ConfigType.Other:
            self.value = self._mutate_other()
        else:
            raise ValueError("未知的配置类型，无法变异。")

    def _mutate_consensus(self):
        """
        针对共识相关配置项的特殊变异规则，确保每次都返回新的变异值。
        """
        mutated_value = self.value  # 初始化为当前值，用于确保变异发生

        # 针对具体 key 的变异规则
        if "timeout" in self.key.lower():
            # 超时：返回极端值、小数值或范围外的偏移
            while mutated_value == self.value:
                mutated_value = random.choice([0, 1, 99999, self.value * 2, self.value + random.randint(-200, 200)])

        elif "type" in self.key.lower():
            # 共识类型：切换为合法或非法的协议类型
            valid_types = ["PoW", "PoS", "DPoS", "PBFT", "RAFT"]
            invalid_types = ["UnknownConsensus", "InvalidType123"]
            all_types = valid_types + invalid_types
            while mutated_value == self.value:
                mutated_value = random.choice(all_types)

        elif "snap_count" in self.key.lower():
            # 快照计数：极小值、极大值或倍数变化
            while mutated_value == self.value:
                mutated_value = random.choice([1, 2, self.value * 2, max(1, self.value // 2)])

        elif "ticker" in self.key.lower():
            # 定时器：设置极快或极慢值，避免原值
            while mutated_value == self.value:
                mutated_value = random.choice([0.01, 10, 100, self.value * 2, self.value + random.uniform(-1, 5)])

        elif isinstance(self.value, bool):
            # 布尔值：始终反转
            mutated_value = not self.value

        elif isinstance(self.value, int):
            # 整数值：设置极端值或范围外偏移
            while mutated_value == self.value:
                mutated_value = random.choice([0, 1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, str):
            # 字符串值：插入特殊字符或非法值
            while mutated_value == self.value:
                mutated_value = random.choice([self.value[::-1], f"{self.value}_mutated", "InvalidString!@#"])

        # 如果没有特定规则适用，抛出异常
        if mutated_value == self.value:
            raise RuntimeError(f"未能成功变异值: {self.key} ({self.value})")

        return mutated_value

    def _mutate_network(self):
        """
        针对网络相关配置项的特殊变异规则，确保每次都返回新的变异值。
        """
        mutated_value = self.value  # 初始化为当前值，用于确保变异发生

        # 针对具体 key 的变异规则
        if "port" in self.key.lower():
            # 端口号变异：非法端口、极小值、极大值、随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice(
                    [0, 65536, self.value + random.randint(-100, 100), random.randint(1, 65535)])

        elif "addr" in self.key.lower():
            # 地址变异：无效 IP、随机端口、不合法格式
            invalid_addresses = [
                "0.0.0.0:0",
                "127.0.0.1:-1",
                "invalid_address",
                "256.256.256.256:80",
                "[::1]:8080"  # IPv6 地址
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(
                    invalid_addresses + [f"192.168.1.{random.randint(0, 255)}:{random.randint(1024, 65535)}"])

        elif "seeds" in self.key.lower():
            # 种子节点变异：添加无效节点、重复节点或空列表
            invalid_seeds = [
                "/ip4/0.0.0.0/tcp/0/p2p/invalid",
                "/ip4/127.0.0.1/tcp/-1/p2p/QmInvalid",
            ]
            while mutated_value == self.value:
                mutated_value = self.value + random.choices(invalid_seeds, k=random.randint(1, 3))

        elif "tls" in self.key.lower():
            # TLS 配置：无效文件路径、错误证书格式
            invalid_tls_files = [
                "/invalid/path/tls.crt",
                "/invalid/path/tls.key",
                None,
                "",
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_tls_files)

        elif "key" in self.key.lower() or "cert" in self.key.lower():
            # 密钥或证书：无效路径、随机字符串
            while mutated_value == self.value:
                mutated_value = random.choice(["/invalid/path", "invalid_key", ""])

        elif isinstance(self.value, bool):
            # 布尔值：始终反转
            mutated_value = not self.value

        elif isinstance(self.value, str):
            # 字符串值：插入特殊字符或生成无效值
            while mutated_value == self.value:
                mutated_value = random.choice([self.value[::-1], f"{self.value}_mutated", "invalid_string"])

        elif isinstance(self.value, int):
            # 整数值：设置为极端值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([0, 1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, list):
            # 列表值：添加无效项或清空列表
            while mutated_value == self.value:
                mutated_value = self.value + random.choices(["/invalid/entry", "/duplicate/entry"], k=2)

        # 如果没有特定规则适用，抛出异常
        if mutated_value == self.value:
            raise RuntimeError(f"未能成功变异值: {self.key} ({self.value})")

        return mutated_value

    def _mutate_transaction(self):
        """
        针对交易相关配置项的特殊变异规则，确保每次都返回新的变异值。
        """
        mutated_value = self.value  # 初始化为当前值，用于确保变异发生

        # 针对具体 key 的变异规则
        if "txpoolsize" in self.key.lower():
            # 交易池大小变异：设置为极端值、随机倍数变化或非法值
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 无交易池
                    -1,  # 非法值
                    self.value * 2,  # 倍数变化
                    self.value + random.randint(-5000, 5000),  # 偏移
                    1000000  # 超大交易池大小
                ])

        elif "txpooltype" in self.key.lower():
            # 交易池类型变异：切换类型或插入非法值
            valid_types = ["normal", "single", "priority"]
            invalid_types = ["undefined", "invalid_type"]
            while mutated_value == self.value:
                mutated_value = random.choice(valid_types + invalid_types)

        elif "batch" in self.key.lower():
            # 批量处理相关配置变异
            if "create_timeout" in self.key.lower():
                # 批量创建超时时间：极短或极长
                while mutated_value == self.value:
                    mutated_value = random.choice([0, 1, self.value * 10, self.value - random.randint(1, 50)])
            elif "max_size" in self.key.lower():
                # 批量最大交易数：非法值或随机偏移
                while mutated_value == self.value:
                    mutated_value = random.choice([
                        0,  # 禁用批量
                        -1,  # 非法负值
                        self.value * 2,
                        self.value + random.randint(-50, 100)
                    ])

        elif "queue" in self.key.lower():
            # 队列相关配置变异
            if "common_queue_num" in self.key.lower():
                # 设置队列数量：非法值或倍数变化
                while mutated_value == self.value:
                    mutated_value = random.choice([0, -1, self.value * 2, self.value + random.randint(-10, 20)])
            elif "is_dump_txs_in_queue" in self.key.lower():
                # 布尔值：始终反转
                mutated_value = not self.value

        elif isinstance(self.value, bool):
            # 布尔值：直接反转
            mutated_value = not self.value

        elif isinstance(self.value, int):
            # 整数值：设置为极端值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([0, 1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, str):
            # 字符串值：插入特殊字符或非法值
            while mutated_value == self.value:
                mutated_value = random.choice([self.value[::-1], f"{self.value}_mutated", "invalid_string"])

        # 如果没有特定规则适用，抛出异常
        if mutated_value == self.value:
            raise RuntimeError(f"未能成功变异值: {self.key} ({self.value})")

        return mutated_value

    def _mutate_storage(self):
        """
        针对存储相关配置项的特殊变异规则，确保每次都返回新的变异值。
        """
        mutated_value = self.value  # 初始化为当前值，用于确保变异发生

        # 针对具体 key 的变异规则
        if "store_path" in self.key.lower():
            # 存储路径变异：非法路径、无权限路径或不存在路径
            invalid_paths = [
                "/invalid/path",
                "/tmp/readonly_path",
                "",
                None
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_paths + [f"/tmp/test_path_{random.randint(1, 1000)}"])

        elif "write_buffer_size" in self.key.lower():
            # 写缓冲大小变异：设置极端值、负值或超大值
            while mutated_value == self.value:
                mutated_value = random.choice([0, -1, self.value * 2, 10 ** 6])

        elif "provider" in self.key.lower():
            # 存储后端提供者：切换存储类型或插入非法值
            valid_providers = ["leveldb", "rocksdb", "inmemory"]
            invalid_providers = ["undefined", "invalid_provider"]
            while mutated_value == self.value:
                mutated_value = random.choice(valid_providers + invalid_providers)

        elif "cache" in self.key.lower():
            # 缓存相关配置：修改缓存大小、清理时间等
            while mutated_value == self.value:
                mutated_value = random.choice([0, -1, self.value * 2, self.value + random.randint(-100, 100)])

        elif "disable" in self.key.lower():
            # 布尔值：始终反转（禁用状态）
            mutated_value = not self.value

        elif "interval" in self.key.lower():
            # 时间间隔变异：设置极短或极长时间
            while mutated_value == self.value:
                mutated_value = random.choice([0, 1, self.value * 10, self.value + random.randint(-50, 50)])

        elif isinstance(self.value, bool):
            # 布尔值：直接反转
            mutated_value = not self.value

        elif isinstance(self.value, int):
            # 整数值：设置为极端值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([0, -1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, str):
            # 字符串值：插入特殊字符或非法值
            while mutated_value == self.value:
                mutated_value = random.choice([self.value[::-1], f"{self.value}_mutated", "invalid_string"])

        elif isinstance(self.value, list):
            # 列表值：添加无效项或清空列表
            while mutated_value == self.value:
                mutated_value = self.value + random.choices(["/invalid/entry", "/duplicate/entry"], k=2)

        # 如果没有特定规则适用，抛出异常
        if mutated_value == self.value:
            raise RuntimeError(f"未能成功变异值: {self.key} ({self.value})")

        return mutated_value

    def _mutate_other(self):
        """
        针对其他配置项的通用变异规则，确保每次都返回新的变异值。
        """
        mutated_value = self.value  # 初始化为当前值，用于确保变异发生

        if isinstance(self.value, int):
            # 整数值：设置极端值、非法值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 最小值
                    -1,  # 非法负值
                    99999,  # 极端大值
                    self.value + random.randint(-500, 500),  # 随机偏移
                ])

        elif isinstance(self.value, float):
            # 浮点数：设置极端值、非法值或随机比例变化
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0.0,  # 最小值
                    -1.0,  # 非法负值
                    self.value * random.uniform(0.1, 10.0),  # 随机比例变化
                    round(random.uniform(-1000.0, 1000.0), 3),  # 随机生成
                ])

        elif isinstance(self.value, str):
            # 字符串：反转、插入特殊字符、替换为非法值
            while mutated_value == self.value:
                mutated_value = random.choice([
                    self.value[::-1],  # 反转字符串
                    f"{self.value}_mutated",  # 拼接后缀
                    "invalid_string!@#",  # 非法字符串
                    "",  # 空字符串
                    None,  # 空值
                ])

        elif isinstance(self.value, bool):
            # 布尔值：直接反转
            mutated_value = not self.value

        elif isinstance(self.value, list):
            # 列表值：插入无效项、重复项或清空
            while mutated_value == self.value:
                mutated_value = random.choice([
                    self.value + ["invalid_entry"],  # 添加无效项
                    self.value + random.choices(self.value, k=2),  # 添加重复项
                    [],  # 清空列表
                ])

        elif isinstance(self.value, dict):
            # 字典值：插入无效键值对或清空
            while mutated_value == self.value:
                mutated_value = random.choice([
                    {**self.value, "invalid_key": "invalid_value"},  # 添加无效键值对
                    {},  # 清空字典
                ])

        else:
            # 其他类型：尝试转换为字符串或整数以触发异常场景
            while mutated_value == self.value:
                mutated_value = random.choice([
                    str(self.value),  # 转换为字符串
                    0,  # 转换为整数
                    None,  # 转换为空值
                ])

        # 如果没有特定规则适用，抛出异常
        if mutated_value == self.value:
            raise RuntimeError(f"未能成功变异值: {self.key} ({self.value})")

        return mutated_value

    def __str__(self):
        """
        Returns a string representation of the ConfigItem.

        :return: A string in the format 'key: value (config_type)'.
        """
        if self.config_type:
            return f"{self.key}: {self.value} ({self.config_type.value})"
        return f"{self.key}: {self.value}"

    def to_dict(self):
        """
        Converts the ConfigItem to a dictionary.

        :return: A dictionary with keys 'key', 'value', and 'config_type'.
        """
        return {
            "key": self.key,
            "value": self.value,
            "config_type": self.config_type.value if self.config_type else None,
        }
