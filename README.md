# CS2 饰品分析服务（第一版）

这是一个最小可部署的 FastAPI 后端。**当前没有自动交易功能，也没有接入悠悠有品实时销量。**

## 接口

- `GET /health`：服务健康检查
- `GET /api/status`：仅显示密钥是否配置，不返回密钥
- `POST /api/score`：六维评分（25+20+20+20+10+5）
- `POST /api/csqaq/rank`：CSQAQ 排行榜
- `POST /api/csqaq/supply`：CSQAQ 存世量走势
- `POST /api/csqaq/volume`：CSQAQ 成交量详情
- `GET /docs`：API 交互文档

CSQAQ 代理请求示例：

```http
POST /api/csqaq/rank
X-Access-Key: <在 Railway 配置的 ANALYZER_ACCESS_KEY>
Content-Type: application/json

{"payload": {}}
```

`payload` 必须根据 CSQAQ 官方接口文档填写，不能保证空对象有效。接口名称与请求字段请在上线前核对官方文档。

## Railway 部署

1. 将本文件夹中的文件上传到 GitHub 仓库根目录（不要只上传 ZIP）。
2. Railway 项目连接该仓库的 `main` 分支。
3. 在 Railway 服务 Variables 中设置 `ANALYZER_ACCESS_KEY`（新生成的随机强口令）和 `CSQAQ_API_TOKEN`（重新签发的 Token）。**不要把密钥发到聊天里或提交到 GitHub。**
4. 确认 CSQAQ 的 IP 白名单及使用许可；如需固定出口 IP，应在 Railway 配置相应网络能力。
5. 访问 `/health` 检查运行状态。

安全说明：CSQAQ 代理必须提供 `X-Access-Key` 才能调用，未设置访问口令时代理保持关闭。评分接口仅接受人工输入，不产生投资建议。生产使用前仍需完善数据校验、持久化、监控、手续费与滑点计算、共享限速等。
