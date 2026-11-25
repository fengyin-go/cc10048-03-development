# 多维指标分析与实验评估平台

面向业务指标口径管理、多维分析查询、AB实验评估、分群圈选和数据服务发布的分析应用平台

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

健康检查：`curl http://127.0.0.1:8012/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5176/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8012`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标口径 | `metric_def` | 指标定义 | 指标标识、指标名称、计算公式 |
| 多维查询 | `dim_query` | 查询方案 | 方案标识、查询指标、维度行 |
| 实验管理 | `experiment` | 实验方案 | 方案标识、实验名称、对照组 |
| 显著性检验 | `significance` | 检验任务 | 任务标识、关联实验、检验方法 |
| 人群圈选 | `cohort` | 人群分群 | 分群标识、分群名称、圈选条件 |
| 数据服务 | `data_service` | 服务接口 | 接口标识、关联指标、接口路径 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则标识、关联指标、检测算法 |
| 分析图表 | `analysis_chart` | 图表配置 | 图表标识、数据来源、图表类型 |
| 预聚合任务 | `aggregate_job` | 聚合任务 | 任务标识、目标指标、聚合维度 |
| 下钻分析 | `drill_down` | 下钻路径 | 路径标识、起始指标、下钻维度 |
| 数据导出 | `data_export` | 导出任务 | 任务标识、查询方案、导出格式 |
| 数据时效 | `data_freshness` | 时效检查 | 检查标识、数据来源、期望时效 |
| 查询缓存 | `query_cache` | 缓存策略 | 策略标识、关联查询、缓存键 |
| 分群对比 | `segment_compare` | 对比方案 | 方案标识、分群A、分群B |
| 指标校准 | `metric_calibrate` | 校准记录 | 记录标识、校准指标、原口径值 |
| 查询日志 | `query_log` | 查询记录 | 记录标识、查询内容、查询用户 |
| 数据采样 | `data_sample` | 采样配置 | 配置标识、数据来源、采样方法 |
| 报告调度 | `report_schedule` | 报告任务 | 任务标识、报告内容、接收人 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 生效口径批次（华东 / 华南双版本场景）

指标定义可以登记口径版本，但下游还需要一份“已算好、可查阅”的生效结果。
新增的**生效口径批次**由指标定义版本与地区维度数据聚合而成，并关联实际聚合输入，
批次状态只有：`准备(preparing) → 可读(readable) → 已归档(archived)`。

核心不变量（由 `backend/tests/test_effective_batch_narrative.py` 与
`backend/verify_effective_batch.py` 守护）：

1. **同批一致**：地区结果只存在批次内部，查询当前口径时整批读取，
   每行都带同一个 `batch_id`，结构上不可能把“新版华东 22”和“旧版华南 40”拼接。
2. **齐备才生效**：只有 `processed_regions` 覆盖全部 `region_scope`
   （`pending_regions` 为空）时，批次才能由“准备”确认为“可读”，
   并在首次确认时写入不可变的实际发布时点 `published_at`。
3. **中断可续算**：中断后 `progress()` 辨认已处理 / 待做范围；
   `resume()` 只补算 `pending_regions`，已处理地区跳过且发布时点不变。
4. **切换原子化**：`switch` 把新可读批次置为当前，同时把原当前批次归档；
   切换前查询仍旧批，切换后所有地区同属新批。
5. **旧报告可复算**：报告固定 `pinned_batch_id` 与 `input_scope`，
   口径切换、旧批归档后仍按原批次与原输入范围复算（华东 20 / 华南 40 / 合计 60）。

内置种子场景 `EffectiveBatchService.seed_scenario()`：

- 旧口径 `EB-V1-20261125`：华东 20、华南 40，可读且当前生效；
- 旧报告 `R-OLD` 锚定该批次；
- 新口径 `EB-V2-20261125`：只算出华东 22 后中断，华南待做，状态“准备”。

### 后端入口

- 领域服务：`app/services/effective_batch.py`
  （`EffectiveBatchService` / 共享单例 `get_service()`；
  另有 `caliber_batch`、`effective_caliber_batch`、`metric_batch` 等命名别名模块）。
- HTTP 路由：`app/routers/effective_batch.py`，主前缀 `/api/effective-batches`
  （同时兼容 `/api/effective_batch(s)`、`/api/caliber-batches`、
  `/api/effective-caliber-batches`、`/api/metric-batches` 及下划线/单数变体）。

常用接口：

```text
GET  /api/effective-batches/demo/run     # 一键跑完 中断→恢复→切换→复算 并给核对结论
POST /api/effective-batches/scenario     # 重置为华东/华南中断种子场景
GET  /api/effective-batches/current      # 当前生效批次（含完整地区结果）
GET  /api/metric-query?metric_id=M-REGION
GET  /api/effective-batches/{id}/progress
POST /api/effective-batches/{id}/region  # 计算单个地区
POST /api/effective-batches/{id}/interrupt
POST /api/effective-batches/{id}/resume  # 只补 pending；齐备后自动转可读
POST /api/effective-batches/{id}/confirm
POST /api/effective-batches/{id}/switch
POST /api/effective-batches/{id}/archive
POST /api/effective-batches/{id}/actions # {"action":"resume|switch|..."} 脚手架风格
POST /api/reports/R-OLD/recompute        # 按报告锚定的原批次复算
POST /api/domain/report_schedule/1/recompute
```

进程重启核对发布时点：设置 `EFFECTIVE_BATCH_STATE_PATH=/path/state.json`
后服务会把批次/报告/事件落盘，`EffectiveBatchService.recover(path)`
或直接重启 uvicorn 即可恢复现场；`./run.sh` 默认写入
`backend/.data/effective_batch_state.json`（该目录不入库）。

自检：

```bash
cd backend && .venv/bin/python verify_effective_batch.py
```

前端侧边栏新增“生效口径批次”页（路由 `/effective-batches`），
可在页面上依次执行重置、恢复、切换与旧报告复算并查看每批来源。

领域：数据工程
子方向：数据分析与数据应用
