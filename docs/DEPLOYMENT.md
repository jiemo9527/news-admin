# 容器部署

## 持久化布局

```text
/opt/news-admin/
  docker-compose.yml
  .env                  # Compose 参数：NEWS_IMAGE / NEWS_BIND / NEWS_PORT
  data/                 # UID/GID 10001，可写，700
    config.env          # 模型与应用配置，600
    news.db             # SQLite（含账号、来源、记录）
    cache/              # 模型、向量缓存
    thumbs/             # WebP 缩略图
```

镜像内无模型密钥和任何用户数据，不包含本地 `agent.md`。默认 `127.0.0.1:5080`，容器内监听 `0.0.0.0:5080`；不要改用多 worker。

## 首次启动

1. 安装 Docker Engine 与 Compose v2。
2. 下载仓库中的 `docker-compose.yml`；创建 `data` 并赋予 `10001:10001` 所有权。
3. 可将 `.env.example` 复制到 `data/config.env` 并填 key，也可以登录后设置。
4. `docker compose up -d`。
5. `docker compose logs news-admin`：首次生成的管理员账号和随机密码只输出一次，不写 TXT。日志包含初始密码，不要公开粘贴。已有账号不会重复输出密码。
6. 登录后修改管理员密码、配置模型与推送渠道、分别启用抓取与推送计划。

## 公网与反向代理

默认本机绑定，推荐由 HTTPS 反向代理暴露。若没有代理而临时直接访问，设置 `NEWS_BIND=0.0.0.0` 和防火墙白名单；HTTP 不加密，不适合长期传输密码。

Nginx 示例（证书和域名由实际环境提供）：

```nginx
location / {
    proxy_pass http://127.0.0.1:5080;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 256m;
    proxy_read_timeout 300s;
}
```

只有在代理可信且后端不被绕过访问时，才设置 `NEWS_TRUST_PROXY=true`。否则访客能伪造访问记录里的 IP。

## 更新

```bash
docker compose pull
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:5080/healthz
```

生产建议固定 `NEWS_IMAGE=ghcr.io/jiemo9527/news-admin@sha256:...`。更新前可自行手动导出 ZIP；系统不会自动创建备份。优先在无抓取任务时更新。数据库迁移在启动时执行，版本回退前先检查 schema 兼容性。

## 迁移现有数据

迁移时停掉旧端的定时调度，避免两端重复抓取/推送。用 SQLite backup API 取得一致数据库快照，连同 `thumbs/`、所需缓存、应用配置迁至 `data/`；不要复制仍在变动的 WAL 文件，不迁移 `admin_password.txt`。移动后 `chown -R 10001:10001 data`。

已有账号、角色、来源、删除来源的标记、Cron 等均在 SQLite 内保持。程序不会因为容器启动而重置管理员或覆盖用户计划。

## GHCR 发布

`main` 推送触发 `.github/workflows/container.yml`：

1. Python 3.12 安装锁定依赖并运行 pytest。
2. BuildKit 构建 `linux/amd64` 镜像。
3. 全新容器启动测试：健康探针、游客重定向、初始化日志、无密码 TXT、模板资源存在。
4. 使用 `GITHUB_TOKEN` 的 `packages:write` 推送 `latest`、`sha-<完整提交>`（及版本标签）。

公共 GHCR package 可匿名拉取；私有包需有 `read:packages` 权限的凭据，勿把凭据写到 Compose 或代码里。


### 7×24 小时低速回溯（条数不设上限）

7 天模式忽略来源的 `max_items`，处理时间窗内来源公开提供的全部可获取候选；普通时间范围仍遵循每源条数。支持 JSON Feed `next_url`、RSS/Atom `rel=next` 和网页明确的“下一页”链接；同站点翻页，空页、重复页或整页已早于窗口即停止。Google News 来源按每天公开搜索覆盖 7 天，不再受原 URL 的 `when:1d` 限制。无公开历史入口的源只能取得其当前公开列表，不保证一周完整覆盖；不绕过付费墙或访问限制。单线程、每请求含跳转间隔 3–6 秒，403/429 停止该源并保留已取列表。

定时抓取只生成简报和入库，不自动推送；只有独立的简报推送计划到点才生成汇总快照并发送。手动推送仍由管理员明确操作。7 天全量可能显著增加耗时和模型费用。
