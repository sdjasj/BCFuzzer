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

        if "timeout" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([0, 1, 99999, self.value * 2, self.value + random.randint(-200, 200)])

        elif "type" in self.key.lower():

            valid_types = ["PoW", "PoS", "DPoS", "PBFT", "RAFT"]
            invalid_types = ["UnknownConsensus", "InvalidType123"]
            all_types = valid_types + invalid_types
            while mutated_value == self.value:
                mutated_value = random.choice(all_types)

        elif "snap_count" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([1, 2, self.value * 2, max(1, self.value // 2)])

        elif "ticker" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([0.01, 10, 100, self.value * 2, self.value + random.uniform(-1, 5)])

        elif isinstance(self.value, bool):

            mutated_value = not self.value

        elif isinstance(self.value, int):

            while mutated_value == self.value:
                mutated_value = random.choice([0, 1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, str):

            while mutated_value == self.value:
                mutated_value = random.choice([self.value[::-1], f"{self.value}_mutated", "InvalidString!@#"])


        if mutated_value == self.value:
            raise RuntimeError(f" {self.key} ({self.value})")

        return mutated_value

    def _mutate_network(self):

        mutated_value = self.value


        if "port" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice(
                    [0, 65536, self.value + random.randint(-100, 100), random.randint(1, 65535)])

        elif "addr" in self.key.lower():

            invalid_addresses = [
                "0.0.0.0:0",
                "127.0.0.1:-1",
                "invalid_address",
                "256.256.256.256:80",
                "[::1]:8080"
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(
                    invalid_addresses + [f"192.168.1.{random.randint(0, 255)}:{random.randint(1024, 65535)}"])

        elif "seeds" in self.key.lower():

            invalid_seeds = [
                "/ip4/0.0.0.0/tcp/0/p2p/invalid",
                "/ip4/127.0.0.1/tcp/-1/p2p/QmInvalid",
            ]
            while mutated_value == self.value:
                mutated_value = self.value + random.choices(invalid_seeds, k=random.randint(1, 3))

        elif "tls" in self.key.lower():

            invalid_tls_files = [
                "/invalid/path/tls.crt",
                "/invalid/path/tls.key",
                None,
                "",
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_tls_files)

        elif "key" in self.key.lower() or "cert" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice(["/invalid/path", "invalid_key", ""])

        elif isinstance(self.value, bool):

            mutated_value = not self.value

        elif isinstance(self.value, str):

            while mutated_value == self.value:
                mutated_value = random.choice([self.value[::-1], f"{self.value}_mutated", "invalid_string"])

        elif isinstance(self.value, int):

            while mutated_value == self.value:
                mutated_value = random.choice([0, 1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, list):

            while mutated_value == self.value:
                mutated_value = self.value + random.choices(["/invalid/entry", "/duplicate/entry"], k=2)


        if mutated_value == self.value:
            raise RuntimeError(f"{self.key} ({self.value})")

        return mutated_value

    def _mutate_transaction(self):

        mutated_value = self.value


        if "txpoolsize" in self.key.lower():

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
                    mutated_value = random.choice([0, 1, self.value * 10, self.value - random.randint(1, 50)])
            elif "max_size" in self.key.lower():

                while mutated_value == self.value:
                    mutated_value = random.choice([
                        0,
                        -1,
                        self.value * 2,
                        self.value + random.randint(-50, 100)
                    ])

        elif "queue" in self.key.lower():

            if "common_queue_num" in self.key.lower():

                while mutated_value == self.value:
                    mutated_value = random.choice([0, -1, self.value * 2, self.value + random.randint(-10, 20)])
            elif "is_dump_txs_in_queue" in self.key.lower():

                mutated_value = not self.value

        elif isinstance(self.value, bool):

            mutated_value = not self.value

        elif isinstance(self.value, int):

            while mutated_value == self.value:
                mutated_value = random.choice([0, 1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, str):

            while mutated_value == self.value:
                mutated_value = random.choice([self.value[::-1], f"{self.value}_mutated", "invalid_string"])

        if mutated_value == self.value:
            raise RuntimeError(f"未能成功变异值: {self.key} ({self.value})")

        return mutated_value

    def _mutate_storage(self):

        mutated_value = self.value


        if "store_path" in self.key.lower():

            invalid_paths = [
                "/invalid/path",
                "/tmp/readonly_path",
                "",
                None
            ]
            while mutated_value == self.value:
                mutated_value = random.choice(invalid_paths + [f"/tmp/test_path_{random.randint(1, 1000)}"])

        elif "write_buffer_size" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([0, -1, self.value * 2, 10 ** 6])

        elif "provider" in self.key.lower():

            valid_providers = ["leveldb", "rocksdb", "inmemory"]
            invalid_providers = ["undefined", "invalid_provider"]
            while mutated_value == self.value:
                mutated_value = random.choice(valid_providers + invalid_providers)

        elif "cache" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([0, -1, self.value * 2, self.value + random.randint(-100, 100)])

        elif "disable" in self.key.lower():

            mutated_value = not self.value

        elif "interval" in self.key.lower():

            while mutated_value == self.value:
                mutated_value = random.choice([0, 1, self.value * 10, self.value + random.randint(-50, 50)])

        elif isinstance(self.value, bool):

            mutated_value = not self.value

        elif isinstance(self.value, int):

            while mutated_value == self.value:
                mutated_value = random.choice([0, -1, 99999, self.value + random.randint(-500, 500)])

        elif isinstance(self.value, str):

            while mutated_value == self.value:
                mutated_value = random.choice([self.value[::-1], f"{self.value}_mutated", "invalid_string"])

        elif isinstance(self.value, list):

            while mutated_value == self.value:
                mutated_value = self.value + random.choices(["/invalid/entry", "/duplicate/entry"], k=2)


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
                    "invalid_string!@#"
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
            raise RuntimeError(f"{self.key} ({self.value})")

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
