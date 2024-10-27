from collections import deque


class AhoCorasick:
    def __init__(self):
        self.trie = {}
        self.fail = {}
        self.output = {}
        self.node_id = 0  # 给每个节点分配唯一的ID
        self.node_map = {}  # 用来存储节点的id映射

    def insert(self, word):
        node = self.trie
        for char in word:
            if char not in node:
                node[char] = {}
            node = node[char]
        node_id = id(node)
        self.output[node_id] = len(word)

    def build(self):
        self.fail = {}
        root_id = id(self.trie)
        self.fail[root_id] = root_id
        queue = deque([self.trie])
        self.node_map[id(self.trie)] = self.trie

        while queue:
            node = queue.popleft()
            node_id = id(node)
            for char, child in node.items():
                if char == 'fail':
                    continue
                child_id = id(child)
                self.node_map[child_id] = child
                # Set fail link for child
                fail_node = self.fail[node_id]
                while fail_node != root_id and char not in self.node_map[fail_node]:
                    fail_node = self.fail[fail_node]
                if char in self.node_map[fail_node]:
                    self.fail[child_id] = id(self.node_map[fail_node][char])
                else:
                    self.fail[child_id] = root_id
                # Set output for child
                self.output[child_id] = max(self.output.get(child_id, 0), self.output.get(self.fail[child_id], 0))
                # Add child to queue
                queue.append(child)

    def find_all_matches(self, s):
        node = self.trie
        root_id = id(self.trie)
        node_id = root_id
        for i, char in enumerate(s):
            while node_id != root_id and char not in self.node_map[node_id]:
                node_id = self.fail[node_id]
            if char in self.node_map[node_id]:
                node = self.node_map[node_id][char]
                node_id = id(node)
                if node_id in self.output and self.output[node_id] > 0:
                    yield (i - self.output[node_id] + 1, i)
            else:
                node = self.trie
                node_id = root_id


def min_concat(words, target):
    # Step 1: Build AC Automaton
    ac = AhoCorasick()
    for word in words:
        ac.insert(word)
    ac.build()

    # Step 2: DP array to store minimum number of strings to form the target
    n = len(target)
    dp = [float('inf')] * (n + 1)
    dp[0] = 0

    # Step 3: Iterate over the target and use AC automaton to find valid prefixes
    for i in range(n):
        if dp[i] == float('inf'):
            continue
        # Find all matches ending at or after i
        for start, end in ac.find_all_matches(target[i:]):
            dp[i + end - start + 1] = min(dp[i + end - start + 1], dp[i] + 1)

    return dp[n] if dp[n] != float('inf') else -1


# 示例
words = ["abc", "aaaaa", "bcdef"]
target = "aabcdabc"
print(min_concat(words, target))  # 输出: 3
