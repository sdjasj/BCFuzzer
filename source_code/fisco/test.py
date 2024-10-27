import threading

# 共享集合
shared_set = {1, 2, 3, 4, 5}
lock = threading.Lock()

def check_in_set(item):
    """
    检查元素是否在集合中，线程安全。
    """
    with lock:
        if item in shared_set:
            print(f"{item} is in the set", item in shared_set)
        else:
            print(f"{item} is NOT in the set", item in shared_set)

def add_to_set(item):
    """
    添加元素到集合，线程安全。
    """
    with lock:
        shared_set.add(item)
        print(f"Added {item} to the set")

# 测试多线程检查和添加集合元素
def worker_thread(item):
    """
    模拟一个线程任务，检查元素是否在集合中，并添加元素。
    """
    check_in_set(item)
    add_to_set(item)

if __name__ == "__main__":
    # 创建多个线程，检查元素并可能添加新的元素
    threads = []
    items_to_check = [3, 6, 2, 9, 5]  # 一些要检查的元素
    for item in items_to_check:
        t = threading.Thread(target=worker_thread, args=(item,))
        threads.append(t)
        t.start()

    # 等待所有线程完成
    for t in threads:
        t.join()

    # 最后查看集合内容
    print("Final set:", shared_set)
