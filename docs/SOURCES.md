# 数据源核查记录（2026-11-16 UTC）

## 已真实验证

- GitHub 仓库 `DA561/cs2-skin-analyzer`，开发基线 `5b99ff4`。原仓库只有 main.py、README.md、requirements.txt。
- Railway 项目 `CS2 饰品行情分析`，现有服务 `cs2-skin-api`，production 最近部署状态 SUCCESS，运行提交 `0089526`；未更改生产配置或部署。
- 公网 `/health` 实际返回 `{"status":"ok"}`。健康检查不代表行情接口可用。
- Railway 配置仍包含 `ANALYZER_ACCESS_KEY`、`CSQAQ_API_TOKEN`；OAuth 连接未提供密钥值，开发环境也没有绑定 Token。
- 成功读取 CSQAQ 官方公开文档及 OpenAPI 描述；这只能验证字段定义，不能验证当前账户能取得真实字段。
- 从开发环境无 Token 请求 `GET /info/good?id=7310` 遇到上游连接超时；没有取得真实行情，不标记为验证成功。

**当前真实行情验证数量：0 件。所有自动化测试行情均为明确的合成数据。**

## 字段矩阵

| 目标数据 | 官方文档/接入依据 | 实测状态 | 系统行为 |
|---|---|---|---|
| 悠悠最低在售价（CNY） | CSQAQ `info/good` → `yyyp_sell_price` | 需要 Token、IP 白名单及网络验证 | 按原字段采集，保留来源时间 |
| 悠悠最高求购价（CNY） | `yyyp_buy_price` | 同上 | 报价倒挂/0值拒绝评分 |
| 悠悠在售/求购数量（件） | `yyyp_sell_num` / `yyyp_buy_num` | 同上 | 官方口径为该指标最大值，不当作逐价位深度 |
| 悠悠历史报价/挂单数 | `info/chart`, platform=2, keys=sell_price/sell_num/buy_num | 同上 | 数组不对齐、缺日不插值，不默默截断 |
| 存世量（件） | `statistic` / GET `info/good/statistic` | 同上；覆盖范围待确认 | 不声称全市场完整覆盖 |
| 实际成交价格 | 未确认悠悠对应字段 | 暂不可用 | 不使用通用 turnover_avg_price 冒充 |
| 悠悠24h/7d/30d成交量、成交额 | 没有已验证授权源 | 暂不可用 | 不使用 Steam 成交量、热度或总市值替代 |
| 悠悠逐档买卖盘深度 | 没有已验证授权源 | 暂不可用 | 挂单总数不替代深度 |
| 悠悠实际手续费 | 没有已核验官方费率及适用条件 | 暂不可用 | 不假定0手续费，净损益/对应评分为空 |
| 目标排行榜：今日、>¥100、成交额降序、30件 | 未找到可确认的悠悠官方授权公开接口 | 需要官方授权方案 | `/api/analyze/top30` 明确503，不替换榜单 |
| 截图OCR | OpenAI Responses图像输入，需OPENAI_API_KEY和VISION_MODEL | 未配置，未实图测试 | 未配置返回503；成本按模型图像/文本token收费 |

特别注意：CSQAQ 图表接口明确限制 `turnover_number` 只能用于 `platform=3`（Steam）；`platform=2` 才是悠悠有品。实时成交专栏现标记为暂停更新。不能据此产出悠悠真实成交活跃度分数。

## 授权后可执行的验证

无需修改代码：在可信执行环境通过秘密变量注入现有 Token，然后运行 `python -m scripts.probe_sources`；也可以部署经批准的版本后带现有访问口令调用 `/api/csqaq/diagnostic`。诊断仅返回字段名、时间及错误码。不要把 Token 发到聊天、提交到仓库或写入日志。

白名单绑定接口会改变账户授权状态，因此代码不会自动调用它。应先确认生产实际出口和现有白名单；不能以开发机器 IP 覆盖生产绑定。超时、Token/IP被拒绝和业务错误分别显示，业务 HTTP 200 不自动认定成功。

## 官方参考

- [接入指南（1次/秒、Token和IP绑定）](https://docs.csqaq.com/doc-4588854)
- [数据口径](https://docs.csqaq.com/doc-4589773)
- [ID信息及中英文匹配](https://docs.csqaq.com/api-187131777)
- [单件详情及悠悠字段](https://docs.csqaq.com/api-187131780)
- [图表平台/字段限制](https://docs.csqaq.com/api-187131781)
- [存世量历史](https://docs.csqaq.com/api-366480669)
- [排行榜参数](https://docs.csqaq.com/doc-4619235)：未确认与目标App榜单相同
- [悠悠有品官网](https://www.youpin898.com/)：公开页没有提供可确认的目标榜单接口；未使用第三方逆向爬虫、登录绕过或验证码绕过

CSQAQ 公开文档说明数据限参考学习、禁止商业用途；商业/高频需求需取得合作授权。本项目没有自动交易功能。
