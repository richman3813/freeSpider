# Web Crawler Service - ReadMeForSpider

## 概述

`service.py` 是一个基于 FastAPI 和 Playwright 的高性能异步网络爬虫服务，专为大规模网站链接抓取和动态内容检测而设计。该服务具备强大的反爬虫机制、分布式任务管理和实时监控功能。

## 核心特性

### 🔍 爬虫功能
- **深度爬取**：支持配置最大爬取深度（默认3层）
- **动态内容检测**：自动识别和抓取通过 AJAX/XHR 加载的动态内容
- **智能链接验证**：自动验证链接有效性，支持重试机制
- **域名限制**：自动限制在指定域名内爬取，避免外部链接
- **URL 规范化**：自动处理相对路径、锚点等，确保链接唯一性

### 🛡️ 反爬虫机制
- **随机延迟**：请求间随机延迟（1.5-3.0秒），模拟人类行为
- **浏览器指纹模拟**：
  - 真实 User-Agent 设置
  - Navigator.webdriver 属性屏蔽
  - 插件和语言环境模拟
  - Chrome 运行时环境注入
- **请求头优化**：
  - 精准的 Accept-Language 和 Accept 头
  - DNT (Do Not Track) 标识
  - 动态 Referer 注入
- **页面行为模拟**：
  - 真实视窗大小（1440x900）
  - 网络空闲检测
  - 页面加载超时控制

### ⚡ 性能优化
- **异步架构**：基于 asyncio 和 Playwright 的异步实现
- **并发控制**：
  - 最大并发任务数可配置（默认10个）
  - 单任务内页面并发控制（默认5个）
  - Redis 原子操作确保并发安全
- **浏览器管理**：
  - 每个任务独立浏览器实例
  - 智能浏览器生命周期管理
  - 启动超时和重试机制
- **内存优化**：
  - 使用集合（Set）存储已访问 URL，避免重复
  - 队列（Deque）管理待爬取任务
  - 默认字典（DefaultDict）组织结果

### 📊 监控与管理
- **Redis 集成**：
  - 任务队列管理
  - 容器状态跟踪
  - 任务结果存储
  - 已发现 URL 计数
- **实时状态监控**：
  - 当前运行任务数
  - 任务队列长度
  - 容器最后活跃时间
  - 已发现 URL 数量
- **详细日志记录**：
  - 系统级日志记录
  - 任务级日志追踪
  - 错误和异常详细记录

## API 接口

### 1. 启动爬取任务
```
POST /crawl
```
**请求体**：
```json
{
  "url": "https://example.com",
  "max_depth": 3,
  "task_id": "optional-custom-id",
  "website_id": "optional-website-id"
}
```

**响应**：
- 成功：返回任务ID和状态
- 排队：当达到最大并发数时，任务进入队列
- 错误：返回具体错误信息

### 2. 获取任务结果
```
GET /task/result/{task_id}
```
**响应**：
```json
{
  "status": "completed",
  "task_id": "task-uuid",
  "discovered_urls": [...],
  "dynamic_urls": [...],
  "api_endpoints": [...],
  "failed_urls": [...],
  "statistics": {...}
}
```

### 3. 容器状态查询
```
GET /container/status
```
**响应**：
```json
{
  "current_tasks": 5,
  "max_tasks": 10,
  "queue_size": 3,
  "last_active": "2024-01-01 12:00:00"
}
```

### 4. 已发现 URL 数量查询
```
GET /task/discovered_count/{task_id}
```

## 配置参数

### 环境变量
- `REDIS_HOST`：Redis 服务器地址（默认：localhost）
- `REDIS_PORT`：Redis 端口（默认：6379）
- `API_PORT`：API 服务端口（默认：8000）
- `MAX_CONCURRENT_TASKS`：最大并发任务数（默认：10）
- `MAX_CONCURRENT_PAGES`：单任务最大并发页面数（默认：5）

### 反爬虫常量
- `HEADLESS`：是否无头模式（默认：True）
- `TIMEOUT_MS`：页面导航超时（默认：60000ms）
- `NETWORKIDLE_MS`：网络空闲超时（默认：10000ms）
- `DELAY_MIN`：最小随机延迟（默认：1.5秒）
- `DELAY_MAX`：最大随机延迟（默认：3.0秒）
- `RETRY_ON_400`：400错误重试次数（默认：2次）

## 架构设计

### 核心类

#### `FinalCrawler` 类
主爬虫类，负责：
- URL 爬取和解析
- 动态内容检测
- 链接验证和规范化
- 浏览器管理
- 结果收集和存储

**主要方法**：
- `extract_full_domain()`：提取完整域名
- `is_internal_link()`：判断是否为内部链接
- `normalize_url()`：URL 规范化
- `validate_link()`：链接验证
- 网络请求监控和动态内容检测相关方法

#### `CrawlRequest` 模型
定义爬虫请求的数据结构：
- `url`：起始URL
- `max_depth`：最大爬取深度
- `task_id`：任务ID（可选）
- `website_id`：网站ID（可选）

### 数据流
1. **任务提交**：通过 `/crawl` 接口提交爬取任务
2. **任务排队**：检查并发限制，超限则进入队列
3. **任务执行**：
   - 创建独立浏览器实例
   - 注入反爬虫脚本
   - 开始爬取流程
   - 实时监控网络请求
4. **结果存储**：将结果存储到 Redis
5. **状态更新**：更新容器状态和任务计数

## 技术栈

- **框架**：FastAPI（异步Web框架）
- **浏览器自动化**：Playwright（现代浏览器自动化）
- **数据存储**：Redis（任务队列和结果存储）
- **HTML解析**：BeautifulSoup4
- **编码检测**：chardet
- **异步支持**：asyncio
- **服务器**：uvicorn

## 部署要求

### 系统依赖
- Python 3.8+
- Redis 服务器
- Chromium 浏览器（Playwright 自动安装）

### Python 依赖
- fastapi
- playwright
- redis
- beautifulsoup4
- chardet
- uvicorn
- pydantic

### 启动命令
```bash
# 安装依赖
pip install -r requirements.txt
playwright install chromium

# 设置环境变量
export REDIS_HOST=localhost
export REDIS_PORT=6379
export API_PORT=8000
export MAX_CONCURRENT_TASKS=10

# 启动服务
python service.py
```

## 监控与维护

### 健康检查
- 通过 `/container/status` 接口监控服务状态
- 检查 Redis 连接和任务队列
- 监控容器最后活跃时间

### 故障排查
1. **任务卡住**：检查浏览器启动超时设置
2. **内存泄漏**：监控长时间运行任务的内存使用
3. **网络问题**：调整超时和重试参数
4. **反爬虫检测**：调整延迟时间和请求头

### 性能调优
- 根据服务器资源调整并发数
- 优化爬取深度和页面超时
- 调整随机延迟范围
- 监控 Redis 性能

## 安全考虑

- 仅支持 HTTP/HTTPS 协议
- 自动限制在指定域名内
- 防止无限重定向循环
- 请求频率限制避免被封禁
- 敏感信息日志过滤

## 扩展性

- 支持分布式部署（多容器）
- Redis 支持集群模式
- 可集成消息队列系统
- 支持自定义爬取规则
- 可扩展结果处理器

---

**版本**：1.0.0  
**最后更新**：2024年1月  
**适用场景**：网站链接抓取、SEO分析、内容监控、链接验证
