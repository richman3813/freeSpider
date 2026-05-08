import requests
import time
import json

# 目标请求地址
CRAWL_API = "http://127.0.0.1:8000/crawl"
# 域名文件路径
DOMAIN_FILE = "domains.txt"
# 每次请求后休眠时间(秒)
SLEEP_TIME = 20
# 请求头（指定json格式）
HEADERS = {"Content-Type": "application/json; charset=utf-8"}


def crawl_domain():
    try:
        # 按行读取域名文件，编码为utf-8
        with open(DOMAIN_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total = len(lines)
        print(f"共读取到 {total} 条域名，开始发起请求...\n")

        # 遍历每一条域名
        for index, line in enumerate(lines, start=1):
            # 清理首尾空白字符（换行、空格、制表符）
            domain = line.strip()
            # 过滤空行
            if not domain:
                print(f"第{index}行：空行，跳过")
                continue

            # 兼容处理：去除域名末尾的/，避免拼接/home出现//
            target_url = domain.rstrip("/")
            # 构造请求体
            request_body = {"url": target_url}

            try:
                # 发起POST请求
                response = requests.post(
                    url=CRAWL_API,
                    headers=HEADERS,
                    data=json.dumps(request_body),
                    timeout=10  # 超时时间10秒，避免请求挂起
                )
                # 打印请求结果
                print(f"第{index}/{total}条 | 目标URL：{target_url}")
                print(f"响应状态码：{response.status_code} | 响应内容：{response.text[:200]}...\n")

            except requests.exceptions.RequestException as e:
                # 捕获所有请求异常（超时、连接失败、网络错误等）
                print(f"第{index}/{total}条 | 目标URL：{target_url} | 请求失败：{str(e)}\n")

            # 最后一条请求不休眠，避免多余等待
            if index < total:
                time.sleep(SLEEP_TIME)

    except FileNotFoundError:
        print(f"错误：未找到文件 {DOMAIN_FILE}，请检查文件路径是否正确")
    except Exception as e:
        print(f"程序运行异常：{str(e)}")


if __name__ == "__main__":
    crawl_domain()
    print("所有域名请求处理完成！")