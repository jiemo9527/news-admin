# News Admin

一个可迁移的本地新闻聚合控制面板。

## 功能

- 管理 8 个预置新闻来源，可新增和修改来源规则
- 管理推送平台：Telegram 已可用，钉钉/飞书已接入统一适配层
- 管理多个接收人，可配置多个 Telegram `chat_id`
- 手动执行聚合任务
- 用 Cron 表达式配置定时任务
- 生成统一发布页，并提供固定入口 `/published/latest`
- 记录每次运行、每个来源命中情况、每条聚合结果和每次投递状态
- 支持导出/导入 `data/`，便于迁移到新机器

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

## 初始化

```bash
python3 run.py init-db
```

## 启动控制面板

```bash
python3 run.py serve --host 127.0.0.1 --port 5080
```

## 手动执行一次

```bash
python3 run.py run-now
```

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

## 后续增强方向

- 更细的聚合打分
- 来源健康告警
- Web 端导出/导入按钮
- 钉钉/飞书应用级消息推送
