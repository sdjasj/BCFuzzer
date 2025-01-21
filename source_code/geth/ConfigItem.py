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
        if "gas" in self.key.lower():
            # 与 Gas 相关的配置：设置极端值、非法值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用 Gas
                    -1,  # 非法负值
                    self.value * 2,  # 倍数变化
                    self.value + random.randint(-100000, 100000),  # 随机偏移
                    10 ** 18  # 超大值
                ])

        elif "timeout" in self.key.lower():
            # 超时配置：极短、极长或非法值
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用超时
                    -1,  # 非法负值
                    self.value * 10,  # 倍数变化
                    self.value + random.randint(-1000, 1000)  # 随机偏移
                ])

        elif "enable" in self.key.lower():
            # 布尔值：直接反转
            mutated_value = not self.value

        elif "discovery" in self.key.lower():
            # 与发现相关的配置：布尔值直接反转
            mutated_value = not self.value

        elif isinstance(self.value, int):
            # 整数值：设置为极端值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 极小值
                    -1,  # 非法负值
                    99999,  # 极端大值
                    self.value + random.randint(-500, 500)  # 随机偏移
                ])

        elif isinstance(self.value, str):
            # 字符串值：插入特殊字符或非法值
            while mutated_value == self.value:
                mutated_value = random.choice([
                    self.value[::-1],  # 反转字符串
                    f"{self.value}_mutated",  # 拼接后缀
                    "invalid_string",  # 非法字符串
                    "",  # 空字符串
                    None  # 空值
                ])

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
        if "listen_ip" in self.key.lower() or "bind_ip" in self.key.lower():
            # 监听或绑定 IP 变异：非法地址、随机生成地址或特殊值
            invalid_ips = [
                "256.256.256.256",  # 非法 IPv4
                "0.0.0.0",  # 通配地址
                "127.0.0.1",  # 本地回环
                "invalid_ip",  # 非法字符串
                "[::1]"  # IPv6 地址
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_ips + [
                    f"192.168.{random.randint(0, 255)}.{random.randint(0, 255)}"  # 随机合法地址
                ])

        elif "listen_port" in self.key.lower() or "bind_port" in self.key.lower():
            # 监听或绑定端口变异：非法端口、随机端口或超大值
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 非法端口
                    65536,  # 超出范围端口
                    self.value + random.randint(-100, 100),  # 随机偏移
                    random.randint(1, 65535)  # 有效随机端口
                ])

        elif "peers" in self.key.lower():
            # 节点列表变异：插入无效节点或空列表
            invalid_peers = [
                "0.0.0.0:0",
                "256.256.256.256:8080",
                "127.0.0.1:-1",
                "localhost:invalid"
            ]
            while mutated_value == self.value:
                mutated_value = random.choice([
                    self.value + [random.choice(invalid_peers)],  # 添加无效节点
                    [],  # 空列表
                    self.value[:random.randint(0, len(self.value))]  # 截断部分节点
                ])

        elif "ssl" in self.key.lower() or "tls" in self.key.lower():
            # SSL 或 TLS 相关配置：布尔值直接反转
            mutated_value = not self.value

        elif "rate_limit" in self.key.lower() or "max_connections" in self.key.lower():
            # 限制配置：设置为极端值、非法值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用限制
                    -1,  # 非法负值
                    self.value * 2,  # 倍数变化
                    self.value + random.randint(-50, 50)  # 随机偏移
                ])

        elif isinstance(self.value, int):
            # 整数值：设置为极端值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([0, -1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, str):
            # 字符串值：插入特殊字符或非法值
            while mutated_value == self.value:
                mutated_value = random.choice([
                    self.value[::-1],  # 反转字符串
                    f"{self.value}_mutated",  # 拼接后缀
                    "invalid_string",  # 非法字符串
                    "",  # 空字符串
                    None  # 空值
                ])

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
        if "txpool_size" in self.key.lower() or "max_txpool_size" in self.key.lower():
            # 交易池大小：设置极端值、非法值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用交易池
                    -1,  # 非法负值
                    self.value * 2,  # 倍数变化
                    self.value + random.randint(-5000, 5000),  # 随机偏移
                    1000000  # 超大值
                ])

        elif "tx_timeout" in self.key.lower() or "tx_expiration" in self.key.lower():
            # 交易超时或过期时间：极短时间、极长时间或随机变化
            while mutated_value == self.value:
                mutated_value = random.choice([
                    1,  # 极短过期时间
                    self.value * 10,  # 倍数变化
                    self.value + random.randint(-1000, 1000),  # 随机偏移
                    0,  # 禁用过期时间
                    -1  # 非法负值
                ])

        elif "tx_rate_limit" in self.key.lower():
            # 交易速率限制：禁用、超大值或非法值
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用速率限制
                    -1,  # 非法负值
                    self.value * 2,  # 倍数变化
                    self.value + random.randint(-100, 100),  # 随机偏移
                    1000000  # 超高速率限制
                ])

        elif "batch" in self.key.lower():
            # 批量处理相关配置
            if "batch_size" in self.key.lower():
                # 批量大小：设置非法值或极端值
                while mutated_value == self.value:
                    mutated_value = random.choice([
                        0,  # 禁用批量
                        -1,  # 非法负值
                        self.value * 2,  # 倍数变化
                        self.value + random.randint(-50, 100)  # 随机偏移
                    ])
            elif "batch_timeout" in self.key.lower():
                # 批量超时：极短时间或极长时间
                while mutated_value == self.value:
                    mutated_value = random.choice([
                        1,  # 极短时间
                        self.value * 10,  # 倍数变化
                        self.value + random.randint(-100, 500),  # 随机偏移
                    ])

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
                mutated_value = random.choice([
                    self.value[::-1],  # 反转字符串
                    f"{self.value}_mutated",  # 拼接后缀
                    "invalid_string",  # 非法字符串
                    "",  # 空字符串
                    None  # 空值
                ])

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
        if "storage_path" in self.key.lower() or "db_path" in self.key.lower():
            # 存储路径：非法路径、无权限路径或随机路径
            invalid_paths = [
                "/invalid/path",
                "/root/restricted_path",
                "/tmp/invalid_db_path",
                "",
                None
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_paths + [
                    f"/tmp/test_storage_{random.randint(1, 1000)}"
                ])

        elif "write_buffer_size" in self.key.lower() or "cache_size" in self.key.lower():
            # 缓冲区大小或缓存大小：设置极端值、非法值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用缓冲
                    -1,  # 非法负值
                    self.value * 2,  # 倍数变化
                    self.value + random.randint(-1024, 1024)  # 随机偏移
                ])

        elif "flush_interval" in self.key.lower() or "gc_interval" in self.key.lower():
            # 刷新间隔或垃圾回收间隔：极短时间或极长时间
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用
                    1,  # 极短时间
                    self.value * 10,  # 倍数变化
                    self.value + random.randint(-1000, 1000)  # 随机偏移
                ])

        elif "compression" in self.key.lower():
            # 压缩相关配置：布尔值直接反转或插入非法值
            if isinstance(self.value, bool):
                mutated_value = not self.value
            else:
                while mutated_value == self.value:
                    mutated_value = random.choice(["invalid_compression", "gzip", "snappy", "none"])

        elif "max_open_files" in self.key.lower():
            # 最大打开文件数：设置极端值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用
                    -1,  # 非法负值
                    self.value * 2,  # 倍数变化
                    self.value + random.randint(-100, 100)  # 随机偏移
                ])

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
                mutated_value = random.choice([
                    self.value[::-1],  # 反转字符串
                    f"{self.value}_mutated",  # 拼接后缀
                    "invalid_string",  # 非法字符串
                    "",  # 空字符串
                    None  # 空值
                ])

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
