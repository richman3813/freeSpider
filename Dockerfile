# 使用 Python 3.12 官方镜像（基于 Debian Bookworm 12）
FROM python:3.12-slim


# 设置工作目录
WORKDIR /app

# 配置与Python 3.12匹配的APT源（Debian Bookworm 12）
RUN echo "deb http://mirrors.aliyun.com/debian bookworm main contrib non-free" > /etc/apt/sources.list && \
    echo "deb http://mirrors.aliyun.com/debian bookworm-updates main contrib non-free" >> /etc/apt/sources.list && \
    echo "deb http://mirrors.aliyun.com/debian-security bookworm-security main contrib non-free" >> /etc/apt/sources.list && \
    # 备用源（防止阿里云源故障）
    echo "deb http://deb.debian.org/debian bookworm main contrib non-free" >> /etc/apt/sources.list && \
    echo "deb http://deb.debian.org/debian bookworm-updates main contrib non-free" >> /etc/apt/sources.list && \
    echo "deb http://deb.debian.org/debian-security bookworm-security main contrib non-free" >> /etc/apt/sources.list

# 合并安装依赖（适配Bookworm版本）
RUN apt-get update --fix-missing && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
    apt-transport-https \
    ca-certificates \
    curl \
    gnupg \
    software-properties-common && \
    # 再安装系统依赖（移除libpango的版本锁定）
    apt-get install -y --no-install-recommends \
    libx11-6 \  
    libxext6 \
    gcc g++ \
    libnss3 libnspr4 \
    libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libcairo2 \
    libasound2 libatspi2.0-0 \
    fonts-noto-color-emoji fonts-noto-cjk \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libpangoft2-1.0-0 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
# 复制并安装Python依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --default-timeout=100 \
    --upgrade pip

# 配置Playwright国内镜像并安装浏览器
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
RUN playwright install chromium

# 创建日志和数据目录
RUN mkdir -p /app/log /app/data

# 复制代码
COPY service.py .

# 启动命令
ENTRYPOINT ["python", "service.py"]
    
