import subprocess
import re
import time

# 用于存储基本块信息的全局字典，键为文件路径及位置，值为语句数量和执行次数
coverage_data = {}


# 模拟 goc profile 的输出并解析
def parse_goc_profile(profile_output):
    new_coverage_data = {}

    # 正则表达式解析 goc profile 输出
    pattern = r'(.+):(\d+\.\d+),(\d+\.\d+) (\d+) (\d+)'
    matches = re.findall(pattern, profile_output)

    for match in matches:
        file_path, start_pos, end_pos, stmt_count, exec_count = match
        key = f"{file_path}:{start_pos},{end_pos}"

        stmt_count = int(stmt_count)
        exec_count = int(exec_count)

        # 将基本块信息存储到字典中
        if key in new_coverage_data:
            existing_stmt, existing_exec = new_coverage_data[key]
            new_coverage_data[key] = (stmt_count, max(exec_count, existing_exec))  # 取最大执行次数
        else:
            new_coverage_data[key] = (stmt_count, exec_count)

    return new_coverage_data


# 合并新的基本块覆盖信息到全局覆盖数据中
def merge_coverage_data(new_coverage_data):
    global coverage_data

    for key, (new_stmt_count, new_exec_count) in new_coverage_data.items():
        if key in coverage_data:
            existing_stmt_count, existing_exec_count = coverage_data[key]
            # 更新执行次数，取新的最大执行次数
            coverage_data[key] = (new_stmt_count, max(new_exec_count, existing_exec_count))
        else:
            coverage_data[key] = (new_stmt_count, new_exec_count)


# 计算总语句数量
def get_total_executed_statements():
    total_statements = 0
    for stmt_count, exec_count in coverage_data.values():
        if exec_count > 0:  # 仅统计被执行到的基本块
            total_statements += stmt_count
    return total_statements


# 运行 goc profile 命令并获取输出
def run_goc_profile():
    try:
        # 执行 goc profile 命令
        result = subprocess.run(['goc', 'profile'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            return result.stdout
        else:
            print(f"Error running goc profile: {result.stderr}")
            return None
    except Exception as e:
        print(f"Exception while running goc profile: {e}")
        return None

def append_to_file(total_statements):
    with open("coverage_results.txt", "a") as file:
        file.write(f"累计的被执行到的总语句数量: {total_statements}\n")

# 不断重复运行 goc profile 并更新覆盖信息
def monitor_goc_profile():
    while True:
        print("Running goc profile...")

        # 获取 goc profile 输出
        profile_output = run_goc_profile()
        if profile_output:
            # 解析并更新覆盖数据
            new_coverage_data = parse_goc_profile(profile_output)
            merge_coverage_data(new_coverage_data)

            # 计算累计的覆盖结果
            total_statements = get_total_executed_statements()

            # 打印累积的覆盖结果
            print(f"累计的被执行到的总语句数量: {total_statements}")

            # 将结果追加到文件中
            append_to_file(total_statements)
        else:
            print("Failed to get profile output.")



# 开始监控 goc profile 结果
monitor_goc_profile()