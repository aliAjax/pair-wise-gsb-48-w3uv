# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性和公司行动调整和冲突检查。
- `src/repository.py`：SQLite建表、事务、旧数据幂等升级和查询。
- `src/service.py`：用例编排、权限检查、乐观并发、公司行动版本与净额批次原子确认。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `GET /api/corporate-actions?instrument=..&type=split`：公司行动版本列表。
- `POST /api/corporate-actions`：交收前修正公司行动比例（发布新版本），角色`corporate_actions`，请求体为`{"data":{"instrument":"ACME","corporate_action":"split","action_ratio":3.0,"remark":"..."}}`。
- `POST /api/net-batches`：按账户/币种/交收日对已复核、未冻结指令生成净额批次（逐笔快照公司行动版本与净收付数量），角色`settlement_officer`，请求体为`{"data":{"batch_no":"NB-001","account":"ACC-1","currency":"CNY","settlement_day":2}}`。
- `POST /api/net-batches/{batch_no}/confirm`：确认批次。逐笔冻结采用的公司行动版本与净收付数量并交收；同组未入批指令按最新公司行动版本重算。
  - 两位主管同时确认只有一位成功；败方收到`409`，`details`中包含`collisions`（被胜出批次冻结的指令及胜出批次号）、`stale`（快照过期的指令）和`leftovers`（按新版本重算的未入批指令）。
  - 写入失败整体回滚，不留半张批次；同一批次号重提会继续核对，已确认批次幂等返回（响应带`replayed:true`）。
- `GET /api/net-batches`：批次列表，可带`status`参数。
- `GET /api/net-batches/{batch_no}`：批次详情，含逐笔冻结行与批次审计。

指令创建时锁定当时最新公司行动版本；用旧比例提交会收到`409`并返回最新版本号。首个指令自动登记`v1`。旧库启动时自动升级：按每条指令原`action_ratio`补一版公司行动版本（幂等，重复执行不产生重复版本）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
