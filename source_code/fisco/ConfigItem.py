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


        if "min_seal_time" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 2,
                    self.value + random.randint(-500, 500)
                ])

        elif "timeout" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    1,
                    self.value * 10,
                    self.value + random.randint(-100, 100)
                ])

        elif "enable" in self.key.lower():

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
            raise RuntimeError(f"{self.key} ({self.value})")

        return mutated_value

    def _mutate_network(self):

        mutated_value = self.value


        if "listen_ip" in self.key.lower():

            invalid_ips = [
                "256.256.256.256",
                "0.0.0.0",
                "127.0.0.1",
                "invalid_ip",
                "[::1]"
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_ips + [
                    f"192.168.{random.randint(0, 255)}.{random.randint(0, 255)}"
                ])

        elif "listen_port" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    65536,
                    self.value + random.randint(-100, 100),
                    random.randint(1, 65535)
                ])

        elif "nodes_path" in self.key.lower() or "nodes_file" in self.key.lower():

            invalid_paths = [
                "/invalid/path",
                "/root/restricted_path",
                "",
                None
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_paths + [
                    f"./nodes_{random.randint(1, 100)}.json"
                ])

        elif "sm_ssl" in self.key.lower():

            mutated_value = not self.value

        elif "enable" in self.key.lower():

            mutated_value = not self.value

        elif "compression" in self.key.lower():

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
            raise RuntimeError(f"")

        return mutated_value

    def _mutate_transaction(self):

        mutated_value = self.value


        if "limit" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 2,
                    self.value + random.randint(-5000, 5000),
                    1000000
                ])

        elif "notify_worker_num" in self.key.lower() or "verify_worker_num" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 2,
                    self.value + random.randint(-10, 50)
                ])

        elif "txs_expiration_time" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 10,
                    self.value + random.randint(-600, 600)
                ])

        elif "send_txs_by_tree" in self.key.lower() or "sync_block_by_tree" in self.key.lower():

            mutated_value = not self.value

        elif "tree_width" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 2,
                    self.value + random.randint(-5, 10)
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
            raise RuntimeError(f" {self.key} ({self.value})")

        return mutated_value

    def _mutate_transaction(self):

        mutated_value = self.value

        if "limit" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 2,
                    self.value + random.randint(-5000, 5000),
                    1000000
                ])

        elif "notify_worker_num" in self.key.lower() or "verify_worker_num" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    0,
                    -1,
                    self.value * 2,
                    self.value + random.randint(-10, 10)
                ])

        elif "txs_expiration_time" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([
                    1,
                    self.value * 10,
                    self.value + random.randint(-1000, 1000),
                    0,
                    -1
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
            raise RuntimeError(f"{self.key} ({self.value})")

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
            raise RuntimeError(f" {self.key} ({self.value})")

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
