# CS2 饰品客观行情分析服务

在现有FastAPI服务上开发，保留原有接口和环境变量。**本次代码尚未部署生产；目前没有真实行情验证成功，目标悠悠排行榜和完整100分排名仍被数据授权缺口阻塞。** 详细证据见 [数据源核查](docs/SOURCES.md)。不提供交易执行或上涨概率。

## 当前实现

- 清单分析，最多30件，完整中文/英文名称精确核对，不模糊猜测版本。
- 独立截图识别适配器，OpenAI Responses图像输入；未配置时明确报错，不假装FastAPI能读图。
- CSQAQ文档接口：名称目录、单件详情、悠悠价格/挂单历史、存世量历史；统一1.1秒请求间隔、5分钟有界缓存、超时/业务错误/Token/IP拒绝/429冷却处理。HTTP 200不等于业务成功。
- 数据源、查询时间、单位和统计口径逐字段记录。未知时区、缺失、过期、倒挂、数组不对齐等明确提示。
- 六维固定评分25+25+20+15+10+5；关键维度缺失则total=null，不放大部分分数，不生成推荐排名。公式见 [评分说明](docs/SCORING.md)。
- SQLite后台任务、状态查询、8个待处理任务上限、请求限流、24小时结果可访问窗口；重启未完成任务明确标记interrupted。
- [ChatGPT Actions schema](docs/actions-openapi.json)及[一次性接入说明](docs/CHATGPT.md)。实现了接口，不代表用户账户已经连接。
- 外部定时调用入口 `python -m scripts.scheduled_analysis`；没有启用任何生产定时任务或创建新服务。

## 明确未完成

- CSQAQ真实Token/IP/出口网络验证；仓库开发环境未绑定密钥，连接工具不能读出Railway现有值。
- 悠悠官方授权目标榜：今日、价格>¥100、成交额降序、前30。接口返回503，不返回其他榜单。
- 悠悠真实成交价/量/额、逐档深度和核验后的实际手续费。CSQAQ `turnover_number` 只支持Steam，不能替代悠悠成交量。
- 真实截图识别精度测试、真实30件端到端验收、ChatGPT账户Actions配置。
- 完整评分被上述数据缺失阻塞，返回部分报告是预期行为；报告中测试数据从未作为真实行情提供。

## 接口

新接口需要X-Access-Key或Bearer访问口令，缺口令配置时保持关闭。

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/health`, `/api/health` | 公开存活检查，不验证上游 |
| POST | `/api/analyze/items` | 返回202与job_id，后台查询/分析 |
| POST | `/api/analyze/image?expected_count=30` | 上传图片原始二进制，返回202；PNG/JPEG/WEBP，8MiB以内 |
| POST | `/api/recognize/image?expected_count=30` | 仅识别并返回结构化清单；仍需要视觉API配置 |
| GET | `/api/jobs/{job_id}` | 返回queued/running/completed/failed/interrupted及完整JSON报告 |
| GET | `/api/analyze/top30` | 官方目标榜未授权，明确503 |
| GET | `/api/capabilities` | 配置状态与已知功能边界，不假称已验证 |
| GET | `/api/csqaq/diagnostic` | 认证后的真实上游诊断，只显示错误码或存在的字段名 |
| GET | `/api/actions-schema` | 公开Actions配置，无密钥或行情 |

旧 `/api/status`、`/api/score`、`/api/csqaq/{rank,supply,volume}`、`/docs` 保留；旧人工评分维持原参数/权重，并明确不参与自动报告。旧supply路径保留POST外观，内部改为官方文档要求的GET；volume原上游专栏已暂停更新，不能当作当前悠悠成交量使用。

清单请求示例（访问口令放HTTP头，不能放URL）：

```json
{
  "items": [{
    "name": "AK-47 | 红线 (久经沙场)",
    "market_hash_name": null,
    "wear": "久经沙场",
    "version": "normal",
    "pattern": null,
    "rank": 1,
    "screenshot_price": null,
    "confidence": 1,
    "needs_review": false
  }],
  "expected_count": 30,
  "origin": "item_list"
}
```

只有截图中显示的一部分就传这一部分，expected_count仍填30，报告会说明缺行数。version允许normal/stattrak/souvenir/unknown；unknown不查询和评分。特殊模板待核实，不与同名普通版本合并。items清单不证明来源是官方排行榜。

报告包含识别清单、匹配身份、逐字段行情、辅助数据、缺失/异常、指标、各维公式与分数、可评分完整排名和前5研究清单。没有可评分项则两个清单为空。

## 环境变量

| 变量 | 用途 |
|---|---|
| `ANALYZER_ACCESS_KEY` | 保留现有服务访问口令，新接口同时支持Bearer；不要换成行情Token |
| `CSQAQ_API_TOKEN` | 保留现有行情Token，需官方IP白名单 |
| `OPENAI_API_KEY`（可选） | 独立截图接口调用视觉模型，独立API计费；ChatGPT自身读图→items不需要它 |
| `VISION_MODEL`（可选） | 支持Responses图像输入和严格JSON Schema输出的模型ID；与上项一起配置才启用后端OCR |
| `ANALYZER_DB_PATH`（可选） | 默认/tmp/cs2-analyzer.sqlite3；临时存储，持久化需已有服务挂卷后使用卷路径 |
| `WEB_CONCURRENCY` | 必须为1；多副本/进程上线前需共享限流和分布式任务协调 |
| `ANALYZER_BASE_URL` | 仅外部定时调用脚本使用，HTTPS服务地址 |

密钥不提交Git，不输出日志。原有Railway密钥不删除、不覆盖。不要自动绑定开发机器IP覆盖生产白名单。

## 本地运行和测试

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

通过执行环境的秘密变量设置访问口令和行情Token，不要在公开终端命令中硬编码。没有Token也能运行健康检查、验证和测试；真实分析会明确报告缺授权。测试使用合成数据和模拟HTTP，覆盖手算结果、缺维度、版本误配、上游错误、截图任务、认证、缓存、限流和重启状态，不是行情实测。

## Railway发布（必须先获用户确认）

继续使用现有项目“CS2 饰品行情分析”及服务`cs2-skin-api`，不创建重复服务。代码准备在独立分支，不能提前推main触发生产自动部署。`railway.toml`和Procfile使用单worker，/health健康检查，不改变原密钥。获批后合并开发分支并检查GitHub源分支与构建日志，必要时只对该现有服务手动部署经批准的提交。

已核对：生产运行0089526，GitHub开发基线5b99ff4，不能仅凭关联仓库就认定自动部署正常；当前生产源配置没有显式branch，是否因自动部署设置/集成权限/排队/手动发布尚待进一步确认。本次没有擅自改动线上源配置。完整部署与回滚清单见 [发布说明](docs/DEPLOYMENT.md)。
