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
        if "backend" in self.key.lower():
            # 安全规则存储后端类型变异：切换为其他存储类型或插入非法值
            valid_backends = ["on_disk_storage", "in_memory", "cloud_storage"]
            invalid_backends = ["undefined_backend", "invalid_storage"]
            while mutated_value == self.value:
                mutated_value = random.choice(valid_backends + invalid_backends)

        elif "path" in self.key.lower():
            # 文件路径变异：非法路径、无权限路径或随机路径
            invalid_paths = ["/invalid/path", "/root/restricted_path", "", None]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_paths + [f"/tmp/test_path_{random.randint(1, 1000)}"])

        elif "type" in self.key.lower():
            # 类型字段变异：切换为非法类型或插入无效值
            valid_types = ["thread", "process"]
            invalid_types = ["unknown_type", "invalid_type"]
            while mutated_value == self.value:
                mutated_value = random.choice(valid_types + invalid_types)

        elif "timeout" in self.key.lower():
            # 超时设置：极端值、非法值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用超时
                    -1,  # 非法负值
                    self.value * 2,  # 倍数变化
                    self.value + random.randint(-500, 500)  # 随机偏移
                ])

        elif "identity_blob_path" in self.key.lower():
            # 身份文件路径变异：使用非法路径或生成随机路径
            while mutated_value == self.value:
                mutated_value = random.choice([
                    "/invalid/identity/path",
                    None,
                    f"/tmp/test_identity_{random.randint(1, 1000)}.yaml"
                ])

        elif "namespace" in self.key.lower():
            # 命名空间：插入非法值或随机字符串
            while mutated_value == self.value:
                mutated_value = random.choice(["invalid_namespace", "", None, f"namespace_{random.randint(1, 100)}"])

        elif isinstance(self.value, bool):
            # 布尔值：直接反转
            mutated_value = not self.value

        elif isinstance(self.value, int):
            # 整数值：设置为极端值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 极小值
                    -1,  # 非法负值
                    99999,  # 极大值
                    self.value + random.randint(-500, 500)  # 偏移
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
        if "port" in self.key.lower():
            # 端口号变异：非法端口、极端值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 非法端口
                    65536,  # 超出范围端口
                    self.value + random.randint(-100, 100),  # 随机偏移
                    random.randint(1, 65535)  # 有效随机端口
                ])

        elif "address" in self.key.lower():
            # 地址变异：无效地址、随机生成地址或非法格式
            invalid_addresses = [
                "0.0.0.0:0",
                "256.256.256.256:80",
                "[::1]:invalid_port",  # IPv6 无效端口
                "127.0.0.1:-1"
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_addresses + [
                    f"192.168.1.{random.randint(0, 255)}:{random.randint(1024, 65535)}"  # 随机有效地址
                ])

        elif "rate_limit" in self.key.lower():
            # 速率限制配置变异：禁用、极大值、负值或随机范围
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用速率限制
                    -1,  # 非法负值
                    random.randint(1, 1000000),  # 随机大值
                    self.value * 2 if isinstance(self.value, int) else None  # 倍数变化
                ])

        elif "ping_interval" in self.key.lower():
            # Ping 间隔变异：设置为极短或极长时间
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用 Ping
                    1,  # 极短间隔
                    self.value * 10,  # 倍数变化
                    self.value + random.randint(-1000, 1000)  # 随机偏移
                ])

        elif "max_connection" in self.key.lower():
            # 最大连接数变异：非法值、极端值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 无连接
                    -1,  # 非法负值
                    99999,  # 极端大值
                    self.value + random.randint(-50, 50)  # 随机偏移
                ])

        elif "enable" in self.key.lower():
            # 布尔值：直接反转
            mutated_value = not self.value

        elif "buffer_size_bytes" in self.key.lower():
            # 缓冲区大小：设置极端值或随机变化
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用缓冲
                    -1,  # 非法负值
                    self.value * 2,  # 倍数变化
                    self.value + random.randint(-1024, 1024)  # 随机偏移
                ])

        elif isinstance(self.value, int):
            # 通用整数值变异：设置极端值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([0, -1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, str):
            # 字符串值变异：插入特殊字符或非法值
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
        if "txpoolsize" in self.key.lower() or "max_txpool_size" in self.key.lower():
            # 交易池大小：设置极端值、非法值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用交易池
                    -1,  # 非法负值
                    self.value * 2,  # 倍数变化
                    self.value + random.randint(-5000, 5000),  # 随机偏移
                    1000000  # 超大值
                ])

        elif "txpooltype" in self.key.lower():
            # 交易池类型：切换为非法类型或添加无效值
            valid_types = ["normal", "single", "priority"]
            invalid_types = ["undefined", "invalid_type"]
            while mutated_value == self.value:
                mutated_value = random.choice(valid_types + invalid_types)

        elif "batch" in self.key.lower():
            # 批量处理相关配置
            if "create_timeout" in self.key.lower():
                # 批量创建超时：极短、极长或随机偏移
                while mutated_value == self.value:
                    mutated_value = random.choice([
                        0,  # 禁用批量超时
                        1,  # 极短超时
                        self.value * 10,  # 倍数变化
                        self.value + random.randint(-100, 100)  # 随机偏移
                    ])
            elif "max_size" in self.key.lower():
                # 批量最大大小：非法值或倍数变化
                while mutated_value == self.value:
                    mutated_value = random.choice([
                        0,  # 禁用批量
                        -1,  # 非法负值
                        self.value * 2,  # 倍数变化
                        self.value + random.randint(-100, 500)  # 随机偏移
                    ])

        elif "queue" in self.key.lower():
            # 队列相关配置
            if "common_queue_num" in self.key.lower():
                # 队列数量：非法值或倍数变化
                while mutated_value == self.value:
                    mutated_value = random.choice([
                        0,  # 禁用队列
                        -1,  # 非法负值
                        self.value * 2,  # 倍数变化
                        self.value + random.randint(-10, 50)  # 随机偏移
                    ])
            elif "is_dump_txs_in_queue" in self.key.lower():
                # 布尔值：始终反转
                mutated_value = not self.value

        elif "timeout" in self.key.lower():
            # 超时配置：极短、极长或非法值
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用超时
                    -1,  # 非法负值
                    self.value * 10,  # 倍数变化
                    self.value + random.randint(-1000, 1000)  # 随机偏移
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
        if "path" in self.key.lower():
            # 存储路径变异：非法路径、无权限路径或随机路径
            invalid_paths = [
                "/invalid/path",
                "/root/restricted_path",
                "/tmp/unexpected_file",
                "",
                None
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_paths + [f"/tmp/test_path_{random.randint(1, 1000)}"])

        elif "backup_service_address" in self.key.lower():
            # 备份服务地址：使用非法地址、随机生成地址或无效端口
            invalid_addresses = [
                "0.0.0.0:0",
                "256.256.256.256:8080",
                "127.0.0.1:-1",
                "localhost:invalid"
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_addresses + [
                    f"192.168.1.{random.randint(0, 255)}:{random.randint(1024, 65535)}"
                ])

        elif "enable" in self.key.lower():
            # 布尔值：直接反转
            mutated_value = not self.value

        elif "max_frame_size" in self.key.lower() or "max_message_size" in self.key.lower():
            # 最大帧大小或消息大小：设置极端值、非法值或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用
                    -1,  # 非法负值
                    self.value * 2,  # 倍数变化
                    self.value + random.randint(-1024, 1024)  # 随机偏移
                ])

        elif "timeout" in self.key.lower() or "interval" in self.key.lower():
            # 超时或间隔配置：极短、极长或随机偏移
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,  # 禁用
                    1,  # 极短间隔
                    self.value * 10,  # 倍数变化
                    self.value + random.randint(-1000, 1000)  # 随机偏移
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
