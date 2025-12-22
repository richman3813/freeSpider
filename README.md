# freeSpider
基于docker部署的一款支持动态JS爬取的高性能爬虫服务，基于FastApi框架，支持API调用

# 安装与部署
DOCKER_BUILDKIT=1 docker build -t hz-spider:1.2 .

# 启动爬虫任务(以本地运行为例)
-- json消息体力url为要检测的网站首页
-- max_depth 为爬取深度，网站首页为深度0，其下一层为深度1，依次类推
curl -X POST http://192.168.100.18:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{"url":"http://www.boohee.com", "max_depth":2}'

# 支持查看任务详情和任务状态和统计API
  


