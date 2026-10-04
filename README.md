# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本、审计链和请求幂等台账，`src/service.py` 编排权限、依据提升和凭据链，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。写请求可带 `X-Request-Id` 做请求编号。

接口：

- `GET /health`、`GET /api/state`
- `POST /api/items` 上报干扰事件
- `POST /api/items/<id>/sources` 提交观测来源（可带 `bandwidth_mhz`、`frequency_mhz`）
- `POST /api/items/<id>/actions` 执行动作：`assess`、`locate`、`authorize_suspend`、`suspend`、`coordinate`、`resolve`、`correct_measurement`、`cancel`
- `GET /api/items/<id>` 事件详情（含凭据链）、`GET /api/items/<id>/chain` 仅凭据链、`GET /api/items/<id>/audit` 审计链

## 凭据链规则

干扰事件 → 观测来源 → 协调协议 → 停用授权/结案，每一环都固化当时的来源快照（`basis.source_id`）。

1. **当前依据取观测更晚的来源，晚到的只进历史**：`observed_at` 严格晚于当前依据的来源被提升为新依据，更新强度/带宽/频率、重算评估、进入 `basis_history` 并产生 `basis_promoted` 审计事件；更早的来源仅写入 `sources`，不改参数和版本。
2. **协调后改动的连锁作废**：处于 `coordinating` 时，若新依据改动了**强度或带宽**，未执行的停用授权（`authorize_suspend` 签发）进入 `voided_authorizations`（原因 `basis_changed_after_coordination`，编号不可再执行），已发出的协调协议进入 `coordination_history`，待结案回退到 `reopened`；**已执行的停用和协议记录保留原依据快照不变**。仅改频率不触发回退。
3. **旧事件按未确认处理**：没有任何来源记录的事件 `basis_confirmed=false`，提交定位/授权/停用/协调/结案返回 409 `basis_unconfirmed` 并带原因；补录来源后即可提交。页面和错误体都展示失效原因。

## 请求可靠性

- **同一请求编号重复到达只入账一次**：台账 `request_log` 与业务写入在同一个 SQLite 事务提交（exactly-once）；重放返回首次结果并标 `idempotent_replay`，同编号不同内容返回 409。
- **两个辖区同时提交结论，先写入的生效**：后到请求在版本检查处得到 409 `version_conflict`，错误体 `details` 带 `current_version`、`current_status`、`current_basis` 和逐项 `conflicting_fields`，据此拿最新版本重做。
- **写入失败按该编号重试；服务重启从断点继续**：崩溃残留的 `processing` 台账在初始化时标记为可接管，重试不重复累计业务效果。

页面 `/` 可直接提交完整流程，展示事件列表、当前/历史依据、授权状态、作废原因和冲突项。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
