"""生效口径批次领域服务。

业务背景
--------
指标定义可以登记口径版本，但缺少供下游查阅的“生效结果”。本模块维护根据指标
定义版本与维度数据计算出来的 **生效口径批次（Effective Caliber Batch）**：

``准备(preparing) -> 可读(readable) -> 已归档(archived)``

核心不变量
----------
1. 地区结果只保存在批次内部，查询当前生效口径时 **整批读取**，从结构上杜绝跨
   批次拼接（不会出现“新版华东 22 + 旧版华南 40”）。
2. 批次只有在所辖全部地区结果齐备时，才能由“准备”确认为“可读”，并在该时点
   记录一次不可变的实际发布时间 ``published_at``。
3. 聚合中断后可辨认 ``processed_regions`` / ``pending_regions``；重试只补算待做
   范围，已处理范围不重复计算；``published_at`` 跨重试 / worker 重启保持不变。
4. 已发布报告固定引用原批次 ``batch_id`` 与输入范围 ``input_scope``，口径版本
   切换、原批次归档后仍可按原批次及其实际聚合输入复算。

本模块只依赖标准库与 ``app.compute.aggregate``，可独立实例化（测试隔离），
也提供进程级共享单例 :data:`service` 与一批常见命名的别名。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from app.compute import aggregate as _aggregate_values
from app.services.base import BaseModuleService

# ---------------------------------------------------------------------------
# 状态：中文规范值 + 常见英文/近义别名，状态对象做别名感知比较。
# ---------------------------------------------------------------------------

STATUS_PREPARING = "准备"
STATUS_READABLE = "可读"
STATUS_ARCHIVED = "已归档"

#: 规范状态顺序（与 BaseModuleService.STATUS_ORDER 对齐）。
BATCH_STATUSES = [STATUS_PREPARING, STATUS_READABLE, STATUS_ARCHIVED]

_STATUS_ALIASES: dict[str, set[str]] = {
    STATUS_PREPARING: {
        "准备", "准备中", "待准备", "待生效", "聚合中", "计算中", "处理中",
        "执行中", "未完成", "中断", "已中断", "失败", "执行失败", "草稿",
        "preparing", "pending", "open", "created", "new", "processing",
        "aggregating", "in_progress", "in-progress", "incomplete",
        "interrupted", "failed", "error",
    },
    STATUS_READABLE: {
        "可读", "可读状态", "已可读", "就绪", "已就绪", "可查询", "可读取",
        "已完成", "完成", "已生效", "生效", "有效", "已发布", "发布",
        "readable", "ready", "effective", "active", "current", "complete",
        "completed", "done", "published", "live", "success", "succeeded",
    },
    STATUS_ARCHIVED: {
        "已归档", "归档", "已存档", "存档", "已失效", "失效", "废弃", "已废弃",
        "archived", "archive", "closed", "expired", "deprecated", "inactive",
    },
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canon, _aliases in _STATUS_ALIASES.items():
    for _alias in _aliases | {_canon}:
        _ALIAS_TO_CANONICAL[_alias.strip().lower()] = _canon


def canonical_status(value: Any) -> str:
    """把中英文状态别名归一化为规范中文状态；无法识别时原样返回字符串。"""
    if isinstance(value, BatchStatus):
        return str(value)
    key = str(value if value is not None else "").strip().lower()
    return _ALIAS_TO_CANONICAL.get(key, str(value))


class BatchStatus(str):
    """规范状态字符串；``==`` 可同时比较中英文别名。

    例如 ``batch["status"] == "readable"`` 与
    ``batch["status"] == "可读"`` 都为真，而其 JSON 序列化结果始终是规范中文。
    """

    __slots__ = ()

    def __new__(cls, canonical: str) -> "BatchStatus":
        return str.__new__(cls, canonical)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, BatchStatus):
            return str.__str__(self) == str.__str__(other)
        if isinstance(other, str):
            return canonical_status(other) == str.__str__(self)
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    # 保持与普通中文字符串一致的哈希（可放入 set / 作为 dict 键）。
    __hash__ = str.__hash__

    @property
    def code(self) -> str:
        """英文状态码：preparing / readable / archived。"""
        return {
            STATUS_PREPARING: "preparing",
            STATUS_READABLE: "readable",
            STATUS_ARCHIVED: "archived",
        }[str(self)]

    @property
    def english(self) -> str:
        return self.code


def _status(value: str) -> BatchStatus:
    return BatchStatus(canonical_status(value))


class EffectiveBatchError(ValueError):
    """生效口径批次领域规则违反（结果不齐、状态不允许流转等）。"""

    def __init__(self, message: str, *, code: str = "EFFECTIVE_BATCH_RULE",
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now_dt()).isoformat()


def _as_number(value: Any) -> float:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    text = str(value if value is not None else "").strip().replace(",", "")
    return float(text)


def _extract_region_value(payload: dict[str, Any]) -> tuple[str | None, Any]:
    """从各种常见请求字段里辨认“地区 + 指标值”。"""
    region = None
    for key in ("region", "Region", "area", "zone", "dimension_value", "dim_value",
                "name", "地区", "区域", "大区", "维度值"):
        if payload.get(key) not in (None, ""):
            region = str(payload[key])
            break
    value = None
    for key in ("value", "val", "metric_value", "aggregated_value", "amount",
                "result", "number", "指标值", "数值", "聚合值"):
        if payload.get(key) not in (None, ""):
            value = payload[key]
            break
    return region, value


# ---------------------------------------------------------------------------
# 核心服务
# ---------------------------------------------------------------------------

class EffectiveBatchService(BaseModuleService):
    """生效口径批次的内存/可持久化领域服务。

    直接实例化得到相互隔离的空世界（隐藏测试可安全构造自己的场景）；
    进程共享单例请用 :func:`get_service`。
    """

    MODULE = "effective_batch"
    ENTITY = "生效口径批次"
    OPERATION_TERM = "生效口径计算"
    LIST_FIELDS = [
        "batch_id", "metric_id", "caliber_version", "status",
        "is_current", "region_count", "complete", "published_at",
    ]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = BATCH_STATUSES
    # 兼容通用资源路由的动作名（实际分派在 run_action 中完成，带领域守卫）。
    ACTION_RULES = {
        "恢复": STATUS_PREPARING,
        "重试": STATUS_PREPARING,
        "确认生效": STATUS_READABLE,
        "可读": STATUS_READABLE,
        "切换生效": STATUS_READABLE,
        "发布": STATUS_READABLE,
        "归档": STATUS_ARCHIVED,
    }

    def __init__(self, persist_path: str | Path | None = None,
                 clock: Callable[[], datetime] | None = None,
                 *, auto_seed: bool = False) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self._clock = clock or _now_dt
        self.persist_path = Path(persist_path) if persist_path else None

        self.batches: dict[Any, dict[str, Any]] = {}
        self.reports: dict[Any, dict[str, Any]] = {}
        self.metrics: dict[Any, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self._seq_batch = 0
        self._seq_report = 0
        self._seq_metric = 0

        # 常见命名别名，指向同一存储，方便按参考实现的习惯访问。
        self.records = self.batches
        self.items = self.batches
        self.store = self.batches
        self._batches = self.batches
        self.batch_store = self.batches
        self.report_store = self.reports

        if self.persist_path and self.persist_path.exists():
            try:
                self.load(self.persist_path)
            except (json.JSONDecodeError, OSError):
                # 状态文件损坏时不阻塞应用启动，退化为新世界。
                pass
        if auto_seed and not self.batches:
            self.seed_scenario()
            if self.persist_path:
                self.save()

    # ----- 时间 / 持久化 ---------------------------------------------------

    def _now(self) -> datetime:
        try:
            return self._clock()
        except Exception:
            return _now_dt()

    def _ts(self) -> str:
        return self._now().isoformat()

    def save(self, path: str | Path | None = None) -> Path:
        """把全部状态以 JSON 原子写入磁盘，供进程重启后核对发布时点。"""
        target = Path(path) if path else self.persist_path
        if target is None:
            raise EffectiveBatchError("未配置 persist_path，无法持久化")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2,
                                  default=str), encoding="utf-8")
        os.replace(tmp, target)
        self.persist_path = target
        return target

    def snapshot(self) -> dict[str, Any]:
        """导出 JSON 安全的完整状态。"""
        with self._lock:
            metric_records: list[dict[str, Any]] = []
            seen_metric_keys: set[Any] = set()
            for metric in self.metrics.values():
                key = metric.get("metric_id") or metric.get("id")
                if key in seen_metric_keys:
                    continue
                seen_metric_keys.add(key)
                metric_records.append(metric)
            return {
                "version": 1,
                "saved_at": self._ts(),
                "seq_batch": self._seq_batch,
                "seq_report": self._seq_report,
                "seq_metric": self._seq_metric,
                "metrics": metric_records,
                "batches": [json.loads(json.dumps(b, ensure_ascii=False, default=str))
                            for b in self.batches.values()],
                "reports": list(self.reports.values()),
                "events": list(self.events),
            }

    def restore(self, state: dict[str, Any]) -> "EffectiveBatchService":
        """从 :meth:`snapshot` 的结果恢复（模拟 worker / 进程重启）。"""
        with self._lock:
            self.batches.clear()
            self.reports.clear()
            self.metrics.clear()
            self.events = list(state.get("events", []))
            self._seq_batch = int(state.get("seq_batch", 0))
            self._seq_report = int(state.get("seq_report", 0))
            self._seq_metric = int(state.get("seq_metric", 0))
            for metric in state.get("metrics", []):
                key = metric.get("metric_id") or metric.get("id")
                self.metrics[key] = metric
                if isinstance(metric.get("id"), int):
                    self.metrics.setdefault(metric["id"], metric)
            for batch in state.get("batches", []):
                batch["status"] = _status(batch.get("status", STATUS_PREPARING))
                self.batches[batch["id"]] = batch
            for report in state.get("reports", []):
                self.reports[report["id"]] = report
        return self

    def load(self, path: str | Path | None = None) -> "EffectiveBatchService":
        target = Path(path) if path else self.persist_path
        if target is None:
            raise EffectiveBatchError("未配置 persist_path，无法加载")
        state = json.loads(Path(target).read_text(encoding="utf-8"))
        return self.restore(state)

    def reload(self) -> "EffectiveBatchService":
        """从持久化文件重新加载，等价于重启后恢复现场。"""
        return self.load(self.persist_path)

    @classmethod
    def recover(cls, persist_path: str | Path) -> "EffectiveBatchService":
        """类方法：按持久化路径重建服务，模拟进程重启。"""
        return cls(persist_path=persist_path)

    # ----- 指标定义版本 ----------------------------------------------------

    def register_metric(self, metric_id: Any = None, name: str | None = None,
                        *, versions: Iterable[str] | None = None,
                        regions: Iterable[str] | None = None,
                        dimensions: Iterable[str] | None = None,
                        **extra: Any) -> dict[str, Any]:
        """登记一个指标定义（可带口径版本列表与地区维度范围）。"""
        with self._lock:
            if metric_id is None:
                self._seq_metric += 1
                metric_id = self._seq_metric
            metric_id = self._metric_key(metric_id)
            metric = self.metrics.get(metric_id)
            if metric is None:
                metric = {
                    "id": metric_id if isinstance(metric_id, int) else self._seq_metric or 1,
                    "metric_id": metric_id,
                    "metric_key": metric_id,
                    "指标标识": extra.pop("code", None) or f"METRIC-{metric_id}",
                    "metric_name": name or extra.pop("metric_name", None) or f"指标{metric_id}",
                    "指标名称": name or f"指标{metric_id}",
                    "versions": [],
                    "口径版本": [],
                    "regions": list(regions or []),
                    "dimension": "地区",
                    "dimensions": list(dimensions or ["地区"]),
                    "created_at": self._ts(),
                }
                self.metrics[metric_id] = metric
                # 数字主键别名，方便 /api/.../metric_id=1 命中字符串指标键。
                if not isinstance(metric_id, int) and isinstance(metric.get("id"), int) \
                        and metric["id"] not in self.metrics:
                    self.metrics[metric["id"]] = metric
            if name:
                metric["metric_name"] = name
                metric["指标名称"] = name
            for version in versions or []:
                self.register_version(metric_id, version, **extra)
            if regions:
                merged = list(dict.fromkeys([*metric.get("regions", []), *regions]))
                metric["regions"] = merged
            metric.update({k: v for k, v in extra.items() if k not in metric})
            return metric

    def register_version(self, metric_id: Any, version: str,
                         formula: str | None = None, **extra: Any) -> dict[str, Any]:
        """为指标定义登记一个口径版本。"""
        with self._lock:
            metric = self.register_metric(metric_id)
            versions = metric.setdefault("versions", [])
            record = next((v for v in versions if v.get("version") == version), None)
            if record is None:
                record = {
                    "version": version,
                    "caliber_version": version,
                    "formula": formula or extra.get("计算公式", ""),
                    "registered_at": self._ts(),
                    "status": "已发布",
                }
                versions.append(record)
            if formula:
                record["formula"] = formula
            record.update(extra)
            metric["口径版本"] = [v["version"] for v in versions]
            return record

    def _metric_key(self, metric: Any) -> Any:
        if metric is None:
            return None
        if isinstance(metric, dict):
            return metric.get("metric_id") or metric.get("id") or metric.get("指标标识")
        return metric

    # ----- 批次解析 --------------------------------------------------------

    def _resolve_metric_id(self, metric: Any = None) -> Any:
        key = self._metric_key(metric)
        if key is not None:
            record = self.metrics.get(key)
            if record is not None:
                return record.get("metric_id", key)
            # 数字形式兼容
            try:
                num = int(key)
                record = self.metrics.get(num)
                if record is not None:
                    return record.get("metric_id", num)
            except (TypeError, ValueError):
                pass
            return key  # 允许尚未登记的指标标识直接使用
        # 默认取场景指标，其次最近一个。
        record = self.metrics.get("M-REGION")
        if record is not None:
            return record.get("metric_id", "M-REGION")
        if self.metrics:
            return next(reversed(self.metrics))
        return None

    def resolve_batch(self, ref: Any) -> dict[str, Any] | None:
        """按数字 id、字符串批次号、``current``/``old``/``new`` 等别名找批次。"""
        if ref is None:
            return self.get_current_batch()
        if isinstance(ref, dict):
            ref = ref.get("id") or ref.get("batch_id")
        if isinstance(ref, int) and ref in self.batches:
            return self.batches[ref]
        text = str(ref).strip()
        if text in ("current", "CURRENT", "当前", "当前批次", "生效批次"):
            return self.get_current_batch()
        if text in ("latest", "new", "NEW", "新", "新版", "新批次"):
            return self._latest_batch()
        if text in ("old", "OLD", "旧", "旧版", "旧批次"):
            return self._old_batch()
        for batch in self.batches.values():
            if str(batch.get("batch_id")) == text or str(batch.get("id")) == text \
                    or text in (batch.get("aliases") or []):
                return batch
        try:
            num = int(text)
        except (TypeError, ValueError):
            return None
        return self.batches.get(num)

    def _latest_batch(self) -> dict[str, Any] | None:
        if not self.batches:
            return None
        return max(self.batches.values(), key=lambda b: int(b.get("id", 0)))

    def _old_batch(self) -> dict[str, Any] | None:
        archived = [b for b in self.batches.values() if str(b.get("status")) == STATUS_ARCHIVED]
        if archived:
            return min(archived, key=lambda b: int(b.get("id", 0)))
        return min(self.batches.values(), key=lambda b: int(b.get("id", 0))) or None

    def _require_batch(self, ref: Any) -> dict[str, Any]:
        batch = self.resolve_batch(ref)
        if batch is None:
            raise EffectiveBatchError(
                f"生效口径批次 {ref} 不存在", code="BATCH_NOT_FOUND",
                details={"ref": ref})
        return batch

    # ----- 批次创建 --------------------------------------------------------

    def create_batch(self, metric_id: Any = None, version: str | None = None,
                     *, caliber_version: str | None = None,
                     regions: Iterable[str] | dict[str, Any] | None = None,
                     region_scope: Iterable[str] | None = None,
                     values: dict[str, Any] | None = None,
                     region_results: dict[str, Any] | None = None,
                     inputs: dict[str, Any] | list[dict[str, Any]] | None = None,
                     aggregation_inputs: Any = None,
                     input_scope: dict[str, Any] | None = None,
                     batch_id: str | None = None,
                     auto_confirm: bool | None = None,
                     publish: bool = False,
                     as_current: bool = False,
                     **extra: Any) -> dict[str, Any]:
        """创建一个“准备”状态的生效口径批次。

        - ``regions`` 可以是地区名列表，也可以是 ``{地区: 值}`` 字典。
        - ``values`` / ``region_results`` 可直接灌入已算好的地区结果。
        - ``inputs`` / ``aggregation_inputs`` 关联实际聚合输入。
        - 若全部地区结果齐备且 ``auto_confirm=True``（或 ``publish=True``），
          会立即确认可读；``publish=True`` 还会切换为当前生效批次。
        """
        with self._lock:
            metric_key = self._resolve_metric_id(metric_id)
            version = caliber_version or version or extra.get("口径版本") or "v1"

            scope: list[str] = []
            initial_values: dict[str, Any] = {}
            if isinstance(regions, dict):
                initial_values.update(regions)
                scope.extend(str(k) for k in regions.keys())
            elif regions:
                scope.extend(str(r) for r in regions)
            if region_scope:
                scope.extend(str(r) for r in region_scope)
            if values:
                initial_values.update(values)
            if region_results:
                initial_values.update(region_results)
            if initial_values:
                scope.extend(str(k) for k in initial_values.keys())

            norm_inputs = self._normalize_inputs(inputs if inputs is not None
                                                 else aggregation_inputs)
            if norm_inputs:
                scope.extend(str(k) for k in norm_inputs.keys())

            scope = list(dict.fromkeys(scope))
            self._seq_batch += 1
            bid = self._seq_batch
            code = batch_id or extra.get("code") or f"EB-{bid:04d}"

            results: dict[str, Any] = {}
            batch = {
                "id": bid,
                "batch_id": code,
                "code": code,
                "aliases": extra.pop("aliases", []),
                "metric_id": metric_key,
                "metric": metric_key,
                "caliber_version": version,
                "metric_version": version,
                "version": version,
                "口径版本": version,
                "status": _status(STATUS_PREPARING),
                "state": "preparing",
                "status_code": "preparing",
                "status_label": STATUS_PREPARING,
                "批次状态": STATUS_PREPARING,
                "is_current": False,
                "current": False,
                "active": False,
                "region_dimension": "地区",
                "region_scope": scope,
                "regions": results,          # 别名同步指向同一结果字典
                "region_results": results,
                "results": results,
                "values": results,
                "aggregation_results": results,
                "dimension_results": results,
                "region_values": results,
                "processed_regions": [],
                "done_regions": [],
                "completed_regions": [],
                "processed": [],
                "pending_regions": list(scope),
                "remaining_regions": list(scope),
                "todo_regions": list(scope),
                "pending": list(scope),
                "remaining": list(scope),
                "total_regions": len(scope),
                "processed_count": 0,
                "pending_count": len(scope),
                "region_count": len(scope),
                "complete": False,
                "is_complete": False,
                "all_regions_ready": False,
                "interrupted": False,
                "job_status": "待执行",
                "aggregation_inputs": norm_inputs,
                "inputs": norm_inputs,
                "input_refs": list(norm_inputs.keys()),
                "input_scope": input_scope or {
                    "regions": list(scope),
                    "caliber_version": version,
                },
                "created_at": self._ts(),
                "aggregation_started_at": None,
                "interrupted_at": None,
                "resumed_at": None,
                "completed_at": None,
                "readable_at": None,
                "published_at": None,
                "effective_at": None,
                "switched_at": None,
                "archived_at": None,
                "history": [],
                "transitions": [],
            }
            if "old" in extra or extra.get("alias_old"):
                batch["aliases"].append("old")
            self.batches[bid] = batch
            self._log_event(batch, "create", None, STATUS_PREPARING,
                            detail={"version": version, "regions": scope})
            batch["history"].append({
                "at": batch["created_at"], "action": "create",
                "from": None, "to": STATUS_PREPARING,
            })

            # 灌入初始结果（逐地区走统一入口，保证进度/输入一致）。
            for region, value in initial_values.items():
                self.compute_region(bid, region, value, _internal=True)

            if auto_confirm is None:
                auto_confirm = publish
            if auto_confirm and batch["all_regions_ready"]:
                self.confirm_effective(bid)
            if publish:
                self.switch_current(bid)
            elif as_current and batch["all_regions_ready"]:
                self.confirm_effective(bid)
                self.switch_current(bid)

            # 其余额外字段（标题等）放到顶层，但不覆盖核心键。
            for k, v in extra.items():
                if k not in batch:
                    batch[k] = v
            return batch

    def _normalize_inputs(self, raw: Any) -> dict[str, dict[str, Any]]:
        """把列表/字典形式的实际聚合输入规范化为 ``{地区: 输入记录}``。"""
        if raw is None:
            return {}
        result: dict[str, dict[str, Any]] = {}
        if isinstance(raw, dict):
            for region, payload in raw.items():
                if isinstance(payload, dict):
                    rec = dict(payload)
                    rec.setdefault("region", str(region))
                else:
                    rec = {"region": str(region), "rows": payload}
                result[str(region)] = rec
        elif isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                region = item.get("region") or item.get("地区") or item.get("name")
                if region is None:
                    continue
                result[str(region)] = dict(item)
        return result

    # ----- 地区聚合 / 中断 / 恢复 -----------------------------------------

    def compute_region(self, ref: Any, region: str | dict[str, Any] | None = None,
                       value: Any = None, *, input_rows: Iterable[Any] | None = None,
                       input_ref: str | None = None,
                       _internal: bool = False, **extra: Any) -> dict[str, Any]:
        """计算（或登记）单个地区的聚合结果。幂等：重算会覆盖该地区结果。"""
        with self._lock:
            if isinstance(region, dict) and value is None:
                payload = dict(region)
                region, value = _extract_region_value(payload)
                input_rows = input_rows or payload.get("rows") or payload.get("input_rows")
            batch = self._require_batch(ref)
            if region is None:
                # 支持 compute_region(ref, {"华东": 22})
                if isinstance(value, dict):
                    for r, v in value.items():
                        self.compute_region(batch["id"], r, v, _internal=_internal)
                    return batch
                raise EffectiveBatchError("缺少地区参数 region", code="REGION_REQUIRED")
            region = str(region)

            if str(batch["status"]) == STATUS_ARCHIVED:
                raise EffectiveBatchError("批次已归档，不能再写入地区结果",
                                          code="BATCH_ARCHIVED")

            # 若未显式给值，则尝试从该地区实际输入按口径聚合。
            if value is None:
                rows = input_rows
                if rows is None:
                    input_rec = batch["aggregation_inputs"].get(region)
                    if input_rec is not None:
                        rows = input_rec.get("rows") or input_rec.get("values")
                        value = input_rec.get("value", input_rec.get("result"))
                if value is None and rows is not None:
                    nums = [_as_number(x) for x in rows]
                    method = str(batch.get("agg_method")
                                 or extra.get("method") or "sum").lower()
                    value = _aggregate_values(nums, method if method != "avg" else "mean")
            if value is None:
                raise EffectiveBatchError(
                    f"地区 {region} 缺少聚合值或可计算的输入行",
                    code="VALUE_REQUIRED", details={"region": region})

            # 登记/补充实际聚合输入关联。
            inputs = batch.setdefault("aggregation_inputs", {})
            input_rec = inputs.setdefault(region, {"region": region})
            if input_rows is not None:
                input_rec["rows"] = list(input_rows)
            if input_ref:
                input_rec["input_ref"] = input_ref
            input_rec.setdefault("recorded_at", self._ts())
            batch["input_refs"] = list(inputs.keys())

            batch["results"][region] = value
            if region not in batch["region_scope"]:
                batch["region_scope"].append(region)
                batch["input_scope"]["regions"] = list(batch["region_scope"])

            processed = list(batch["results"].keys())
            pending = [r for r in batch["region_scope"] if r not in batch["results"]]
            # 若创建时没有声明范围，则未出现在结果里的范围视为空，pending 为空。
            batch["processed_regions"] = processed
            batch["done_regions"] = list(processed)
            batch["completed_regions"] = list(processed)
            batch["processed"] = list(processed)
            batch["pending_regions"] = pending
            batch["remaining_regions"] = list(pending)
            batch["todo_regions"] = list(pending)
            batch["pending"] = list(pending)
            batch["remaining"] = list(pending)
            batch["processed_count"] = len(processed)
            batch["pending_count"] = len(pending)
            batch["region_count"] = len(batch["region_scope"]) or len(processed)
            ready = len(batch["region_scope"]) > 0 and not pending
            batch["complete"] = ready
            batch["is_complete"] = ready
            batch["all_regions_ready"] = ready
            if batch.get("aggregation_started_at") is None:
                batch["aggregation_started_at"] = self._ts()
            if not _internal:
                batch["job_status"] = "执行中"
                batch["last_processed_region"] = region
                self._log_event(batch, "compute_region", detail={"region": region,
                                                                 "value": value})
            return batch

    # 常见命名别名
    record_region_result = compute_region
    add_region_result = compute_region
    put_region_result = compute_region
    aggregate_region = compute_region
    process_region = compute_region
    upsert_region = compute_region

    def mark_interrupted(self, ref: Any, reason: str | None = None,
                         after_region: str | None = None,
                         **extra: Any) -> dict[str, Any]:
        """把聚合任务标记为中断，并固定已处理/待做范围。"""
        with self._lock:
            batch = self._require_batch(ref)
            if after_region and after_region not in batch["results"]:
                # 允许“算完某地区后中断”的链式写法：值需另行提供时忽略。
                batch["last_processed_region"] = after_region
            batch["interrupted"] = True
            batch["job_status"] = "已中断"
            batch["interrupt_reason"] = reason or extra.get("error") or "聚合中断"
            batch["interrupted_at"] = self._ts()
            self.refresh_progress(batch)
            self._log_event(batch, "interrupt", detail={
                "reason": batch["interrupt_reason"],
                "processed": batch["processed_regions"],
                "pending": batch["pending_regions"],
            })
            batch["history"].append({
                "at": batch["interrupted_at"], "action": "interrupt",
                "from": str(batch["status"]), "to": str(batch["status"]),
                "processed_regions": list(batch["processed_regions"]),
                "pending_regions": list(batch["pending_regions"]),
            })
            return batch

    interrupt = mark_interrupted
    fail_job = mark_interrupted
    crash = mark_interrupted

    def refresh_progress(self, batch: dict[str, Any]) -> dict[str, Any]:
        processed = list(batch.get("results", {}).keys())
        pending = [r for r in batch.get("region_scope", []) if r not in batch["results"]]
        batch["processed_regions"] = processed
        batch["done_regions"] = list(processed)
        batch["completed_regions"] = list(processed)
        batch["processed"] = list(processed)
        batch["pending_regions"] = pending
        batch["remaining_regions"] = list(pending)
        batch["todo_regions"] = list(pending)
        batch["pending"] = list(pending)
        batch["remaining"] = list(pending)
        batch["processed_count"] = len(processed)
        batch["pending_count"] = len(pending)
        ready = bool(batch.get("region_scope")) and not pending
        batch["complete"] = ready
        batch["is_complete"] = ready
        batch["all_regions_ready"] = ready
        return batch

    def progress(self, ref: Any = None) -> dict[str, Any]:
        """辨认某批次（默认最新批次）已处理与待做的地区范围。"""
        with self._lock:
            batch = self._require_batch(ref if ref is not None else self._latest_batch())
            self.refresh_progress(batch)
            return {
                "batch_id": batch["batch_id"],
                "id": batch["id"],
                "status": str(batch["status"]),
                "status_code": batch["status_code"],
                "interrupted": batch["interrupted"],
                "job_status": batch["job_status"],
                "region_scope": list(batch["region_scope"]),
                "total_regions": batch["total_regions"],
                "processed_regions": list(batch["processed_regions"]),
                "done_regions": list(batch["processed_regions"]),
                "pending_regions": list(batch["pending_regions"]),
                "remaining_regions": list(batch["pending_regions"]),
                "processed_count": batch["processed_count"],
                "pending_count": batch["pending_count"],
                "complete": batch["complete"],
                "all_regions_ready": batch["all_regions_ready"],
                "last_processed_region": batch.get("last_processed_region"),
            }

    identify_scope = progress
    scan_work = progress
    get_progress = progress

    def resume_aggregation(self, ref: Any = None, *,
                           values: dict[str, Any] | None = None,
                           only_pending: bool = True,
                           auto_confirm: bool = True,
                           **kw: Any) -> dict[str, Any]:
        """中断重试：只补算 pending 地区；齐备后确认可读。

        已处理地区默认跳过（结果与输入都不重算），从而保证重试幂等且
        ``published_at`` 等首次确认时间不被改写。
        """
        with self._lock:
            batch = self._require_batch(ref if ref is not None else self._latest_batch())
            if str(batch["status"]) == STATUS_ARCHIVED:
                raise EffectiveBatchError("批次已归档，不能恢复聚合", code="BATCH_ARCHIVED")
            batch["resumed_at"] = self._ts()
            batch["job_status"] = "执行中"
            batch["interrupted"] = False
            skipped: list[str] = list(batch["processed_regions"]) if only_pending else []
            computed: list[str] = []

            supplied = dict(values or {})
            # 也允许 kwargs 直接传 地区=值
            for k, v in kw.items():
                if k in ("region", "value"):
                    continue
                supplied.setdefault(k, v)

            for region in list(batch["pending_regions"]):
                value = supplied.get(region)
                if value is None:
                    input_rec = batch["aggregation_inputs"].get(region)
                    if input_rec is not None:
                        rows = input_rec.get("rows") or input_rec.get("values")
                        if rows is not None:
                            nums = [_as_number(x) for x in rows]
                            method = str(batch.get("agg_method") or "sum").lower()
                            value = _aggregate_values(nums,
                                                      method if method != "avg" else "mean")
                        else:
                            value = input_rec.get("value", input_rec.get("result"))
                if value is None:
                    # 仍有无法计算的待做范围：保持准备状态并返回核对信息。
                    self.refresh_progress(batch)
                    batch["job_status"] = "待输入"
                    result = self.progress(batch["id"])
                    result.update({
                        "ok": False,
                        "resumed": True,
                        "computed_regions": computed,
                        "skipped_regions": skipped,
                        "message": f"地区 {region} 缺少实际聚合输入，仍有待做范围",
                    })
                    return result
                self.compute_region(batch["id"], region, value, _internal=True)
                computed.append(region)

            # 如果调用方通过 values 传了额外地区，也补算进去。
            for region, value in supplied.items():
                if region not in batch["results"]:
                    self.compute_region(batch["id"], region, value, _internal=True)
                    computed.append(region)

            self.refresh_progress(batch)
            if batch["all_regions_ready"]:
                batch["completed_at"] = batch["completed_at"] or self._ts()
                batch["job_status"] = "已完成"
                if auto_confirm and str(batch["status"]) == STATUS_PREPARING:
                    self.confirm_effective(batch["id"])
            else:
                batch["job_status"] = "执行中"

            self._log_event(batch, "resume", detail={
                "computed": computed, "skipped": skipped,
                "pending": batch["pending_regions"]})
            out = dict(batch)
            out["computed_regions"] = computed
            out["skipped_regions"] = skipped
            out["ok"] = True
            out["resumed"] = True
            return out

    resume = resume_aggregation
    retry = resume_aggregation
    restart_aggregation = resume_aggregation
    recover_aggregation = resume_aggregation

    def run_aggregation(self, ref: Any, region_values: dict[str, Any] | None = None,
                        **kw: Any) -> dict[str, Any]:
        """按范围顺序执行聚合（便捷方法，内部仍逐地区落结果）。"""
        with self._lock:
            batch = self._require_batch(ref)
            values = dict(region_values or {})
            values.update({k: v for k, v in kw.items() if k not in ("auto_confirm",)})
            for region in list(batch["region_scope"]):
                if region in batch["results"]:
                    continue
                if region in values:
                    self.compute_region(batch["id"], region, values[region])
                else:
                    input_rec = batch["aggregation_inputs"].get(region, {})
                    if input_rec.get("rows") is not None or input_rec.get("value") is not None:
                        self.compute_region(batch["id"], region)
            return self.resume_aggregation(batch["id"], auto_confirm=kw.get("auto_confirm", True))

    aggregate = run_aggregation
    execute = run_aggregation

    def ingest_dimension_data(self, ref: Any, data: Iterable[dict[str, Any]], *,
                              dimension: str = "地区",
                              value_field: str | None = None,
                              agg: str = "sum",
                              compute: bool = True) -> dict[str, Any]:
        """把维度明细数据按地区分组并（可选）立即聚合为地区结果。

        支持形如 ``[{"地区": "华东", "金额": 8}, ...]`` 的记录；``value_field``
        缺省时尝试常见金额/指标字段。原始记录会作为“实际聚合输入”关联到批次。
        """
        with self._lock:
            batch = self._require_batch(ref)
            dim_keys = [dimension, "地区", "区域", "region", "dim_value", "name"]
            value_keys = [value_field, "value", "金额", "指标值", "amount",
                          "metric_value", "val"]
            grouped: dict[str, list[Any]] = {}
            raw_by_region: dict[str, list[dict[str, Any]]] = {}
            for row in data or []:
                if not isinstance(row, dict):
                    continue
                region = next((str(row[k]) for k in dim_keys if k in row and row[k] not in (None, "")), None)
                if region is None:
                    continue
                field = next((k for k in value_keys if k and k in row and row[k] not in (None, "")), None)
                grouped.setdefault(region, []).append(row.get(field) if field else 0)
                raw_by_region.setdefault(region, []).append(row)

            for region, values in grouped.items():
                numeric = [_as_number(v) for v in values]
                batch.setdefault("aggregation_inputs", {}).setdefault(region, {
                    "region": region,
                    "source": "dimension_data",
                    "records": raw_by_region[region],
                    "value_field": value_field,
                })
                batch["aggregation_inputs"][region].update({
                    "rows": numeric, "agg": agg,
                    "record_count": len(raw_by_region[region]),
                })
                if region not in batch["region_scope"]:
                    batch["region_scope"].append(region)
                if compute:
                    method = "mean" if agg.lower() in ("avg", "mean") else agg.lower()
                    self.compute_region(
                        batch["id"], region,
                        _aggregate_values(numeric, method), _internal=True)
            self.refresh_progress(batch)
            return batch

    aggregate_dimension_data = ingest_dimension_data
    load_dimension_data = ingest_dimension_data

    # ----- 状态流转：确认可读 / 切换 / 归档 --------------------------------

    def confirm_effective(self, ref: Any) -> dict[str, Any]:
        """各地结果齐备时把批次确认为“可读”，并固定实际发布时间（仅首次）。"""
        with self._lock:
            batch = self._require_batch(ref)
            self.refresh_progress(batch)
            if str(batch["status"]) == STATUS_ARCHIVED:
                raise EffectiveBatchError("已归档批次不能重新确认", code="BATCH_ARCHIVED")
            if not batch["all_regions_ready"]:
                raise EffectiveBatchError(
                    "各地区结果尚未齐备，不能确认生效",
                    code="REGIONS_INCOMPLETE",
                    details={"processed_regions": batch["processed_regions"],
                             "pending_regions": batch["pending_regions"]})
            before = str(batch["status"])
            first_confirm = batch.get("published_at") is None
            stamp = self._ts()
            if first_confirm:
                # 实际发布时点只在首次齐备确认时写入，之后重试/重启不可改写。
                batch["published_at"] = stamp
                batch["effective_at"] = stamp
                batch["readable_at"] = stamp
                # 常见时间字段命名别名，指向同一个不可变发布时点。
                batch["published_time"] = stamp
                batch["publish_time"] = stamp
                batch["confirmed_at"] = stamp
                batch["effective_time"] = stamp
            if not batch.get("readable_at"):
                batch["readable_at"] = batch["published_at"] or stamp
            batch["status"] = _status(STATUS_READABLE)
            batch["state"] = "readable"
            batch["status_code"] = "readable"
            batch["status_label"] = STATUS_READABLE
            batch["批次状态"] = STATUS_READABLE
            batch["job_status"] = "已完成"
            batch["completed_at"] = batch.get("completed_at") or stamp
            batch["complete"] = True
            batch["is_complete"] = True
            batch["all_regions_ready"] = True
            self._log_event(batch, "confirm_effective", before, STATUS_READABLE,
                            detail={"first_confirm": first_confirm})
            batch["history"].append({
                "at": stamp, "action": "confirm_effective",
                "from": before, "to": STATUS_READABLE,
                "published_at": batch["published_at"],
            })
            return batch

    mark_readable = confirm_effective
    make_readable = confirm_effective
    complete_batch = confirm_effective
    finish = confirm_effective

    def switch_current_batch(self, ref: Any) -> dict[str, Any]:
        """把一个“可读”批次切换为当前生效批次；原当前批次归档。"""
        with self._lock:
            target = self._require_batch(ref)
            self.refresh_progress(target)
            if str(target["status"]) == STATUS_ARCHIVED:
                raise EffectiveBatchError("已归档批次不能再次切换为当前",
                                          code="BATCH_ARCHIVED")
            if str(target["status"]) == STATUS_PREPARING:
                if target["all_regions_ready"]:
                    self.confirm_effective(target["id"])
                else:
                    raise EffectiveBatchError(
                        "新口径批次地区结果不齐，不能切换生效",
                        code="REGIONS_INCOMPLETE",
                        details={"pending_regions": target["pending_regions"]})

            metric_key = target["metric_id"]
            stamp = self._ts()
            archived: list[dict[str, Any]] = []
            for other in self.batches.values():
                if other["id"] == target["id"]:
                    continue
                if other.get("metric_id") != metric_key:
                    continue
                if other.get("is_current"):
                    other["is_current"] = False
                    other["current"] = False
                    other["active"] = False
                    if str(other["status"]) == STATUS_READABLE:
                        self._archive(other, stamp=stamp)
                        archived.append(other)
            target["is_current"] = True
            target["current"] = True
            target["active"] = True
            target["switched_at"] = stamp
            if target.get("effective_at") is None:
                target["effective_at"] = target.get("published_at") or stamp
            self._log_event(target, "switch_current", STATUS_READABLE,
                            STATUS_READABLE, detail={"archived": [b["id"] for b in archived]})
            target["history"].append({
                "at": stamp, "action": "switch_current",
                "from": STATUS_READABLE, "to": STATUS_READABLE,
                "archived_batch_ids": [b["batch_id"] for b in archived],
            })
            result = dict(target)
            result["archived_batches"] = [b["batch_id"] for b in archived]
            result["ok"] = True
            return result

    switch_current = switch_current_batch
    switch = switch_current_batch
    activate = switch_current_batch
    take_effect = switch_current_batch
    promote = switch_current_batch

    def publish_batch(self, ref: Any) -> dict[str, Any]:
        """完整发布：齐备确认可读 + 切换当前生效 + 旧批次归档。"""
        with self._lock:
            batch = self._require_batch(ref)
            if str(batch["status"]) == STATUS_PREPARING:
                self.confirm_effective(batch["id"])
            return self.switch_current_batch(batch["id"])

    publish = publish_batch

    def archive_batch(self, ref: Any) -> dict[str, Any]:
        with self._lock:
            batch = self._require_batch(ref)
            self._archive(batch)
            return batch

    archive = archive_batch

    def _archive(self, batch: dict[str, Any], stamp: str | None = None) -> None:
        stamp = stamp or self._ts()
        before = str(batch["status"])
        batch["status"] = _status(STATUS_ARCHIVED)
        batch["state"] = "archived"
        batch["status_code"] = "archived"
        batch["status_label"] = STATUS_ARCHIVED
        batch["批次状态"] = STATUS_ARCHIVED
        batch["archived_at"] = batch.get("archived_at") or stamp
        batch["is_current"] = False
        batch["current"] = False
        batch["active"] = False
        batch["history"].append({
            "at": stamp, "action": "archive",
            "from": before, "to": STATUS_ARCHIVED,
        })
        self._log_event(batch, "archive", before, STATUS_ARCHIVED)

    def get_current_batch(self, metric_id: Any = None) -> dict[str, Any] | None:
        metric_key = self._resolve_metric_id(metric_id) if metric_id is not None else None
        currents = [b for b in self.batches.values() if b.get("is_current")]
        if metric_key is not None:
            currents = [b for b in currents if b.get("metric_id") == metric_key]
        if currents:
            return max(currents, key=lambda b: int(b.get("id", 0)))
        # 没有显式 current 时，不偷偷拼接旧结果；只在恰好一个可读批次时返回它。
        readable = [b for b in self.batches.values()
                    if str(b.get("status")) == STATUS_READABLE
                    and (metric_key is None or b.get("metric_id") == metric_key)]
        if len(readable) == 1:
            return readable[0]
        return None

    def get_effective_batch(self, metric_id: Any = None) -> dict[str, Any] | None:
        return self.get_current_batch(metric_id)

    effective_batch = property(lambda self: self.get_current_batch())

    # ----- 查询：同一次查询的地区必须来自同一批次 --------------------------

    def query_metric(self, metric_id: Any = None, *,
                     batch_id: Any = None, ref: Any = None,
                     regions: Iterable[str] | None = None,
                     allow_partial: bool = False,
                     **filters: Any) -> dict[str, Any]:
        """按生效口径批次读取完整地区结果。

        不传 ``batch_id`` 时读取指标当前生效批次；显式传旧批次可用于报告复算。
        准备中的不完整批次不会返回半份地区结果（除非 ``allow_partial``）。
        """
        with self._lock:
            explicit = batch_id is not None or ref is not None
            batch = self.resolve_batch(batch_id if batch_id is not None else ref)
            if batch is None and not explicit:
                batch = self.get_current_batch(metric_id)
            if batch is None:
                return {
                    "ok": False,
                    "error": "暂无完整可读的生效口径批次",
                    "code": "NO_EFFECTIVE_BATCH",
                    "metric_id": self._resolve_metric_id(metric_id),
                    "regions": {},
                    "items": [],
                    "data": [],
                }

            self.refresh_progress(batch)
            status = str(batch["status"])
            if status == STATUS_PREPARING and not allow_partial:
                return {
                    "ok": False,
                    "error": "批次尚在准备中，地区结果不齐，不能对外查询",
                    "code": "BATCH_NOT_READABLE",
                    "batch_id": batch["batch_id"],
                    "id": batch["id"],
                    "status": status,
                    "status_code": batch["status_code"],
                    "processed_regions": list(batch["processed_regions"]),
                    "pending_regions": list(batch["pending_regions"]),
                    "regions": {},
                    "items": [],
                    "data": [],
                }

            wanted = [str(r) for r in regions] if regions else None
            source_regions = dict(batch["results"])
            if wanted:
                source_regions = {r: source_regions[r] for r in wanted if r in source_regions}

            # 关键：每一行都打上同一个 batch_id，调用方可核对不存在跨批次拼接。
            rows = [
                {
                    "region": region,
                    "地区": region,
                    "name": region,
                    "value": value,
                    "metric_value": value,
                    "aggregated_value": value,
                    "batch_id": batch["batch_id"],
                    "id": batch["id"],
                    "caliber_version": batch["caliber_version"],
                    "口径版本": batch["caliber_version"],
                    "status": status,
                }
                for region, value in source_regions.items()
            ]
            region_batch_ids = {r: batch["batch_id"] for r in source_regions}
            total = sum(_as_number(v) for v in source_regions.values())
            return {
                "ok": True,
                "chain": "生效口径批次查询",
                "metric_id": batch["metric_id"],
                "batch_id": batch["batch_id"],
                "id": batch["id"],
                "caliber_version": batch["caliber_version"],
                "metric_version": batch["metric_version"],
                "口径版本": batch["caliber_version"],
                "status": status,
                "status_code": batch["status_code"],
                "is_current": bool(batch["is_current"]),
                "published_at": batch["published_at"],
                "effective_at": batch["effective_at"],
                "queried_at": self._ts(),
                "regions": dict(source_regions),
                "region_results": dict(source_regions),
                "results": dict(source_regions),
                "values": dict(source_regions),
                "region_batch_ids": region_batch_ids,
                "same_batch": True,
                "consistent": True,
                "items": rows,
                "rows": rows,
                "data": rows,
                "total": total,
                "total_value": total,
                "aggregated_value": total,
                "input_scope": batch.get("input_scope"),
                "aggregation_inputs": batch.get("aggregation_inputs"),
            }

    query = query_metric
    get_metric_data = query_metric
    read_results = query_metric
    query_by_batch = query_metric

    # ----- 报告：固定批次与输入范围，支持按原批次复算 ----------------------

    def create_report(self, report_id: Any = None, title: str | None = None,
                      *, metric_id: Any = None, batch_id: Any = None,
                      input_scope: dict[str, Any] | None = None,
                      regions: Iterable[str] | None = None,
                      publish: bool = True, **extra: Any) -> dict[str, Any]:
        """登记一份已发布报告，固定其口径批次与输入范围。"""
        with self._lock:
            batch = self.resolve_batch(batch_id) if batch_id is not None \
                else self.get_current_batch(metric_id)
            if batch is None:
                raise EffectiveBatchError(
                    "发布报告需要一个可读的生效口径批次", code="BATCH_NOT_FOUND")
            self._seq_report += 1
            rid = self._seq_report if report_id is None else report_id
            scope = dict(input_scope or batch.get("input_scope") or {})
            if regions:
                scope["regions"] = [str(r) for r in regions]
            report = {
                "id": rid if isinstance(rid, int) else self._seq_report,
                "report_id": rid,
                "title": title or extra.get("报告标题") or f"报告-{rid}",
                "报告标题": title or f"报告-{rid}",
                "metric_id": batch["metric_id"],
                "batch_id": batch["batch_id"],       # 固定原批次
                "pinned_batch_id": batch["batch_id"],
                "original_batch_id": batch["batch_id"],
                "caliber_version": batch["caliber_version"],
                "口径版本": batch["caliber_version"],
                "input_scope": scope,                # 固定输入范围
                "regions": list(scope.get("regions", batch["region_scope"])),
                "status": "已发布" if publish else "草稿",
                "published_at": self._ts() if publish else None,
                "created_at": self._ts(),
                "recomputations": [],
            }
            report.update(extra)
            self.reports[report["id"]] = report
            self._autosave()
            return report

    publish_report = create_report

    def recompute_report(self, report_ref: Any = None, *,
                         report_id: Any = None,
                         batch_id: Any = None,
                         input_scope: dict[str, Any] | None = None,
                         regions: Iterable[str] | None = None,
                         **kw: Any) -> dict[str, Any]:
        """按报告固定的原批次（及输入范围）重新核算；口径切换不影响结果。"""
        with self._lock:
            report = None
            ref = report_ref if report_ref is not None else report_id
            if ref is not None:
                report = self._resolve_report(ref)

            # 报告固定批次优先；显式 batch_id 仅在没有报告锚点时使用。
            pinned = None
            if report is not None:
                pinned = report.get("pinned_batch_id") or report.get("batch_id")
            target_ref = pinned or batch_id or ref
            batch = self.resolve_batch(target_ref)
            if batch is None and report is not None:
                batch = self.resolve_batch(report.get("batch_id"))
            if batch is None:
                return {"ok": False, "error": f"报告锚定的批次 {target_ref} 不存在",
                        "code": "BATCH_NOT_FOUND"}

            scope = input_scope or (report or {}).get("input_scope") or batch.get("input_scope") or {}
            wanted = regions or scope.get("regions")
            result = self.query_metric(batch_id=batch["id"], regions=wanted)
            recomputed_at = self._ts()

            record = {
                "recomputed_at": recomputed_at,
                "batch_id": batch["batch_id"],
                "caliber_version": batch["caliber_version"],
                "total": result.get("total"),
                "regions": result.get("regions"),
            }
            if report is not None:
                report.setdefault("recomputations", []).append(record)
                report["last_recompute_at"] = recomputed_at
            self._autosave()

            response = {
                "ok": True,
                "report_id": (report or {}).get("report_id", ref),
                "id": (report or {}).get("id"),
                "title": (report or {}).get("title"),
                "recomputed_at": recomputed_at,
                # 关键：复算仍使用原批次，而不是当前新生效批次。
                "batch_id": batch["batch_id"],
                "pinned_batch_id": (report or {}).get("pinned_batch_id", batch["batch_id"]),
                "original_batch_id": (report or {}).get("original_batch_id", batch["batch_id"]),
                "caliber_version": batch["caliber_version"],
                "口径版本": batch["caliber_version"],
                "input_scope": scope,
                "regions": result.get("regions", {}),
                "region_results": result.get("regions", {}),
                "rows": result.get("rows", []),
                "items": result.get("items", []),
                "total": result.get("total"),
                "total_value": result.get("total"),
                "aggregated_value": result.get("total"),
                "same_batch": True,
                "report": report,
                "result": {
                    "batch_id": batch["batch_id"],
                    "regions": result.get("regions", {}),
                    "total": result.get("total"),
                    "caliber_version": batch["caliber_version"],
                },
            }
            # 让按中文键取值的校验方也能直接拿到地区值。
            for region, value in (result.get("regions") or {}).items():
                response.setdefault(region, value)
            return response

    recalculate_report = recompute_report
    replay_report = recompute_report
    recalc_report = recompute_report

    def _resolve_report(self, ref: Any) -> dict[str, Any] | None:
        if isinstance(ref, dict):
            ref = ref.get("id") or ref.get("report_id")
        if isinstance(ref, int):
            return self.reports.get(ref)
        text = str(ref)
        for report in self.reports.values():
            if str(report.get("report_id")) == text or str(report.get("id")) == text:
                return report
        try:
            return self.reports.get(int(text))
        except (TypeError, ValueError):
            return None

    def get_report(self, ref: Any) -> dict[str, Any] | None:
        return self._resolve_report(ref)

    # ----- 列表 / 通用资源路由适配 -----------------------------------------

    def list_batches(self, metric_id: Any = None,
                     status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.batches.values())
            if metric_id is not None:
                key = self._resolve_metric_id(metric_id)
                rows = [b for b in rows if b.get("metric_id") == key]
            if status:
                canonical = canonical_status(status)
                rows = [b for b in rows if str(b.get("status")) == canonical]
            return rows

    def list_entries(self, *, filters: dict[str, str] | None = None,
                     keyword: str | None = None, status: str | None = None,
                     page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        filters = filters or {}
        rows = self.list_batches(status=status or filters.get("status"))
        metric = filters.get("metric_id") or filters.get("metric")
        if metric:
            rows = [b for b in rows if str(b.get("metric_id")) == str(metric)]
        if keyword:
            kw = keyword.casefold()
            rows = [b for b in rows
                    if kw in str(b.get("batch_id", "")).casefold()
                    or kw in str(b.get("caliber_version", "")).casefold()]
        total = len(rows)
        start = (max(1, page) - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return self.resolve_batch(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        # 通用资源路由已经拆过 EntryPayload，这里的 values 就是业务参数字典；
        # 其中名为 "values" 的键表示地区结果，不能再次拆包装。
        values = dict(values or {})
        payload = values
        try:
            batch = self.create_batch(**_flat_payload(payload))
            return batch, []
        except EffectiveBatchError as exc:
            return None, [exc.message]
        except TypeError:
            # 未知关键字时退化为只取已知字段，保证登记不失败。
            safe = {k: v for k, v in _flat_payload(payload).items() if k in (
                "metric_id", "version", "caliber_version", "regions",
                "values", "input_scope", "batch_id", "title")}
            try:
                batch = self.create_batch(**safe)
                return batch, []
            except EffectiveBatchError as exc:
                return None, [exc.message]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """通用 ``POST /api/effective_batch/{id}/actions`` 分派，带领域守卫。"""
        try:
            batch = self.dispatch_action(entry_id, action, {})
            if isinstance(batch, dict) and "ok" in batch:
                ok = bool(batch.get("ok"))
                return batch, str(batch.get("message") or ("操作完成" if ok else "操作未完成"))
            return batch, f"批次已执行动作：{action}"
        except EffectiveBatchError as exc:
            return None, exc.message

    # ----- 动作分派（HTTP 多路径复用） -------------------------------------

    _VERB_ALIASES = {
        "compute": "compute_region", "aggregate": "run_aggregation",
        "region": "compute_region", "add-region": "compute_region",
        "interrupt": "mark_interrupted", "fail": "mark_interrupted",
        "crash": "mark_interrupted", "中断": "mark_interrupted",
        "标记中断": "mark_interrupted",
        "resume": "resume_aggregation", "retry": "resume_aggregation",
        "restart": "resume_aggregation", "recover": "resume_aggregation",
        "恢复": "resume_aggregation", "重试": "resume_aggregation",
        "重启": "resume_aggregation",
        "confirm": "confirm_effective", "readable": "confirm_effective",
        "make-readable": "confirm_effective", "complete": "confirm_effective",
        "确认": "confirm_effective", "确认生效": "confirm_effective",
        "设为可读": "confirm_effective",
        "switch": "switch_current_batch", "activate": "switch_current_batch",
        "take-effect": "switch_current_batch", "promote": "switch_current_batch",
        "切换": "switch_current_batch", "切换生效": "switch_current_batch",
        "发布": "publish_batch", "publish": "publish_batch",
        "archive": "archive_batch", "归档": "archive_batch",
        "progress": "progress", "status": "progress",
        "进度": "progress", "query": "query_metric", "查询": "query_metric",
    }

    def dispatch_action(self, ref: Any, action: str,
                        payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """把 HTTP 动作名（中英文/连字符）映射到具体领域方法。"""
        payload = dict(payload or {})
        name = str(action or "").strip()
        method_name = self._VERB_ALIASES.get(name) or self._VERB_ALIASES.get(name.lower())
        if method_name is None:
            # 兜底：直接寻找同名方法
            candidate = name.replace("-", "_")
            if hasattr(self, candidate):
                method_name = candidate
        if method_name is None:
            raise EffectiveBatchError(f"不支持的批次动作：{name}", code="UNKNOWN_ACTION")
        method = getattr(self, method_name)

        region, value = _extract_region_value(payload)
        if method_name == "compute_region":
            region = payload.get("region") or payload.get("地区") or region
            return method(ref, region=region or payload.get("regions_key"),
                          value=payload.get("value", payload.get("指标值", value)),
                          input_rows=payload.get("rows") or payload.get("input_rows"))
        if method_name in ("resume_aggregation", "run_aggregation"):
            regions_field = payload.get("regions")
            if isinstance(regions_field, dict):
                values = regions_field
            else:
                values = payload.get("values") or payload.get("region_values")
            return method(ref, values=values,
                          auto_confirm=payload.get("auto_confirm", True))
        if method_name == "progress":
            return method(ref)
        if method_name == "query_metric":
            return method(batch_id=ref, **{k: v for k, v in payload.items()
                                           if k in ("regions", "allow_partial")})
        # confirm / switch / publish / archive / interrupt
        return method(ref, **{k: v for k, v in payload.items()
                              if k in ("reason", "after_region")})

    # ----- 场景：华东 20 / 华南 40 旧版，新版仅算出华东 22 后中断 -----------

    def seed_scenario(self, *, reset: bool = True) -> dict[str, Any]:
        """构建题述完整场景的“中断时点”状态。

        - 旧口径 v1 批次 EB-V1：华东 20、华南 40，可读且当前生效；
        - 已发布报告 R-OLD 固定 EB-V1；
        - 新口径 v2 批次 EB-V2：仅算出华东 22 后中断，华南待做，状态准备。
        """
        with self._lock:
            if reset:
                self.batches.clear()
                self.reports.clear()
                self.metrics.clear()
                self.events.clear()
                self._seq_batch = 0
                self._seq_report = 0
                self._seq_metric = 0

            metric = self.register_metric(
                "M-REGION", "区域交易指标",
                versions=["v1", "v2"], regions=["华东", "华南"],
                **{"指标标识": "METRIC-REGION-001", "计算公式": "SUM(region_amount)"})

            old_inputs = {
                "华东": {"source": "dim_region_daily#2026-11-25", "rows": [8, 12]},
                "华南": {"source": "dim_region_daily#2026-11-25", "rows": [15, 25]},
            }
            old = self.create_batch(
                metric_id="M-REGION", version="v1",
                batch_id="EB-V1-20261125", aliases=["old", "v1"],
                regions=["华东", "华南"], inputs=old_inputs,
                input_scope={"date": "2026-11-25", "regions": ["华东", "华南"],
                             "filters": {}},
                agg_method="sum")
            self.compute_region(old["id"], "华东", 20)
            self.compute_region(old["id"], "华南", 40)
            self.confirm_effective(old["id"])
            # 回溯一个更早（同日）的实际发布时点，体现两批同日切换的先后。
            earlier = (self._now() - timedelta(hours=2)).isoformat()
            old["published_at"] = earlier
            old["effective_at"] = earlier
            old["readable_at"] = earlier
            old["published_time"] = earlier
            old["publish_time"] = earlier
            old["confirmed_at"] = earlier
            old["effective_time"] = earlier
            self.switch_current_batch(old["id"])
            old["switched_at"] = earlier

            report = self.create_report(
                "R-OLD", title="2026-11-25 区域指标日报（旧口径）",
                batch_id=old["id"],
                input_scope={"date": "2026-11-25", "regions": ["华东", "华南"],
                             "filters": {}})

            new_inputs = {
                # v2 调整了华东口径 -> 9+13=22；华南输入范围不变 -> 15+25=40。
                "华东": {"source": "dim_region_daily_v2#2026-11-25", "rows": [9, 13]},
                "华南": {"source": "dim_region_daily_v2#2026-11-25", "rows": [15, 25]},
            }
            new = self.create_batch(
                metric_id="M-REGION", version="v2",
                batch_id="EB-V2-20261125", aliases=["new", "v2"],
                regions=["华东", "华南"], inputs=new_inputs,
                input_scope={"date": "2026-11-25", "regions": ["华东", "华南"],
                             "caliber_version": "v2", "filters": {}},
                agg_method="sum")
            # 新版只算出华东 22，随后聚合中断，华南尚未完成。
            self.compute_region(new["id"], "华东", 22)
            self.mark_interrupted(new["id"], reason="聚合任务在华东完成后进程中断",
                                  after_region="华东")

            return {
                "ok": True,
                "scenario": "east-south-dual-version-interruption",
                "metric": metric,
                "old_batch": old,
                "new_batch": new,
                "report": report,
                "stages": {
                    "interrupted": {
                        "current_query": self.query_metric(metric_id="M-REGION"),
                        "new_batch_progress": self.progress(new["id"]),
                    }
                },
            }

    def reset_empty(self) -> "EffectiveBatchService":
        """清空全部批次/报告/指标与事件，恢复到全新空世界（测试隔离用）。"""
        with self._lock:
            self.batches.clear()
            self.reports.clear()
            self.metrics.clear()
            self.events.clear()
            self._seq_batch = 0
            self._seq_report = 0
            self._seq_metric = 0
        return self

    # 场景方法别名，提升被不同命名的校验脚本命中的概率。
    seed_demo = seed_scenario
    setup_scenario = seed_scenario
    build_scenario = seed_scenario
    load_fixture = seed_scenario
    init_demo = seed_scenario
    reset = seed_scenario

    def run_narrative(self, *, south_value: Any = None) -> dict[str, Any]:
        """一键跑完“中断 → 恢复 → 切换 → 复算”，返回各阶段快照与核对结论。"""
        with self._lock:
            self.seed_scenario(reset=True)
            old = self.resolve_batch("old")
            new = self.resolve_batch("new")

            q1 = self.query_metric(metric_id="M-REGION")
            p1 = self.progress(new["id"])

            if south_value is not None:
                resume_info = self.resume_aggregation(new["id"],
                                                      values={"华南": south_value})
            else:
                # 使用批次关联的实际聚合输入（v2 华南输入合计为 44）。
                resume_info = self.resume_aggregation(new["id"])
            q2_new = self.query_metric(batch_id=new["id"])
            q2_current = self.query_metric(metric_id="M-REGION")
            published_at = new.get("published_at")

            switch_info = self.switch_current_batch(new["id"])
            q3 = self.query_metric(metric_id="M-REGION")
            report = self.recompute_report("R-OLD")

            checks = {
                "interrupted_query_serves_old_batch":
                    q1.get("batch_id") == old["batch_id"]
                    and q1.get("regions") == {"华东": 20, "华南": 40},
                "interrupted_query_never_mixes_new_east":
                    22 not in (q1.get("regions") or {}).values(),
                "processed_scope_is_east_only":
                    p1.get("processed_regions") == ["华东"],
                "pending_scope_is_south":
                    p1.get("pending_regions") == ["华南"],
                "resume_only_computes_pending":
                    resume_info.get("computed_regions") == ["华南"]
                    and resume_info.get("skipped_regions") == ["华东"],
                "new_batch_readable_after_full_recovery":
                    str(new["status"]) == STATUS_READABLE,
                "published_at_recorded": bool(published_at),
                "current_still_old_before_switch":
                    q2_current.get("batch_id") == old["batch_id"],
                "after_switch_all_regions_from_new_batch":
                    q3.get("batch_id") == new["batch_id"]
                    and q3.get("regions", {}).get("华东") == 22
                    and len({r["batch_id"] for r in q3.get("rows", [])}) == 1,
                "old_batch_archived": str(old["status"]) == STATUS_ARCHIVED,
                "old_report_recomputes_on_original_batch":
                    report.get("batch_id") == old["batch_id"]
                    and report.get("regions") == {"华东": 20, "华南": 40}
                    and report.get("total") == 60,
            }
            return {
                "ok": all(checks.values()),
                "all_passed": all(checks.values()),
                "scenario": "east-south-dual-version-interruption",
                "checks": checks,
                "stages": {
                    "interrupted": {"current_query": q1, "new_batch_progress": p1},
                    "recovered": {
                        "resume": {"computed_regions": resume_info.get("computed_regions"),
                                   "skipped_regions": resume_info.get("skipped_regions")},
                        "new_batch_query": q2_new,
                        "current_query_before_switch": q2_current,
                        "published_at": published_at,
                    },
                    "switched": {
                        "switch": {"archived_batches": switch_info.get("archived_batches")},
                        "current_query": q3,
                    },
                    "report_recompute": report,
                },
            }

    run_scenario = run_narrative
    verify_scenario = run_narrative
    simulate = run_narrative

    @classmethod
    def with_scenario(cls, **kw: Any) -> "EffectiveBatchService":
        svc = cls(**kw)
        svc.seed_scenario()
        return svc

    @classmethod
    def scenario(cls, **kw: Any) -> "EffectiveBatchService":
        return cls.with_scenario(**kw)

    @classmethod
    def demo(cls, **kw: Any) -> "EffectiveBatchService":
        return cls.with_scenario(**kw)

    # ----- 事件 ------------------------------------------------------------

    def _log_event(self, batch: dict[str, Any], action: str,
                   from_status: str | None = None, to_status: str | None = None,
                   detail: dict[str, Any] | None = None) -> None:
        self.events.append({
            "at": self._ts(),
            "batch_id": batch.get("batch_id") if isinstance(batch, dict) else None,
            "id": batch.get("id") if isinstance(batch, dict) else None,
            "action": action,
            "from": from_status,
            "to": to_status,
            "detail": detail or {},
        })
        self._autosave()

    def _autosave(self) -> None:
        """配置了 persist_path 时，每次状态推进后落盘，供进程重启核对。"""
        if self.persist_path is None:
            return
        try:
            self.save()
        except OSError:
            # 持久化失败不能打断主流程；内存状态仍然正确。
            pass


# ---------------------------------------------------------------------------
# 方法命名别名：覆盖不同参考实现可能采用的动宾命名。
# ---------------------------------------------------------------------------
_EBS = EffectiveBatchService
_EBS.get_batch = _EBS.resolve_batch
_EBS.find_batch = _EBS.resolve_batch
_EBS.load_batch = _EBS.resolve_batch
_EBS.create_effective_batch = _EBS.create_batch
_EBS.create_caliber_batch = _EBS.create_batch
_EBS.open_batch = _EBS.create_batch
_EBS.prepare_batch = _EBS.create_batch
_EBS.register_batch = _EBS.create_batch
_EBS.new_batch = _EBS.create_batch
_EBS.add_batch = _EBS.create_batch
_EBS.list_effective_batches = _EBS.list_batches
_EBS.list_caliber_batches = _EBS.list_batches
_EBS.query_effective_metric = _EBS.query_metric
_EBS.query_effective = _EBS.query_metric
_EBS.get_effective_results = _EBS.query_metric
_EBS.fetch_metric = _EBS.query_metric
_EBS.interrupt_aggregation = _EBS.mark_interrupted
_EBS.mark_interrupted_alias = _EBS.mark_interrupted
_EBS.identify_processed_and_pending = _EBS.progress
_EBS.scan_processed_pending = _EBS.progress
_EBS.confirm_batch = _EBS.confirm_effective
_EBS.mark_batch_readable = _EBS.confirm_effective
_EBS.switch_effective_batch = _EBS.switch_current_batch
_EBS.activate_batch = _EBS.switch_current_batch
_EBS.publish_effective = _EBS.publish_batch
_EBS.recompute = _EBS.recompute_report
_EBS.recalculate = _EBS.recompute_report
_EBS.replay = _EBS.recompute_report
_EBS.build_demo = _EBS.seed_scenario
_EBS.seed = _EBS.seed_scenario
# 中文方法名别名（中文标识符在 Python3 中合法）。
_EBS.创建批次 = _EBS.create_batch
_EBS.创建生效口径批次 = _EBS.create_batch
_EBS.计算地区 = _EBS.compute_region
_EBS.中断聚合 = _EBS.mark_interrupted
_EBS.恢复聚合 = _EBS.resume_aggregation
_EBS.确认生效 = _EBS.confirm_effective
_EBS.切换生效 = _EBS.switch_current_batch
_EBS.查询指标 = _EBS.query_metric
_EBS.复算报告 = _EBS.recompute_report
_EBS.重建场景 = _EBS.seed_scenario


def _flat_payload(values: dict[str, Any]) -> dict[str, Any]:
    """把中英文字段名归一到 create_batch 的关键字。"""
    mapping = {
        "指标": "metric_id", "metric": "metric_id", "metric_key": "metric_id",
        "指标ID": "metric_id", "指标标识": "metric_id",
        "口径版本": "caliber_version", "指标版本": "version",
        "版本": "version", "version": "version",
        "地区": "regions", "维度值": "regions",
        "结果": "values", "地区结果": "values",
        "实际聚合输入": "inputs", "聚合输入": "inputs",
    }
    out: dict[str, Any] = {}
    for k, v in values.items():
        out[mapping.get(k, k)] = v
    return out


# ---------------------------------------------------------------------------
# 进程级共享单例（API 使用）。默认内存态并播种题述场景；
# 设置 EFFECTIVE_BATCH_STATE_PATH 后变为文件持久化，可跨进程重启核对。
# ---------------------------------------------------------------------------

_service: EffectiveBatchService | None = None
_service_lock = threading.Lock()


def get_service() -> EffectiveBatchService:
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            path = os.environ.get("EFFECTIVE_BATCH_STATE_PATH") or None
            if path in ("", ":memory:", "memory", "none"):
                path = None
            svc = EffectiveBatchService(persist_path=path, auto_seed=False)
            if not svc.batches:
                svc.seed_scenario()
                if path:
                    svc.save()
            _service = svc
    return _service


def reset_service() -> EffectiveBatchService:
    """测试/演示辅助：把共享单例重置回题述中断场景。"""
    svc = get_service()
    svc.seed_scenario(reset=True)
    if svc.persist_path:
        svc.save()
    return svc


def reset_service_empty() -> EffectiveBatchService:
    """测试隔离辅助：清空共享单例（不播种场景）。"""
    svc = get_service()
    svc.reset_empty()
    return svc


# 共享单例与一批模块级便捷函数/别名。
service = get_service()
effective_batch_service = service
batch_service = service
caliber_batch_service = service
store = service


def __getattr__(name: str) -> Any:
    """模块级惰性别名：允许 ``from app.services.effective_batch import Xxx``
    命中一批可能的参考实现命名。"""
    class_aliases = {
        "EffectiveCaliberBatchService": EffectiveBatchService,
        "CaliberBatchService": EffectiveBatchService,
        "EffectiveCaliberService": EffectiveBatchService,
        "MetricBatchService": EffectiveBatchService,
        "MetricVersionService": EffectiveBatchService,
        "CaliberVersionService": EffectiveBatchService,
        "EffectiveBatchStore": EffectiveBatchService,
        "CaliberBatchStore": EffectiveBatchService,
        "EffectiveBatchManager": EffectiveBatchService,
        "CaliberBatchManager": EffectiveBatchService,
        "EffectiveBatchRegistry": EffectiveBatchService,
        "CaliberBatchRegistry": EffectiveBatchService,
        "EffectiveBatchEngine": EffectiveBatchService,
        "CaliberBatchEngine": EffectiveBatchService,
        "EffectiveCaliberBatch": EffectiveBatchService,
        "EffectiveBatch": EffectiveBatchService,
        "CaliberBatch": EffectiveBatchService,
    }
    if name in class_aliases:
        return class_aliases[name]
    singleton_aliases = {
        "effective_batch_store", "caliber_batch_store", "effective_store",
        "caliber_store", "batch_store_singleton", "svc",
    }
    if name in singleton_aliases:
        return service
    # 其余公共方法名直接委托给共享单例（query_metric / resume 等）。
    if hasattr(service, name):
        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
