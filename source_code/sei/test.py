import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


# 定义类 A，具有 fuzz() 方法
class A:
    def __init__(self, name):
        self.name = name

    def fuzz(self):
        # 模拟 fuzz() 函数需要花费一些时间
        print(f"{self.name}: Fuzzing started")
        time.sleep(2)  # 模拟时间消耗
        print(f"{self.name}: Fuzzing completed")
        return f"{self.name} fuzz() done"


# 定义类 B，管理多个 A 的对象并执行 fuzz()
class B:
    def __init__(self, objects):
        self.objects = objects
        self.executor = ThreadPoolExecutor(max_workers=len(objects))  # 使用线程池管理异步任务

    def manage_fuzzing(self):
        futures = {self.executor.submit(obj.fuzz): obj for obj in self.objects}

        while True:
            for future in as_completed(futures):
                obj = futures[future]
                result = future.result()
                print(result)  # 显示 fuzz() 函数完成的结果

                # fuzz() 完成后重新提交任务
                futures[self.executor.submit(obj.fuzz)] = obj


# 实例化类 A 的对象
a1 = A("Object1")
a2 = A("Object2")
a3 = A("Object3")

# 实例化类 B，并管理这些 A 的对象
b_manager = B([a1, a2, a3])

# 启动管理线程，持续检查并执行 fuzz() 函数
b_manager.manage_fuzzing()
