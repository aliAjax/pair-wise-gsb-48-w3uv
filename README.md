# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动（启动时自动执行旧数据升级）。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额折算、公司行动版本规则和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询（批次与明细单事务写入）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、净额批次、并发确认和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表，并对旧数据执行升级：按指令原始公司行动参数为每个标的补登版本1（幂等，可重复执行）。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data`可带`account`（缺省`DEFAULT`）。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 公司行动版本

- `POST /api/corporate-actions`：登记公司行动新版本（交收前比例修正），角色`corporate_actions`。请求体`{"instrument":"ACME","action_type":"split","ratio":3.0,"note":"..."}`；参数与最新版本一致时幂等返回。响应中`unbatched`为按新版本重算的未入批指令。
- `GET /api/corporate-actions?instrument=ACME`：版本列表。

### 净额批次

- `POST /api/batches`：按账户、币种、交收日生成净额批次，角色`settlement_officer`/`settlement_supervisor`。请求体`{"batch_no":"BN-1","account":"ACC-1","currency":"CNY","settlement_day":2}`。批次与全部明细单事务写入，不留半张批次；同一`batch_no`重提幂等返回原批次（HTTP 200）并附继续核对结果`verification`。
- `GET /api/batches?state=open`、`GET /api/batches/{id}`：批次查询（含明细与核对结果）。
- `POST /api/batches/{id}/actions/confirm`：主管确认（`settlement_supervisor`），请求体`{"expected_version":1}`。确认即冻结每笔指令采用的公司行动版本与净收付数量；两人同时确认仅一人成功，另一人收到409及`details.unbatched`未入批指令清单。
- `POST /api/batches/{id}/actions/cancel`：取消未确认批次并释放指令，请求体`{"expected_version":1,"reason":"..."}`。
- `GET /api/instructions/unbatched?account=&currency=&settlement_day=`：未入批指令，按最新公司行动版本重算。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。角色：`trader`、`corporate_actions`、`settlement_officer`、`settlement_supervisor`、`admin`。

## 行为约定

- 公司行动比例以`ca_versions`版本表为准：未入批指令始终按最新版本重算，已确认批次的指令以冻结的版本与净收付数量为准，两者不再混用。
- 批次内净收付数量带符号：正为收（证券/资金），负为付；`totals`按标的汇总净证券数量，`net_cash`为批次净现金。
- 已入批（未取消）指令禁止逐笔`settle`/`fail`/`reverse`，须通过批次处理；取消批次后指令释放，可按新版本重新净额。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、净额批次分组、确认冻结与未入批重算、并发确认唯一成功、写入失败重提不留半张批次、旧数据升级补登。
