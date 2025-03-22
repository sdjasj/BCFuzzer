from enum import Enum
import random


class ConfigType(Enum):
    Consensus = "consensus"
    Network = "network"
    Storage = "storage"
    Transaction = "transaction"
    Other = "other"


class ConfigItem:
    def __init__(self, key, value, config_type=None):
        self.key = key
        self.value = value
        self.config_type = config_type

    def mutate(self):
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
            raise ValueError("The configuration type is unknown and cannot be changed.")

    def _mutate_consensus(self):
        mutated_value = self.value

        if "backend" in self.key.lower():
            valid_backends = ["on_disk_storage", "in_memory", "cloud_storage"]
            invalid_backends = ["undefined_backend", "invalid_storage"]
            while mutated_value == self.value:
                mutated_value = random.choice(valid_backends + invalid_backends)

        elif "path" in self.key.lower():
            invalid_paths = ["/invalid/path", "/root/restricted_path", "", None]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_paths + [f"/tmp/test_path_{random.randint(1, 1000)}"])

        elif "type" in self.key.lower():
            valid_types = ["thread", "process"]
            invalid_types = ["unknown_type", "invalid_type"]
            while mutated_value == self.value:
                mutated_value = random.choice(valid_types + invalid_types)

        elif "timeout" in self.key.lower():
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 2,
                    self.value + random.randint(-500, 500)
                ])

        elif "identity_blob_path" in self.key.lower():
            while mutated_value == self.value:
                mutated_value = random.choice([
                    "/invalid/identity/path",
                    None,
                    f"/tmp/test_identity_{random.randint(1, 1000)}.yaml"
                ])

        elif "namespace" in self.key.lower():
            while mutated_value == self.value:
                mutated_value = random.choice(["invalid_namespace", "", None, f"namespace_{random.randint(1, 100)}"])

        elif isinstance(self.value, bool):
            mutated_value = not self.value

        elif isinstance(self.value, int):
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    99999,
                    self.value + random.randint(-500, 500)
                ])

        elif isinstance(self.value, str):
            while mutated_value == self.value:
                mutated_value = random.choice([
                    self.value[::-1],
                    f"{self.value}_mutated",
                    "invalid_string",
                    "",
                    None
                ])

        if mutated_value == self.value:
            raise RuntimeError(f"Failed mutation value: {self.key} ({self.value})")

        return mutated_value

    def _mutate_network(self):
        mutated_value = self.value

        if "port" in self.key.lower():
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    65536,
                    self.value + random.randint(-100, 100),
                    random.randint(1, 65535)
                ])

        elif "address" in self.key.lower():
            invalid_addresses = [
                "0.0.0.0:0",
                "256.256.256.256:80",
                "[::1]:invalid_port",
                "127.0.0.1:-1"
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_addresses + [
                    f"192.168.1.{random.randint(0, 255)}:{random.randint(1024, 65535)}"  # 随机有效地址
                ])

        elif "rate_limit" in self.key.lower():
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    random.randint(1, 1000000),
                    self.value * 2 if isinstance(self.value, int) else None
                ])

        elif "ping_interval" in self.key.lower():
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    1,
                    self.value * 10,
                    self.value + random.randint(-1000, 1000)
                ])

        elif "max_connection" in self.key.lower():
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    99999,
                    self.value + random.randint(-50, 50)
                ])

        elif "enable" in self.key.lower():
            mutated_value = not self.value

        elif "buffer_size_bytes" in self.key.lower():
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 2,
                    self.value + random.randint(-1024, 1024)
                ])

        elif isinstance(self.value, int):
            while mutated_value == self.value:
                mutated_value = random.choice([0, -1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, str):
            while mutated_value == self.value:
                mutated_value = random.choice([
                    self.value[::-1],
                    f"{self.value}_mutated",
                    "invalid_string",
                    "",
                    None
                ])

        if mutated_value == self.value:
            raise RuntimeError(f"Failed mutation value: {self.key} ({self.value})")

        return mutated_value

    def _mutate_transaction(self):
        mutated_value = self.value

        if "txpoolsize" in self.key.lower() or "max_txpool_size" in self.key.lower():
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 2,
                    self.value + random.randint(-5000, 5000),
                    1000000
                ])

        elif "txpooltype" in self.key.lower():
            valid_types = ["normal", "single", "priority"]
            invalid_types = ["undefined", "invalid_type"]
            while mutated_value == self.value:
                mutated_value = random.choice(valid_types + invalid_types)

        elif "batch" in self.key.lower():
            if "create_timeout" in self.key.lower():
                while mutated_value == self.value:
                    mutated_value = random.choice([
                        0,
                        1,
                        self.value * 10,
                        self.value + random.randint(-100, 100)
                    ])
            elif "max_size" in self.key.lower():
                while mutated_value == self.value:
                    mutated_value = random.choice([
                        0,
                        -1,
                        self.value * 2,
                        self.value + random.randint(-100, 500)
                    ])

        elif "queue" in self.key.lower():
            if "common_queue_num" in self.key.lower():
                while mutated_value == self.value:
                    mutated_value = random.choice([
                        0,
                        -1,
                        self.value * 2,
                        self.value + random.randint(-10, 50)
                    ])
            elif "is_dump_txs_in_queue" in self.key.lower():
                mutated_value = not self.value

        elif "timeout" in self.key.lower():
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 10,
                    self.value + random.randint(-1000, 1000)
                ])

        elif isinstance(self.value, bool):
            mutated_value = not self.value

        elif isinstance(self.value, int):
            while mutated_value == self.value:
                mutated_value = random.choice([0, -1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, str):
            while mutated_value == self.value:
                mutated_value = random.choice([
                    self.value[::-1],
                    f"{self.value}_mutated",
                    "invalid_string",
                    "",
                    None
                ])

        if mutated_value == self.value:
            raise RuntimeError(f"Failed mutation value: {self.key} ({self.value})")

        return mutated_value

    def _mutate_storage(self):

        mutated_value = self.value


        if "path" in self.key.lower():

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

            mutated_value = not self.value

        elif "max_frame_size" in self.key.lower() or "max_message_size" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 2,
                    self.value + random.randint(-1024, 1024)
                ])

        elif "timeout" in self.key.lower() or "interval" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    1,
                    self.value * 10,
                    self.value + random.randint(-1000, 1000)
                ])

        elif isinstance(self.value, bool):

            mutated_value = not self.value

        elif isinstance(self.value, int):

            while mutated_value == self.value:
                mutated_value = random.choice([0, -1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, str):

            while mutated_value == self.value:
                mutated_value = random.choice([
                    self.value[::-1],
                    f"{self.value}_mutated",
                    "invalid_string",
                    "",
                    None
                ])

        # 如果没有特定规则适用，抛出异常
        if mutated_value == self.value:
            raise RuntimeError(f"Failed mutation value:{self.key} ({self.value})")

        return mutated_value

    def _mutate_other(self):
        mutated_value = self.value

        if isinstance(self.value, int):
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    99999,
                    self.value + random.randint(-500, 500),
                ])

        elif isinstance(self.value, float):
            while mutated_value == self.value:
                mutated_value = random.choice([
                    0.0,
                    -1.0,
                    self.value * random.uniform(0.1, 10.0),
                    round(random.uniform(-1000.0, 1000.0), 3),
                ])

        elif isinstance(self.value, str):
            while mutated_value == self.value:
                mutated_value = random.choice([
                    self.value[::-1],
                    f"{self.value}_mutated",
                    "invalid_string!@#",
                    "",
                    None,
                ])

        elif isinstance(self.value, bool):
            mutated_value = not self.value

        elif isinstance(self.value, list):
            while mutated_value == self.value:
                mutated_value = random.choice([
                    self.value + ["invalid_entry"],
                    self.value + random.choices(self.value, k=2),
                    [],
                ])

        elif isinstance(self.value, dict):
            while mutated_value == self.value:
                mutated_value = random.choice([
                    {**self.value, "invalid_key": "invalid_value"},
                    {},
                ])

        else:
            while mutated_value == self.value:
                mutated_value = random.choice([
                    str(self.value),
                    0,
                    None,
                ])

        if mutated_value == self.value:
            raise RuntimeError(f"未能成功变异值: {self.key} ({self.value})")

        return mutated_value

    def __str__(self):
        if self.config_type:
            return f"{self.key}: {self.value} ({self.config_type.value})"
        return f"{self.key}: {self.value}"

    def to_dict(self):
        return {
            "key": self.key,
            "value": self.value,
            "config_type": self.config_type.value if self.config_type else None,
        }
