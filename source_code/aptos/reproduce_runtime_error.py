import os
import subprocess
import time
from multinode_config_fuzz import init_log

runtime_error_dir = "/root/test/test_result/test_result_1726363592.8993495/1/runtime_error/"
reproduce_runtime_error_dir = "/root/test/runtime_panic_reproduce/"
config_path = "/tmp/.tmpVNJecV/1/node.yaml"
panic_log_path = "/tmp/.tmpVNJecV/1/log"

def get_all_directories(path):
    # 使用 os.listdir 获取目录下的所有内容，然后使用 os.path.isdir 过滤出文件夹
    return [name for name in os.listdir(path) if os.path.isfile(os.path.join(path, name))]


def check_alive():
    command = "ps -ef | grep {} | grep -v grep".format(config_path)
    # 使用 subprocess.run 执行命令并捕获输出
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    if result.stdout.strip():
        return True
    return False

panic_files = get_all_directories(runtime_error_dir)
for panic_file in panic_files:
    print("start reproduce.....")
    panic_file_path = os.path.join(runtime_error_dir, panic_file)
    os.system("cp {} {}".format(panic_file_path, config_path))
    os.system("./stop.sh")
    time.sleep(3)
    os.system("./start.sh")
    cnt = 0
    while check_alive():
        print("check for {} times".format(cnt + 1))
        cnt += 1
        time.sleep(10)
        if cnt > 10:
            break
    if cnt > 10:
        print("maybe false positive of runtime error....")
        continue
    print("reproduce {} finish".format(panic_file_path))
    record_dir = reproduce_runtime_error_dir + "runtime_panic_" + str(time.time()) + "/"
    os.makedirs(record_dir)
    os.system("cp {} {}".format(config_path, record_dir))
    os.system("cp {} {}".format(panic_log_path, record_dir))
