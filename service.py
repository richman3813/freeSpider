import asyncio
import json
import re
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse, urljoin
from collections import defaultdict, deque
from logging.handlers import SysLogHandler
from playwright.async_api import async_playwright, Response, Browser, Page, Playwright
from bs4 import BeautifulSoup
from fastapi import FastAPI, BackgroundTasks, HTTPException
import uvicorn
import redis
from pydantic import BaseModel
import hashlib

from typing import Tuple, List

# Redis配置
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
API_PORT = int(os.getenv("API_PORT", 8000))
CONTAINER_ID = os.getenv("CONTAINER_ID", "unknown-container")
# 关键对齐：最大并发任务数 = 最大浏览器数（每个任务1个浏览器）
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", 10))
MAX_CONCURRENT_PAGES = int(os.getenv("MAX_CONCURRENT_PAGES", 5))  # 单个任务内最大并发页面数
BROWSER_CREATE_RETRY = 3  # 浏览器创建重试次数
BROWSER_START_TIMEOUT = 120000  # 浏览器启动超时（120秒）

# 设置容器内的路径
LOG_DIR = "/app/log"
RESULT_DIR = "/app/data"

# 确保目录存在
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)

# 初始化FastAPI应用
app = FastAPI()
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)

# 全局任务队列键
TASK_QUEUE_KEY = "task_queue"


# 初始化系统日志记录器（用于浏览器创建/关闭、任务调度等系统操作）
def setup_system_logger():
    sys_logger = logging.getLogger("SystemLogger")
    sys_logger.setLevel(logging.INFO)

    # 避免重复添加处理器
    if sys_logger.handlers:
        return sys_logger

    # 尝试配置系统日志（优先）或文件日志（降级）
    try:
        if os.path.exists("/dev/log"):
            sys_handler = SysLogHandler(address="/dev/log")
        elif os.path.exists("/var/run/syslog"):
            sys_handler = SysLogHandler(address="/var/run/syslog")
        else:
            sys_handler = logging.FileHandler("/app/log/web_crawler_system.log")

        # 系统日志格式（含容器ID，便于多容器识别）
        formatter = logging.Formatter(
            f"web-crawler[{CONTAINER_ID}]: %(levelname)s - %(message)s"
        )
        sys_handler.setFormatter(formatter)
        sys_logger.addHandler(sys_handler)

        # 同时输出到控制台（调试用）
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - SystemLogger - %(levelname)s - %(message)s'
        ))
        sys_logger.addHandler(console_handler)

    except Exception as e:
        # 配置失败时使用基础文件日志
        file_handler = logging.FileHandler("/app/log/system_fallback.log")
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - SystemLogger - %(levelname)s - %(message)s'
        ))
        sys_logger.addHandler(file_handler)
        sys_logger.error(f"Failed to configure system log handler: {str(e)}")

    return sys_logger


# 初始化系统日志
system_logger = setup_system_logger()


class CrawlRequest(BaseModel):
    url: str
    max_depth: int = 3
    task_id: str = None
    website_id: str = None


class FinalCrawler:
    def __init__(self, start_url, max_depth=3, logger=None, task_id=None):
        self.base_full_domain = self.extract_full_domain(start_url)
        self.start_url = start_url
        self.max_depth = max_depth
        self.task_id = task_id or str(uuid.uuid4())  # 任务唯一标识
        self.visited = set()
        self.discovered_urls = set()
        self.url_depth_map = {}
        self.results = defaultdict(list)
        self.queue = deque()
        self.logger = logger or logging.getLogger(f"WebCrawler-{self.task_id}")
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)  # 单个任务内控制并发页面
        self.failed_urls = []
        # 浏览器相关资源（每个任务独立持有）
        self.playwright: Playwright = None
        self.browser: Browser = None
        self.browser_id = f"browser-{self.task_id[:8]}"  # 浏览器唯一标识（关联任务ID）
        # 浏览器请求头（模拟真实浏览器）
        self.valid_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0"
        }

    def extract_full_domain(self, url):
        """提取完整域名（含子域名），用于判断内外链"""
        try:
            parsed = urlparse(url)
            return parsed.netloc.lower()
        except Exception as e:
            self.logger.error(f"Full domain extraction error for {url}: {str(e)}")
            return ""

    def is_internal_link(self, url):
        """严格判断是否为内部链接（完整域名一致）"""
        if not url or not self.base_full_domain:
            return False
        link_full_domain = self.extract_full_domain(url)
        return link_full_domain == self.base_full_domain

    def normalize_url(self, url):
        """URL规范化（去重、补全路径等）"""
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https'):
                self.logger.debug(f"Skip non-HTTP URL: {url}")
                return ""

            # 保留路径尾部斜杠（避免同一页面被识别为不同URL）
            path = parsed.path
            if path and not path.endswith('/') and not os.path.splitext(path)[1]:
                path += '/'

            # 排序URL参数（避免参数顺序不同导致的重复）
            query = parsed.query
            if query:
                query_parts = sorted(query.split('&'))
                query = '&'.join(query_parts)

            cleaned = parsed._replace(fragment="", path=path, query=query)
            return cleaned.geturl()
        except Exception as e:
            self.logger.error(f"URL normalization error for {url}: {str(e)}")
            return ""

    async def _create_browser(self) -> Tuple[Playwright, Browser]:
        """创建独立浏览器实例（带重试，每个任务调用一次）"""
        system_logger.info(f"Task {self.task_id}: Starting browser creation (id: {self.browser_id})")
        playwright = None
        browser = None

        for attempt in range(BROWSER_CREATE_RETRY):
            try:
                # 启动Playwright
                playwright = await async_playwright().start()
                # 启动Chromium浏览器
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=[
                        '--disable-gpu',
                        '--disable-dev-shm-usage',  # 解决容器共享内存不足
                        '--no-sandbox',  # 容器环境必加（禁用沙箱）
                        '--disable-setuid-sandbox',
                        '--dns-prefetch-disable',  # 禁用DNS预取（减少网络请求）
                        '--disk-cache-size=100000000',  # 限制缓存大小（100MB）
                        '--aggressive-cache-discard',  # 主动丢弃缓存
                        '--memory-limit=1536000000',  # 限制单浏览器内存（1.5GB，避免资源超限）
                        '--no-zygote',  # 禁用zygote进程（减少内存占用）
                        '--disable-extensions',  # 禁用扩展（避免冲突）
                        '--disable-background-networking',  # 禁用后台网络请求
                    ],
                    timeout=BROWSER_START_TIMEOUT
                )
                # 验证浏览器连接
                if not browser.is_connected():
                    raise Exception("Browser created but not connected")

                system_logger.info(
                    f"Task {self.task_id}: Browser {self.browser_id} created successfully (attempt {attempt + 1}/{BROWSER_CREATE_RETRY})")
                return playwright, browser

            except Exception as e:
                # 清理失败的资源
                if browser:
                    try:
                        await browser.close()
                    except:
                        pass
                if playwright:
                    try:
                        await playwright.stop()
                    except:
                        pass

                err_msg = str(e)[:80]  # 截断长错误信息
                if attempt < BROWSER_CREATE_RETRY - 1:
                    wait_time = 5 * (attempt + 1)  # 重试间隔递增（5s, 10s...）
                    system_logger.warning(
                        f"Task {self.task_id}: Browser creation failed (attempt {attempt + 1}/{BROWSER_CREATE_RETRY}): {err_msg}. Retry after {wait_time}s")
                    await asyncio.sleep(wait_time)
                else:
                    raise Exception(
                        f"Task {self.task_id}: Browser creation failed after {BROWSER_CREATE_RETRY} attempts: {err_msg}")

        # 理论上不会走到这里（循环内已抛异常）
        raise Exception(f"Task {self.task_id}: Unexpected error in browser creation")

    async def _close_browser(self):
        """关闭浏览器和Playwright（每个任务结束时调用）"""
        system_logger.info(f"Task {self.task_id}: Closing browser (id: {self.browser_id})")

        # 先关闭浏览器
        if self.browser and self.browser.is_connected():
            try:
                await self.browser.close()
                system_logger.info(f"Task {self.task_id}: Browser {self.browser_id} closed successfully")
            except Exception as e:
                system_logger.error(f"Task {self.task_id}: Failed to close browser {self.browser_id}: {str(e)}")

        # 再停止Playwright
        if self.playwright:
            try:
                await self.playwright.stop()
                system_logger.info(f"Task {self.task_id}: Playwright stopped successfully")
            except Exception as e:
                system_logger.error(f"Task {self.task_id}: Failed to stop Playwright: {str(e)}")

        # 重置资源引用
        self.browser = None
        self.playwright = None

    async def validate_link(self, page: Page, url: str) -> bool:
        """验证链接有效性（仅静态资源，发送HEAD请求）"""
        try:
            response = await page.request.head(
                url,
                headers=self.valid_headers,
                timeout=10000  # 10秒超时
            )
            # 允许200（成功）、301/302（重定向）
            return response.ok or response.status in (301, 302)
        except Exception as e:
            self.logger.debug(f"Link validation failed for {url}: {str(e)[:50]}")
            return False

    async def extract_links(self, page: Page, url: str) -> List[str]:
        """从页面提取所有有效链接（含HTML/CSS/JS）"""
        try:
            html_content = await page.content()
            soup = BeautifulSoup(html_content, 'html.parser')
            all_links = set()

            # 1. 提取HTML标签中的链接
            link_tags = {
                'a': 'href', 'link': 'href', 'script': 'src', 'img': 'src', 'iframe': 'src',
                'source': 'src', 'form': 'action', 'area': 'href', 'embed': 'src',
                'video': 'src', 'audio': 'src', 'frame': 'src', 'base': 'href', 'input': 'src'
            }
            base_url = url  # 基础URL（默认当前页面）
            for tag_name, attr in link_tags.items():
                for tag in soup.find_all(tag_name):
                    if tag_name == 'base' and tag.get(attr):
                        base_url = urljoin(url, tag[attr])
                        self.logger.info(f"Found base URL: {base_url} (task: {self.task_id})")
                        continue
                    if tag.get(attr):
                        full_url = urljoin(base_url, tag[attr])
                        all_links.add(full_url)
                        self.logger.debug(f"Extracted from <{tag_name}> tag: {full_url} (task: {self.task_id})")

            # 2. 提取CSS中的链接（内联+外部）
            try:
                # 内联样式
                inline_styles = await page.evaluate("""() => {
                    const styles = [];
                    document.querySelectorAll('*[style]').forEach(el => styles.push(el.style.cssText));
                    document.querySelectorAll('style').forEach(style => styles.push(style.innerHTML));
                    return styles;
                }""")
                # 外部样式表
                external_css_urls = await page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('link[rel="stylesheet"]')).map(link => link.href);
                }""")
                # 加载外部CSS内容
                for css_url in external_css_urls:
                    try:
                        css_resp = await page.request.get(css_url, timeout=10000)
                        if css_resp.ok:
                            inline_styles.append(await css_resp.text())
                    except Exception as e:
                        self.logger.warning(f"Failed to load external CSS {css_url}: {str(e)[:50]}")
                # 提取CSS中的URL（如background-image）
                css_patterns = [r"url\(['\"]?([^'\")]+)['\"]?\)"]
                for style_content in inline_styles:
                    for pattern in css_patterns:
                        for match in re.finditer(pattern, style_content):
                            css_url = match.group(1)
                            full_url = urljoin(base_url, css_url)
                            if full_url.startswith(('http://', 'https://')):
                                all_links.add(full_url)
                                self.logger.debug(f"Extracted from CSS: {full_url} (task: {self.task_id})")
            except Exception as e:
                self.logger.error(f"CSS link extraction failed: {str(e)} (task: {self.task_id})")

            # 3. 提取JavaScript中的链接
            try:
                js_content = await page.evaluate("""() => {
                    return Array.from(document.scripts).map(script => {
                        return script.src ? script.src : script.innerHTML;
                    }).join('\\n');
                }""")
                # 匹配JS中的URL模式
                js_patterns = [
                    r"['\"](https?://[^'\"]+?)['\"]",
                    r"(?:url|src|href)\(['\"]?(.+?)['\"]?\)",
                    r"\.(?:get|post|fetch|load)\(['\"](.+?)['\"]\)",
                    r"window\.location\s*=\s*['\"](.+?)['\"]",
                    r"window\.open\s*\(['\"](.+?)['\"]"
                ]
                for pattern in js_patterns:
                    for match in re.finditer(pattern, js_content):
                        js_url = match.group(1)
                        if not js_url.startswith(('http://', 'https://')):
                            js_url = urljoin(base_url, js_url)
                        all_links.add(js_url)
                        self.logger.debug(f"Extracted from JS: {js_url} (task: {self.task_id})")
            except Exception as e:
                self.logger.error(f"JS link extraction failed: {str(e)} (task: {self.task_id})")

            # 4. 过滤和验证有效链接
            valid_links = []
            static_extensions = {'.pdf', '.png', '.jpg', '.jpeg', '.gif', '.bmp',
                                 '.zip', '.rar', '.tar', '.gz', '.7z',
                                 '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                                 '.mp3', '.mp4', '.avi', '.mov', '.flv',
                                 '.json', '.xml', '.svg', '.ico', '.woff', '.woff2'}
            for link in all_links:
                normalized_link = self.normalize_url(link)
                if not normalized_link:
                    continue
                # 静态资源额外验证有效性
                parsed_link = urlparse(normalized_link)
                file_ext = os.path.splitext(parsed_link.path)[1].lower()
                if file_ext in static_extensions:
                    if not await self.validate_link(page, normalized_link):
                        self.logger.warning(
                            f"Invalid static resource: {normalized_link} (skipped, task: {self.task_id})")
                        continue
                valid_links.append(normalized_link)

            unique_links = list(set(valid_links))
            self.logger.info(f"Extracted {len(unique_links)} valid links from {url} (task: {self.task_id})")
            return unique_links
        except Exception as e:
            self.logger.error(f"Link extraction failed for {url}: {str(e)} (task: {self.task_id})")
            return []

    async def process_page(self, url: str, depth: int) -> List[str]:
        """处理单个页面（爬取内容、提取链接）"""
        # 初始化page和context为None，确保所有代码路径都能访问到
        page: Page = None
        context = None
        try:
            async with self.semaphore:
                # 跳过非HTTP/HTTPS链接
                if not url.startswith(('http://', 'https://')):
                    self.logger.warning(f"Skipping non-HTTP URL: {url} (task: {self.task_id})")
                    return []
                # 跳过已处理或超深度的页面
                if depth > self.max_depth or url in self.visited:
                    return []

                # 1. 静态资源直接记录，不爬取内容
                static_extensions = {'.pdf', '.png', '.jpg', '.jpeg', '.gif', '.bmp',
                                     '.zip', '.rar', '.tar', '.gz', '.7z',
                                     '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                                     '.mp3', '.mp4', '.avi', '.mov', '.flv',
                                     '.json', '.xml', '.svg', '.ico', '.woff', '.woff2'}
                parsed_url = urlparse(url)
                file_ext = os.path.splitext(parsed_url.path)[1].lower()
                if file_ext in static_extensions:
                    self.logger.info(f"Skipping static resource: {url} (depth: {depth}, task: {self.task_id})")
                    self.visited.add(url)
                    link_type = "INTERNAL" if self.is_internal_link(url) else "EXTERNAL"
                    self.results[depth].append(f"{link_type},0.0,200,{url}")
                    self.url_depth_map[url] = depth
                    return []

                # 2. 标记页面为已访问，开始爬取
                self.visited.add(url)
                self.logger.info(
                    f"Crawling page: {url} (depth: {depth}, browser: {self.browser_id}, task: {self.task_id})")
                final_url = url
                navigation_success = False
                page_links = []

                # 页面访问重试策略（深度1重试2次，其他深度重试1次）
                max_retries = 2 if depth == 1 else 1
                retry_delays = [2, 4] if depth == 1 else [2]

                for attempt in range(max_retries):
                    try:
                        # 创建浏览器上下文（隔离不同页面）
                        context = await self.browser.new_context(
                            user_agent=self.valid_headers["User-Agent"],
                            extra_http_headers=self.valid_headers,
                            ignore_https_errors=True,  # 忽略HTTPS证书错误
                            java_script_enabled=True,
                            bypass_csp=True  # 绕过内容安全策略（CSP）
                        )
                        # 拦截非必要资源（减少加载时间）
                        await context.route("**/*", self._filter_resource)
                        # 创建新页面
                        page = await context.new_page()
                        # 监听页面关闭事件
                        page_closing = False

                        def handle_page_close():
                            nonlocal page_closing
                            page_closing = True

                        page.on("close", handle_page_close)

                        # 访问页面（超时时间：深度1用120秒，其他用95秒）
                        timeout = 240000 if depth == 1 else 95000
                        self.logger.info(
                            f"Attempt {attempt + 1}/{max_retries} to load {url} (timeout: {timeout}ms, task: {self.task_id})")
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

                        # 处理重定向（记录最终URL）
                        if response:
                            final_url = page.url
                            if final_url != url:
                                self.logger.info(
                                    f"Page redirected to: {final_url} (original: {url}, task: {self.task_id})")
                                self.visited.add(final_url)

                        # 等待页面渲染（2秒）
                        await asyncio.sleep(2)
                        if page_closing:
                            raise Exception("Page closed during navigation")

                        # 验证页面状态
                        if not page.is_closed():
                            page_state = await page.evaluate("() => document.readyState")
                            if page_state in ["interactive", "complete"]:
                                navigation_success = True
                                self.logger.info(
                                    f"Page loaded successfully: {final_url} (state: {page_state}, task: {self.task_id})")
                                break
                            else:
                                raise Exception(f"Page in invalid state: {page_state}")
                        else:
                            raise Exception("Page closed after redirect")

                    except Exception as e:
                        err_msg = str(e)[:80]
                        if attempt < max_retries - 1:
                            wait_time = retry_delays[attempt]
                            self.logger.warning(
                                f"Attempt {attempt + 1} failed to load {url}: {err_msg}. Retry after {wait_time}s (task: {self.task_id})")
                            await asyncio.sleep(wait_time)
                        else:
                            # 所有重试失败，记录失败URL
                            self.logger.error(
                                f"All {max_retries} attempts failed to load {url}: {err_msg} (task: {self.task_id})")
                            link_type = "INTERNAL" if self.is_internal_link(url) else "EXTERNAL"
                            status_code = 408 if "timeout" in err_msg.lower() else 500
                            self.results[depth].append(f"{link_type},0.0,{status_code},{url}")
                            self.failed_urls.append((url, depth))
                            # 深度1的HTTP链接尝试HTTPS重试
                            if depth == 1 and urlparse(url).scheme == "http" and "https://" not in url:
                                https_url = urlparse(url)._replace(scheme="https").geturl()
                                self.logger.info(f"Retry HTTP -> HTTPS for {url}: {https_url} (task: {self.task_id})")
                                if https_url not in self.visited:
                                    return await self.process_page(https_url, depth)
                            return []
                    finally:
                        # 清理失败的页面/上下文
                        if attempt < max_retries - 1 and not navigation_success:
                            if page and not page.is_closed():
                                try:
                                    await page.close()
                                except:
                                    pass
                            if context:
                                try:
                                    await context.close()
                                except:
                                    pass

                # 3. 提取链接（仅当页面加载成功时）
                if navigation_success and page and not page.is_closed():
                    try:
                        page_links = await self.extract_links(page, final_url)
                    except Exception as e:
                        self.logger.error(f"Failed to extract links from {final_url}: {str(e)} (task: {self.task_id})")
                else:
                    self.logger.error(f"Invalid page context for {final_url} (task: {self.task_id})")
                    link_type = "INTERNAL" if self.is_internal_link(final_url) else "EXTERNAL"
                    self.results[depth].append(f"{link_type},0.0,500,{final_url}")

        # 4. 清理页面/上下文资源

        finally:
            if page and not page.is_closed():
                try:
                    await page.close()
                except Exception as e:
                    self.logger.error(f"Failed to close page {final_url}: {str(e)} (task: {self.task_id})")
            if context:
                try:
                    await context.close()
                except Exception as e:
                    self.logger.error(f"Failed to close context for {final_url}: {str(e)} (task: {self.task_id})")
        return page_links

    async def _filter_resource(self, route):
        """过滤非必要资源（减少加载时间和带宽消耗）"""
        resource_type = route.request.resource_type
        # 仅允许：文档、脚本、样式、图片（其他资源如字体、视频等拦截）
        if resource_type in ["document", "script", "stylesheet", "image"]:
            await route.continue_()
        else:
            await route.abort()

    async def crawl(self):
        """核心爬虫逻辑（创建浏览器→爬取→关闭浏览器）"""
        try:
            # 1. 创建独立浏览器（每个任务一次）
            self.playwright, self.browser = await self._create_browser()

            # 2. 初始化爬取队列
            normalized_start_url = self.normalize_url(self.start_url)
            if not normalized_start_url:
                raise Exception(f"Invalid HTTP/HTTPS URL: {self.start_url} (task: {self.task_id})")
            self.queue.append((normalized_start_url, 1))
            self.discovered_urls.add(normalized_start_url)
            self.logger.info(
                f"Task {self.task_id} started: crawling {normalized_start_url} (max depth: {self.max_depth})")

            # 3. 处理队列中的页面
            while self.queue:
                url, depth = self.queue.popleft()
                # 爬取当前页面，获取子链接
                child_links = await self.process_page(url, depth)
                next_depth = depth + 1

                # 4. 过滤子链接（控制深度、去重、内外链）
                if next_depth > self.max_depth:
                    continue
                filtered_child_links = []
                for link in child_links:
                    normalized_link = self.normalize_url(link)
                    if not normalized_link or normalized_link in self.discovered_urls:
                        continue
                    # 深度3及以上仅保留内部链接
                    if next_depth >= 3 and not self.is_internal_link(normalized_link):
                        self.logger.debug(
                            f"Skipping external link in depth {next_depth}: {normalized_link} (task: {self.task_id})")
                        continue
                    filtered_child_links.append(normalized_link)

                # 5. 加入队列并标记为已发现
                for link in filtered_child_links:
                    self.discovered_urls.add(link)
                    self.url_depth_map[link] = next_depth
                    link_type = "INTERNAL" if self.is_internal_link(link) else "EXTERNAL"
                    self.results[next_depth].append(f"{link_type},0.0,200,{link}")
                    # 仅内部链接加入爬取队列
                    if self.is_internal_link(link) and link not in self.visited:
                        self.queue.append((link, next_depth))

            # 6. 格式化爬取结果
            return self._format_results()

        except Exception as e:
            err_msg = str(e)
            self.logger.error(f"Task {self.task_id} failed: {err_msg}", exc_info=True)
            return {
                "task_id": self.task_id,
                "status": "failed",
                "error": err_msg,
                "count_2": len(self.results.get(2, [])),
                "count_3": len(self.results.get(3, [])),
                "failed_urls": [{"url": url, "depth": depth} for url, depth in self.failed_urls],
                "failed_count": len(self.failed_urls)
            }

        finally:
            # 关键：无论任务成功/失败，都必须关闭浏览器
            await self._close_browser()
            self.logger.info(f"Task {self.task_id} completed. Total failed URLs: {len(self.failed_urls)}")

    def _format_results(self):
        """格式化爬取结果（符合原有输出格式）"""
        result_array = []
        depth_counts = {}
        # 整理各深度结果（从深度2开始，深度1为起始页）
        for depth in range(2, self.max_depth + 1):
            depth_links = self.results.get(depth, [])
            depth_counts[f"count_{depth}"] = len(depth_links)
            result_array.append(depth_links)
        # 加入失败URL列表
        result_array.append([f"FAILED,0.0,500,{url},{depth}" for url, depth in self.failed_urls])
        # 返回最终结果
        return {
            "task_id": self.task_id,
            "status": "1",
            **depth_counts,
            "result": result_array,
            "failed_urls": [{"url": url, "depth": depth} for url, depth in self.failed_urls],
            "failed_count": len(self.failed_urls),
            "total_discovered": len(self.discovered_urls)
        }


def setup_task_logger(target_url: str, task_id: str):
    """设置爬虫任务日志（保持原有文件存储方式，不改变）"""
    os.makedirs(LOG_DIR, exist_ok=True)
    # 提取域名作为日志文件名（避免特殊字符）
    domain = urlparse(target_url).netloc or "unknown_domain"
    safe_domain = re.sub(r'[^\w\.-]', '_', domain)[:30]
    log_filename = f"crawler_{safe_domain}.log"
    log_path = os.path.join(LOG_DIR, log_filename)

    # 初始化日志器（独立于系统日志）
    logger = logging.getLogger(f"WebCrawler-{task_id}")
    logger.setLevel(logging.INFO)
    if logger.hasHandlers():
        logger.handlers.clear()

    # 文件日志（保留原有格式）
    file_handler = logging.FileHandler(log_path, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(file_handler)

    # 控制台日志（便于实时查看）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    logger.addHandler(console_handler)

    return logger


async def run_crawler_async(request: CrawlRequest):
    """异步执行单个爬虫任务（独立创建浏览器）"""
    # 确保任务ID唯一
    task_id = request.task_id or str(uuid.uuid4())
    # 初始化任务日志
    task_logger = setup_task_logger(request.url, task_id)
    # 创建爬虫实例
    crawler = FinalCrawler(
        start_url=request.url,
        max_depth=request.max_depth,
        logger=task_logger,
        task_id=task_id
    )

    try:
        # 执行爬取
        results = await crawler.crawl()
        # 保存结果到文件（保持原有路径）
        domain = urlparse(request.url).netloc or "unknown_domain"
        safe_domain = re.sub(r'[^\w\.-]', '_', domain)[:30]
        result_path = os.path.join(RESULT_DIR, f"results_{safe_domain}.json")
        with open(result_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        task_logger.info(f"Task {task_id} results saved to: {result_path}")
        # 保存结果到Redis（便于接口查询）
        r.setex(f"task_result:{task_id}", 86400, json.dumps(results))
        return results
    except Exception as e:
        err_msg = str(e)
        task_logger.error(f"Task {task_id} unexpected error: {err_msg}", exc_info=True)
        return {
            "task_id": task_id,
            "status": "failed",
            "error": err_msg,
            "failed_count": 1
        }


def update_container_status(is_task_start: bool):
    """
    更新容器任务数（原子操作）
    :param is_task_start: True=任务开始（+1），False=任务结束（-1）
    """
    try:
        task_key = f"container:{CONTAINER_ID}:tasks"
        # 原子操作：任务开始+1，任务结束-1（避免并发竞争）
        if is_task_start:
            current_tasks = r.incr(task_key)  # 原子+1，返回更新后的值
        else:
            current_tasks = r.decr(task_key)  # 原子-1，返回更新后的值
            current_tasks = max(0, current_tasks)  # 确保不出现负数

        # 同步更新最后活跃时间
        r.setex(f"container:{CONTAINER_ID}:last_active", 120, str(int(time.time())))
        system_logger.debug(f"Container status updated: current_tasks={current_tasks}, last_active={int(time.time())}")
    except Exception as e:
        system_logger.error(f"Failed to update container status: {str(e)}")


async def run_crawler_task(request: CrawlRequest):
    """包装任务执行逻辑（状态管理、异常捕获）"""
    task_id = request.task_id or str(uuid.uuid4())
    task_key = f"task:{task_id}"

    # 1. 初始化任务状态（Redis）
    try:
        # 逐个字段设置，每个hset只处理一个键值对
        r.hset(task_key, "status", "processing")
        r.hset(task_key, "start_time", str(int(time.time())))
        r.hset(task_key, "url", request.url)
        r.hset(task_key, "max_depth", str(request.max_depth))
        r.hset(task_key, "website_id", request.website_id or "")
        r.hset(task_key, "container_id", CONTAINER_ID)
        r.expire(task_key, 86400)  # 任务状态保留24小时
        system_logger.info(f"Task {task_id} initialized: url={request.url}, max_depth={request.max_depth}")
    except Exception as e:
        system_logger.error(f"Failed to initialize task {task_id} status: {str(e)}")
        return {"status": "error", "task_id": task_id, "error": "Failed to initialize task status"}

    # 2. 更新容器当前任务数（+1）

    update_container_status(is_task_start=True)  # 不再手动计算，直接调用原子操作
    current_tasks = int(r.get(f"container:{CONTAINER_ID}:tasks") or 0)
    system_logger.info(f"Task {task_id} started: current_tasks={current_tasks}")


    try:
        # 3. 执行爬虫任务
        results = await run_crawler_async(request)
        # 4. 更新任务状态为“完成”
        # 4. 更新任务状态为“完成”（逐个字段设置，兼容所有Redis-Py版本）
        r.hset(task_key, "status", "completed")
        r.hset(task_key, "end_time", str(int(time.time())))
        r.hset(task_key, "failed_count", str(results.get("failed_count", 0)))
        system_logger.info(f"Task {task_id} completed: failed_count={results.get('failed_count', 0)}")
        return {"status": "success", "task_id": task_id, "results": results}
    except Exception as e:
        err_msg = str(e)
        # 5. 更新任务状态为“失败”

        r.hset(task_key, "status", "failed")
        r.hset(task_key, "end_time", str(int(time.time())))
        r.hset(task_key, "error", err_msg)
        system_logger.error(f"Task {task_id} failed: {err_msg}")
        return {"status": "error", "task_id": task_id, "error": err_msg}
    finally:
        # 6. 更新容器当前任务数（-1）
        update_container_status(is_task_start=False)  # 原子操作，避免并发问题
        # 更新后读取最新任务数，用于日志
        current_tasks = int(r.get(f"container:{CONTAINER_ID}:tasks") or 0)
        system_logger.info(f"Task {task_id} finished: current_tasks={current_tasks}")
        # 7. 检查任务队列，继续执行下一个任务
        await process_task_queue()


async def process_task_queue():
    """处理任务队列（确保不超过最大并发数）"""
    current_tasks = int(r.get(f"container:{CONTAINER_ID}:tasks") or 0)
    # 若当前任务数已达上限，不处理队列
    if current_tasks >= MAX_CONCURRENT_TASKS:
        system_logger.debug(f"Task queue skipped: current_tasks={current_tasks} >= max_tasks={MAX_CONCURRENT_TASKS}")
        return

    # 从队列头部获取一个任务
    task_data = r.lpop(TASK_QUEUE_KEY)
    if not task_data:
        system_logger.debug("Task queue is empty, no tasks to process")
        return

    try:
        # 解析任务数据
        request_data = json.loads(task_data)
        request = CrawlRequest(**request_data)
        system_logger.info(f"Processing queued task: task_id={request.task_id or 'auto'}, url={request.url}")
        # 执行任务（非阻塞，直接调用）
        asyncio.create_task(run_crawler_task(request))
    except Exception as e:
        err_msg = str(e)[:80]
        system_logger.error(f"Failed to process queued task: {err_msg}")
        # 失败任务不重新入队（避免死循环）
        return


@app.post("/crawl")
async def start_crawl(request: CrawlRequest, background_tasks: BackgroundTasks):
    """启动爬虫任务接口（核心API）"""
    # 1. 验证URL格式
    if not request.url.startswith(('http://', 'https://')):
        return HTTPException(
            status_code=400,
            detail=f"Invalid URL: {request.url} (only HTTP/HTTPS are supported)"
        )

    # 2. 生成/验证任务ID
    task_id = request.task_id or str(uuid.uuid4())
    request.task_id = task_id  # 确保后续流程使用统一ID

    # 3. 检查当前任务数，判断是否入队
    current_tasks = int(r.get(f"container:{CONTAINER_ID}:tasks") or 0)
    if current_tasks >= MAX_CONCURRENT_TASKS:
        # 任务数超限，加入队列
        try:
            r.rpush(TASK_QUEUE_KEY, json.dumps(request.dict()))
            queue_size = r.llen(TASK_QUEUE_KEY)
            system_logger.info(
                f"Task {task_id} added to queue: current_tasks={current_tasks}, queue_position={queue_size}")
            return {
                "status": "queued",
                "task_id": task_id,
                "message": f"Task queued (max concurrent tasks: {MAX_CONCURRENT_TASKS})",
                "queue_position": queue_size
            }
        except Exception as e:
            return HTTPException(
                status_code=500,
                detail=f"Failed to add task to queue: {str(e)}"
            )

    # 4. 任务数未超限，直接启动
    background_tasks.add_task(run_crawler_task, request)
    system_logger.info(f"Task {task_id} started immediately: current_tasks={current_tasks + 1}")
    return {
        "status": "started",
        "task_id": task_id,
        "message": "Crawl task started in background",
        "current_concurrent_tasks": current_tasks + 1
    }


@app.get("/status/{task_id}")
async def get_task_status(task_id: str):
    """获取任务状态接口"""
    r.setex(
        f"container:{CONTAINER_ID}:last_active",
        240,
        str(int(time.time()))
    )
    task_info = r.hgetall(f"task:{task_id}")

    if not task_info:
        raise HTTPException(status_code=404, detail="Task not found")
    return task_info


@app.get("/result/{task_id}")
async def get_task_result(task_id: str):
    """获取任务结果接口"""
    result_json = r.get(f"task_result:{task_id}")
    if not result_json:
        raise HTTPException(status_code=404, detail="Result not found")

    try:
        return json.loads(result_json)
    except json.JSONDecodeError:
        return {"error": "Failed to parse result"}


@app.get("/container/status")
async def get_container_status():
    """获取容器状态接口（移除浏览器池相关，仅保留任务状态）"""
    try:
        current_tasks = int(r.get(f"container:{CONTAINER_ID}:tasks") or 0)
        last_active = int(r.get(f"container:{CONTAINER_ID}:last_active") or 0)
        queue_size = r.llen(TASK_QUEUE_KEY)
        return {
            "container_id": CONTAINER_ID,
            "current_tasks": current_tasks,
            "max_tasks": MAX_CONCURRENT_TASKS,  # 最大任务数=最大浏览器数
            "queue_size": queue_size,
            "last_active": last_active,
            "browser_mode": "per_task_isolated",  # 标识浏览器模式（每个任务独立）
            "timestamp": int(time.time())
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get container status: {str(e)}"
        )


@app.on_event("startup")
async def startup_event():
    """应用启动事件（初始化容器状态、清空无效队列）"""
    system_logger.info("Application startup started (container: {})".format(CONTAINER_ID))
    try:
        # 初始化容器任务数为0
        r.set(f"container:{CONTAINER_ID}:tasks", "0")
        # 初始化最后活跃时间
        r.setex(f"container:{CONTAINER_ID}:last_active", 120, str(int(time.time())))
        # 清空过期的任务队列（避免重启后执行旧任务）
        r.delete(TASK_QUEUE_KEY)
        system_logger.info("Application startup completed: container status initialized")
    except Exception as e:
        system_logger.error(f"Application startup failed to initialize status: {str(e)}")
        # 启动失败不终止应用，但告警
        system_logger.warning("Application started with partial initialization (check Redis connection)")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭事件（清理临时资源）"""
    system_logger.info("Application shutdown started (container: {})".format(CONTAINER_ID))
    try:
        # 标记容器为非活跃
        r.setex(f"container:{CONTAINER_ID}:last_active", 60, str(int(time.time())))
        # 记录关闭时间
        r.setex(f"container:{CONTAINER_ID}:shutdown_time", 3600, str(int(time.time())))
        system_logger.info("Application shutdown completed: resources cleaned")
    except Exception as e:
        system_logger.error(f"Application shutdown failed to clean resources: {str(e)}")


if __name__ == "__main__":
    # 启动前检查必要环境变量

    # 打印启动信息
    system_logger.info("=" * 50)
    system_logger.info(f"Starting Web Crawler Service (container: {CONTAINER_ID})")
    system_logger.info(f"Max Concurrent Tasks (Browsers): {MAX_CONCURRENT_TASKS}")
    system_logger.info(f"API Port: {API_PORT}")
    system_logger.info(f"Redis Host: {REDIS_HOST}:{REDIS_PORT}")
    system_logger.info("=" * 50)

    # 启动UVicorn服务（单worker，避免多进程资源冲突）
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=API_PORT,
        timeout_keep_alive=600,  # 长连接超时（适应长时间爬虫任务）
        workers=1,
        log_config=None  # 禁用UVicorn默认日志（使用自定义日志）
    )
