# News Admin

多来源新闻 → 结构化分析 → 事件聚类 → 带引用的中文摘要 → 排序 → Web 简报 + 多渠道推送。

大模型负责理解、归纳和判断；**它的每一个输出都经过 Schema 校验和事实校验**，不合格就带着错误重试，重试仍失败就降级，并在后台标记。

```
抓取 ─► 标准化 ─► 一级去重 ─► 逐篇分析(LLM) ─► 事件聚类(向量+LLM) ─► 跨批次比对 ─► 摘要(LLM)+事实校验 ─► 排序 ─► 发布/推送
```

设计原则、阈值和取舍见 [docs/DESIGN.md](docs/DESIGN.md)。

## 各环节怎么做

| 环节 | 做法 | 为什么 |
|---|---|---|
| 抓取 | 四种来源类型：RSS、JSON Feed、央视列表接口、网页列表页（只需一个文章链接正则）；标题/时间/正文用通用元数据与 trafilatura 抽取，失败退回列表摘要 | 旧版为每个站点手写上千行选择器和特例；v2 换站点只改配置 |
| 一级去重 | 规范化 URL 精确去重 + 字符 3-gram 包含度 ≥0.8 判为通稿转载 | 不花模型钱；短文本上比 SimHash 稳定 |
| 分类 | 地区（国内/港澳台/国际/全球）和主题（10 类）**两个独立维度**；置信度 <0.5 归「其他」 | 旧版把地区和主题混在一个字段，「中国芯片出口管制」只能二选一 |
| 同一事件 | 用 LLM 归一化后的「中文标题+事件指纹+关键事实」做向量；≥高阈值直接合并，模糊区间交给 LLM 按「主体+行为+时间地点」判定，48 小时窗口 | 中英文报道原文的向量相距很远；判定标准写死，评论文章不与事实报道合并 |
| 跨批次 | 与近 48 小时已发布事件比对：没有新报道 → 不再推送；有新报道 → 标「更新」 | 定时运行时不会反复推送同一条新闻 |
| 摘要 | 每句必须带来源编号；句中数字必须能在**所引用的那篇来源**中找到；禁止套话；来源说法冲突时单独列出 | 让摘要可追溯，减少编造 |
| 排序 | `影响力×2 + log2(1+来源数)×1.5 + 新鲜度 + 来源权重 − 评论类惩罚` | 可解释；来源数边际递减，避免转载多就排前面 |

### 结构化输出

- 所有模型输出由 Pydantic 定义（`news_admin/models.py`），自动转换为 OpenAI strict JSON Schema（内联 `$ref`、全部字段必填、禁止额外字段）
- 网关不支持 `json_schema` 时自动降级为 `json_object` + 本地校验
- 校验失败把 Pydantic 报错原文回传给模型重试，最多 2 次
- 以「模型 + Prompt 版本 + 输入」为键缓存结果，改 Prompt 必须提升版本号（`news_admin/prompts.py`）
- 每次调用记录阶段、Token、耗时、重试、是否命中缓存，后台按运行汇总

## 快速开始

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env        # 填写模型和向量服务
python -m news_admin run --no-push   # 跑一次，不推送
python -m news_admin show            # 终端查看最新简报
python -m news_admin serve           # http://127.0.0.1:5080
```

`.env`：

```ini
NEWS_LLM_BASE_URL=https://api.openai.com/v1   # 任意 OpenAI 兼容接口
NEWS_LLM_API_KEY=
NEWS_LLM_MODEL=gpt-5.6-luna
NEWS_EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
NEWS_EMBEDDING_API_KEY=                       # 不填则降级为字符哈希向量
NEWS_EMBEDDING_MODEL=BAAI/bge-m3
NEWS_ADMIN_PASSWORD=                          # 不填则首次启动生成到 data/admin_password.txt
NEWS_PUBLIC_BASE_URL=                         # 推送消息中的简报链接
```

## 默认来源

沿用 v1 的 8 家：路透社（buzzing.cc 中文 JSON Feed）、BBC、纽约时报、CNN、央视新闻（国内/国际两个接口）、新华社、人民网、RT、联合早报。
另有 BBC 中文、德国之声中文、法广中文、中新网、卫报、半岛电视台等 RSS 备选（`news_admin/sources.py` 的 `EXTRA_SOURCES`），可在后台添加。

## 后台

- `/briefs/<id>`：公开简报，句末上标可跳到对应来源
- `/admin`：手动运行、Cron 定时、每次运行的统计
- `/admin/runs/<id>`：各阶段数量、模型调用与 Token、来源健康、推送结果、事件打分
- `/admin/sources`：RSS 来源、权重、每次条数上限、连续失败次数
- `/admin/channels`：Telegram / 钉钉 / 飞书 / 企业微信 / 邮件；编辑时密钥留空表示不修改

## 评测

```bash
python -m news_admin export-eval <run_id> --output eval/data/to_label.jsonl  # 导出待标注
# 人工填写 gold_region / gold_topic / gold_event
python -m news_admin eval --data eval/data/to_label.jsonl --output eval/report.json
```

输出地区/主题准确率与混淆、聚类成对 Precision/Recall/F1、摘要首轮通过事实校验的比例，以及 Token 用量。用来对比模型、Prompt 版本和聚类阈值。

## 开发

```bash
python -m pytest
```

外部依赖（抓取、模型、向量）全部可注入，测试不访问网络。
