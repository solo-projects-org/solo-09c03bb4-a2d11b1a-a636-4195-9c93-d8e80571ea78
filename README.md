# 风电机组故障码类别发布台

运维数据员录入上报事件，维护「固件版本 + 故障码 → 类别」的修订映射，并按
「机组 + UTC 日」发布带统一类别的日档给分析组。同一故障码在不同固件版本下可以代表
不同故障，因此映射键永远是 `(firmware_version, fault_code)`，绝不只用故障码。

技术栈：FastAPI + Jinja2 页面 + SQLite（WAL），Docker Compose 一键启动。

## 启动

```bash
docker compose up --build      # http://localhost:8000
```

SQLite 文件在具名卷 `ops-data` 的 `/data/ops.db`，容器重启后数据保留。

本机不使用 Docker 时：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
FAULT_DB=./data/ops.db uvicorn app.main:app --reload
```

运行测试（需要 `pytest`、`httpx`）：

```bash
pip install pytest httpx
pytest
```

## 页面

| 路径 | 用途 |
| --- | --- |
| `/` | 概览：现行日档、按日状态、最近事件修订 |
| `/events` | 录入事件号、递增修订号、机组、固件版本、故障码、UTC 时刻 |
| `/mappings` | 维护固件版本 + 故障码的递增类别映射修订 |
| `/days/{unit}/{day}` | 单日：现行事件、缺映射清单、重算发布、历史撤销记录 |
| `/status` | JSON：现行日档 / 待重算日档 / 最近操作日志 |

给分析组的只读接口：

| 路径 | 说明 |
| --- | --- |
| `GET /api/days` | 当前可用的日档列表 |
| `GET /api/days/{unit}/{day}` | 现行日档内容；不可用时返回 `409` 和原因 |
| `GET /api/days/{unit}/{day}/export.csv` | 同内容 CSV 下载 |
| `GET /healthz` | 存活检查（Compose healthcheck 使用） |

## 规则与实现对应

所有状态都只存在 SQLite 中，进程内不留缓存；每个「读-校验-写」都在
`BEGIN IMMEDIATE` 事务里完成（`app/db.py:Store.write`），因此并发提交或进程重启
都不会把旧结果重新变成现行。

1. **修订号严格递增**：事件与映射都要求下一次修订号等于「现有最大修订 + 1」，
   跳号或重复提交被拒绝（`app/domain.py:add_event_revision` /
   `add_mapping_revision`）。
2. **事件只取最新修订**：`day_view` 用 `MAX(revision) GROUP BY event_no` 取现行修订；
   历史修订保留在 `events` 表里可追溯。
3. **UTC 时刻**：必须带时区且偏移为 0，落库统一为 `...Z`；UTC 日取时间戳前 10 位。
   跨日更正天然落到不同 `day` 桶。
4. **缺映射整日不可供数**：日视图中任一事件找不到 `(固件版本, 故障码)` 的有效映射时，
   页面列出缺映射事件并禁用发布按钮，`publish_day` 直接拒绝，`/api/days` 不出现该日。
5. **事件更正立即撤销新旧日档**：`add_event_revision` 在同一事务里撤销事件
   「原机组 + 原 UTC 日」与「新机组 + 新 UTC 日」的 `CURRENT` 日档，故改机组与改日期
   都会同时影响两侧。
6. **映射修订撤销受影响日档**：新映射修订会撤销所有「现行日档用到该固件版本+故障码」或
   「当前事件集含该固件版本+故障码」的日档。无关日档不受影响，继续可查。
7. **重算须以同一组现行修订发布完整日档**：页面把事件修订快照
   (`EVT-1@2,...`) 和完整发布依据 `recipe`（事件修订 + 映射修订）作为隐藏字段提交；
   任一与事务内实时数据不一致即拒绝发布。事务内重算即整日重建：先撤销旧日档，再写入
   新的 `CURRENT` 日档与全部明细，要么全部成功，要么全部回滚。
8. **失败/重启不能复活旧结果**：撤销与写入同事务；发布失败回滚后旧日档仍是
   `REVOKED`。启动时 `purge_stale_current` 会把「与当前数据不一致的 CURRENT 日档」
   降级（它只会降级，不会升级）。数据库层还有
   `CREATE UNIQUE INDEX ... WHERE status='CURRENT'` 作为每机组每日仅一份现行的兜底。
9. **审计**：`publishes` 只追加不删除，撤销保留时间与原因；`publish_items` 保留当时
   使用的类别与映射修订，可在 `/days/...` 页面查看历史日档；`operation_log` 记录操作。

## 已知限制

- 无身份认证与权限区分：假定部署在内网、由受信运维人员使用。
- 没有删除事件/映射的功能（更正靠新增修订实现），也没有分页；数据量按单站日档规模设计。
- 单进程 SQLite（WAL）写入串行化；`uvicorn` 以单 worker 运行。大规模并发需要换
  PostgreSQL 之类的存储。
