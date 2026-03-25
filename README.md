# News Admin

一个可迁移的本地新闻聚合控制面板，用来抓取多家新闻源、自动聚合、生成 Web 发布页，并按接收人投递到 Telegram / 邮箱 / 企业微信 / 钉钉 / 飞书。

## 快速开始

### 方式一：一键初始化（推荐）

```bash
bash scripts/bootstrap_local.sh
```

### 方式二：手动初始化

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python run.py init-db
python run.py serve --host 127.0.0.1 --port 5080
```

默认后台地址：

- `http://127.0.0.1:5080`
- 最新发布页：`http://127.0.0.1:5080/published/latest`

## 功能

- 管理 8 个预置新闻来源，可新增和修改来源规则
- 管理推送平台：Telegram 已可用，钉钉/飞书已接入统一适配层
- 管理多个接收人，可配置多个 Telegram `chat_id`
- 手动执行聚合任务
- 用 Cron 表达式配置定时任务
- 生成统一发布页，并提供固定入口 `/published/latest`
- 记录每次运行、每个来源命中情况、每条聚合结果和每次投递状态
- 支持导出/导入 `data/`，便于迁移到新机器

## 运行机制

一次完整流程大致如下：

1. 从预置来源抓取新闻列表和正文片段
2. 清洗文本、去掉英文泄漏/低价值碎片/无意义标题
3. 按主题聚合成 `story_groups`
4. 渲染 Web 发布页
5. 生成邮件/推送摘要并发送给已启用接收人
6. 记录运行、来源命中、聚合结果和投递状态

## 重要说明

- 系统目标是“尽可能全面 + 不静默丢失 + 可见来源健康度”，不是虚假承诺全网 100% 完整抓取。
- 对于动态站点、地区差异、反爬限制，系统会保留来源结果和错误信息，便于后续调优规则。

## 目录

- `news_admin/`：后端代码
- `templates/`：管理后台模板
- `static/`：样式资源
- `data/`：SQLite 数据库、抓取 artifacts、发布页、导出包
- `vendor/`：项目本地 Python 依赖

## GitHub 发布说明

- GitHub 仓库不包含真实运行数据、密钥、管理员引导文件、日志和导出包。
- `data/` 目录只保留脱敏占位结构，首次启动后会自动重建实际运行文件。
- 仓库中的 `.env.example` 只提供环境变量示例，不包含真实凭据。
- 如果你不想使用本地 `vendor/` 依赖目录，可直接执行：

```bash
python3 -m pip install -r requirements.txt
```

## 安装依赖

项目默认支持两种方式：

1. 使用系统 Python 已安装依赖
2. 使用项目内 `vendor/` 目录加载依赖

如果需要重新安装到项目目录：

```bash
python3 -m pip install --target ./vendor -r requirements.txt
```

## 环境变量

项目目前只依赖两个可选环境变量：

- `NEWS_ADMIN_PUBLIC_BASE_URL`
  - 用途：生成邮件/推送中的完整 Web 阅读链接
  - 示例：`http://127.0.0.1:5080` 或 `https://your-domain.com`
- `NEWS_ADMIN_SECRET_KEY`
  - 用途：显式指定应用密钥
  - 不设置时，会自动生成到 `data/secret_key.txt`
- `NEWS_ADMIN_ADMIN_USERNAME`
  - 用途：显式指定管理员用户名
- `NEWS_ADMIN_ADMIN_PASSWORD`
  - 用途：显式指定管理员明文密码（首次启动就可直接使用）
- `NEWS_ADMIN_ADMIN_PASSWORD_HASH`
  - 用途：如果你不想放明文密码，可直接提供密码哈希
  - 与 `NEWS_ADMIN_ADMIN_PASSWORD` 二选一即可

可参考仓库中的 `.env.example`。

## 初始化

```bash
python3 run.py init-db
```

它会完成：

- 创建 `data/` 目录结构
- 初始化 SQLite 数据库
- 写入预置新闻来源和推送平台骨架数据

## 首次登录

项目的管理员认证有两种来源：

1. 你在环境变量里显式设置了管理员账号/密码
2. 你没有设置，系统会在首次访问时自动生成：
   - `data/admin_auth.json`
   - `data/admin_bootstrap.txt`

如果你没有在 `.env` 中设置管理员密码，那么首次登录密码可以从下面这个文件读取：

```bash
cat data/admin_bootstrap.txt
```

首次登录后，建议立即在后台修改密码。修改完成后，bootstrap 文件会被移除。

## 启动控制面板

```bash
python3 run.py serve --host 127.0.0.1 --port 5080
```

如果你已经配置了 `NEWS_ADMIN_PUBLIC_BASE_URL`，推送中的 “前往Web阅读” 会自动变成完整 URL。

## 手动执行一次

```bash
python3 run.py run-now
```

适合在配置完来源、平台和接收人之后做首次验证。

## 导出迁移包

```bash
python3 run.py export --output ./data/exports/news-admin-export.zip
```

## 导入迁移包

```bash
python3 run.py import --input ./data/exports/news-admin-export.zip
```

## 已实现的迁移策略

导出包包含整个 `data/` 目录：

- `news_admin.db`
- `artifacts/`
- `publications/`
- `exports/` 中除当前输出文件外的内容

导入时会先把原有 `data/` 备份到 `data-backup-before-import/`，再解压导入。

## 推送配置建议

- Telegram：在平台里配置 bot token，在接收人里填 `chat_id`
- 钉钉：在接收人里配置 webhook 和可选 secret
- 飞书：在接收人里配置 webhook 和可选 secret
- 邮箱：在平台里配置 SMTP 主机、端口、用户名、密码、发件人地址

## 首次启动后的建议步骤

1. 登录后台并检查默认来源是否需要启用/调整
2. 配置至少一个推送平台
3. 添加接收人并启用
4. 手动执行一次 `run-now` 或在后台触发一次运行
5. 确认 `/published/latest`、后台运行记录和推送内容都正常

## 开发与排障

常用命令：

```bash
python3 run.py init-db
python3 run.py serve --host 127.0.0.1 --port 5080
python3 run.py run-now
python3 run.py cleanup --days 7
python3 run.py export --output ./data/exports/news-admin-export.zip
```

排障时重点关注：

- `data/live-server.log`
- 后台中的 runs / publications / activity 页面
- `data/README.md` 中说明的运行期文件

## 后续增强方向

- 更细的聚合打分
- 来源健康告警
- Web 端导出/导入按钮
- 钉钉/飞书应用级消息推送
