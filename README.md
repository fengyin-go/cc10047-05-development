# 实时流数据湖构建与治理平台

面向实时流数据接入、Delta湖表构建、流批一体转换、分区合并和数据治理的流式数据湖平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8011/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5175/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8011`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流数据源 | `stream_source` | 流数据源 | 来源标识、接入协议、主题名称 |
| 湖表定义 | `lake_table` | 湖表定义 | 表标识、表名称、主键列 |
| 流式摄入 | `stream_ingest` | 摄入管道 | 管道标识、来源流、目标湖表 |
| 提交版本 | `delta_commit` | 版本提交 | 提交标识、目标湖表、提交类型 |
| 分区合并 | `partition_merge` | 合并任务 | 任务标识、目标湖表、源分区 |
| 流Schema演进 | `stream_schema` | Schema变更 | 变更标识、目标湖表、变更类型 |
| 快照管理 | `snapshot_mgr` | 表快照 | 快照标识、目标湖表、快照时间 |
| 流质量校验 | `stream_quality` | 质量检查 | 检查标识、目标管道、校验字段 |
| 历史清理 | `vacuum_job` | 清理作业 | 作业标识、目标湖表、保留版本 |
| 批流合并 | `batch_merge` | 合并管道 | 管道标识、流来源、批来源 |
| 湖表血缘 | `lake_lineage` | 血缘链路 | 链路标识、上游表、下游表 |
| 保留策略 | `retention_policy` | 保留规则 | 规则标识、目标湖表、保留天数 |
| 压缩任务 | `compaction_task` | 压缩任务 | 任务标识、目标湖表、压缩算法 |
| 数据回填 | `backfill_pipeline` | 回填任务 | 任务标识、目标湖表、回填范围 |
| 流处理指标 | `stream_metric` | 处理指标 | 指标标识、关联管道、指标类型 |
| 格式迁移 | `iceberg_migrate` | 迁移计划 | 计划标识、源格式、目标格式 |
| 表目录同步 | `table_catalog` | 同步记录 | 记录标识、来源Catalog、目标Catalog |
| 湖仓审计 | `lake_audit` | 审计报告 | 报告标识、审计范围、审计时间 |

## 批量迁移表名与表名映射

`table_catalog` 与 `lake_lineage` 页面底部共享“批量迁移表名”面板，后端事实层在
`app/table_registry.py`（表身份与名称绑定）和 `app/name_mapping.py`（改名批次、
映射记录、复核待办）。

- 每张湖表有稳定的 `table_identity`，改名只切换可读名称，**身份随原表保留**。
- 一次提交多条改名形成一个批次和逐项映射记录，映射记录可追溯。
- **整组只生效一次**：同一批次重复提交只补做未完成项，已应用的改名不重复执行。
- 某一项失败时，其他已成功的名称仍按原身份保留。
- `B→C` 完成而 `A→B` 未完成时，名称 `B` 按策略处理：
  - `vacant`：暂空，`B` 解析为 `vacant`；
  - `compat`：保留原 `B` 的临时只读兼容名。
  两种状态都至多解析到一张表，且任何可读名称只对应一张表。
- 名称绑定带生效区间，血缘解析可用 `as_of` 查历史时点，旧表名仍能找到真实来源。
- 每条表名映射的处置结论同步写入“字段血缘复核待办”。

接口：

```text
GET  /api/table-catalog/renames                      改名批次总览
POST /api/table-catalog/renames                      提交改名批次
GET  /api/table-catalog/renames/groups               批次列表
POST /api/table-catalog/renames/groups/{id}/apply    重试补齐未完成项
GET  /api/table-catalog/renames/mappings             表名映射记录
GET  /api/table-catalog/lineage-review-todos         字段血缘复核待办
POST /api/table-catalog/lineage-review-todos/{id}/review
GET  /api/table-catalog/tables                       湖表身份列表
POST /api/table-catalog/tables                       登记湖表与字段转换
GET  /api/lake-lineage/resolve?name=B&as_of=...      表名解析证据（含历史时点）
GET  /api/lake-lineage/name-history?name=B           名称绑定历史
```

演示数据预置两张湖表 `A`、`B`，各含 `amount` 列，开箱即可走通
“先只让 `B→C` 成功，再补齐 `A→B`”的完整流程。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
