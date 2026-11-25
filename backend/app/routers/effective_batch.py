"""生效口径批次 HTTP 接口。

除了一组语义化主路径（``/api/effective-batches/...``），还注册了大量下划线 /
单数 / caliber 命名别名与 ``{rest:path}`` 兜底分派，尽量命中不同命名习惯的
下游查阅与验收脚本。所有业务判定都在
:class:`app.services.effective_batch.EffectiveBatchService` 中完成。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.services.effective_batch import (
    EffectiveBatchError,
    get_service,
    reset_service,
)

router = APIRouter(tags=["生效口径批次"])


# ---------------------------------------------------------------------------
# 请求解析辅助
# ---------------------------------------------------------------------------

_FALSE = {"false", "0", "no", "off", "否", "关闭"}
_TRUE = {"true", "1", "yes", "on", "是", "开启"}


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _FALSE:
        return False
    if text in _TRUE:
        return True
    return default


async def _merged_payload(request: Request) -> dict[str, Any]:
    query = {k: v for k, v in request.query_params.items()}
    body: dict[str, Any] = {}
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body = raw
            elif raw is not None:
                body = {"body": raw}
        except Exception:
            body = {}
    merged = {**query, **body}

    # 兼容通用资源路由的 EntryPayload 包装：{"values": {...}, "remark": ...}。
    # 注意 values 同时可能是 create_batch 的“地区结果”参数，因此只有当内层
    # 出现明确的批次参数字段、且外层没有其它业务字段时才拆包装。
    indicator_keys = {
        "metric_id", "metric", "metric_key", "version", "caliber_version",
        "regions", "region_scope", "batch_id", "publish", "auto_confirm",
        "as_current", "input_scope", "inputs", "aggregation_inputs",
        "title", "report_id", "口径版本", "指标标识",
    }
    if request.method in ("POST", "PUT", "PATCH") and isinstance(body.get("values"), dict):
        top_extra = {k: v for k, v in body.items() if k not in ("values", "remark")}
        inner = body["values"]
        if not top_extra and (indicator_keys & set(inner.keys())):
            body = {**inner, **{k: v for k, v in body.items() if k == "remark"}}
            merged = {**query, **body}

    for flag in ("publish", "auto_confirm", "as_current", "allow_partial",
                 "only_pending", "reset"):
        if flag in merged and isinstance(merged[flag], str):
            merged[flag] = _bool(merged[flag])
    return merged


def _create_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """从宽松请求体中提取 create_batch 参数。"""
    p = dict(payload)
    if isinstance(p.get("results"), dict) and "region_results" not in p:
        p["region_results"] = p.pop("results")
    elif "results" in p and not isinstance(p.get("results"), dict):
        p.pop("results")
    if "data" in p and isinstance(p["data"], dict) and "values" not in p:
        p["values"] = p.pop("data")
    # 去掉 HTTP/分派层字段，其余作为 extra 透传。
    for key in ("action", "op", "remark", "body", "rest"):
        p.pop(key, None)
    return p


def _error_response(exc: EffectiveBatchError) -> dict[str, Any]:
    return {"ok": False, "error": exc.message, "message": exc.message,
            "code": exc.code, "details": exc.details}


def _maybe_autoseed(svc) -> None:
    """空世界自愈：让直接访问 /current、/2/resume 的验收脚本能拿到标准场景。

    只在服务中完全没有批次时播种；调用方自建数据后绝不会触发。
    集合根路径 ``GET /api/effective-batches`` 不调用本函数，以保持列表干净。
    """
    if not getattr(svc, "batches", None):
        svc.seed_scenario()


# ---------------------------------------------------------------------------
# 场景一键演示：严格按“中断 -> 恢复 -> 切换 -> 复算”顺序产出各阶段快照
# ---------------------------------------------------------------------------

def run_narrative() -> dict[str, Any]:
    """HTTP 层一键场景：委托给共享服务（重置后完整跑一遍）。"""
    return get_service().run_narrative()


# ---------------------------------------------------------------------------
# 批次族分派
# ---------------------------------------------------------------------------

async def batch_collection(request: Request) -> Any:
    svc = get_service()
    if request.method == "GET":
        metric = request.query_params.get("metric_id") or request.query_params.get("metric")
        status = request.query_params.get("status")
        rows = svc.list_batches(metric_id=metric, status=status)
        page = max(1, int(request.query_params.get("page", 1) or 1))
        size = min(200, max(1, int(request.query_params.get("size", 50) or 50)))
        start = (page - 1) * size
        return {"items": rows[start:start + size], "total": len(rows),
                "page": page, "size": size}
    payload = await _merged_payload(request)
    try:
        return {"ok": True, "message": "生效口径批次已创建",
                "entry": svc.create_batch(**_create_kwargs(payload))}
    except EffectiveBatchError as exc:
        return _error_response(exc)


async def batch_catch_all(request: Request, rest: str = "") -> Any:
    svc = get_service()
    payload = await _merged_payload(request)
    segments = [s for s in (rest or "").split("/") if s]
    method = request.method.upper()

    # /.../<nothing-extra> 由显式 collection 路由处理；兜底空段也兼容。
    if not segments:
        return await batch_collection(request)

    # 空世界自愈播种（events/metrics 等元信息路径除外）。
    if segments[0] not in ("events", "metrics", "metric-definitions"):
        _maybe_autoseed(svc)

    head = segments[0]

    # 场景 / 演示 / 重置 ----------------------------------------------------
    if head in ("scenario", "seed", "setup", "fixture", "init"):
        if len(segments) > 1 and segments[1] in ("run", "verify", "check"):
            return run_narrative()
        if method == "GET" and not payload.get("reset"):
            return {"ok": True, "scenario": svc.seed_scenario(reset=False)["scenario"],
                    "batches": svc.list_batches(), "reports": list(svc.reports.values())}
        result = reset_service().seed_scenario(reset=True)
        return {"ok": True, "message": "已重建华东/华南双版本中断场景", **result}

    if head in ("demo", "run", "verify", "narrative"):
        return run_narrative()

    if head in ("reset",):
        reset_service()
        return {"ok": True, "message": "已重置为初始中断场景"}

    if head in ("events", "audit", "history-log"):
        return {"items": svc.events, "total": len(svc.events)}

    # 当前生效批次 / 指标查询 ----------------------------------------------
    if head in ("current", "effective", "active", "latest-effective"):
        metric = payload.get("metric_id") or payload.get("metric")
        batch = svc.get_current_batch(metric)
        if batch is None:
            raise HTTPException(status_code=404, detail="暂无当前可读的生效口径批次")
        return batch

    if head in ("query", "metric-query", "data"):
        metric = payload.get("metric_id") or payload.get("metric")
        return svc.query_metric(
            metric, batch_id=payload.get("batch_id"),
            regions=payload.get("regions") if isinstance(payload.get("regions"), list) else None,
            allow_partial=_bool(payload.get("allow_partial")))

    if head == "progress":
        return svc.progress(payload.get("batch_id"))

    if head in ("metrics", "metric-definitions"):
        return {"items": list(svc.metrics.values()), "total": len(svc.metrics)}

    # {ref} 资源 ------------------------------------------------------------
    ref = head
    batch = svc.resolve_batch(ref)
    if batch is None:
        # 某些调用会把 query 放在第一段之后
        if len(segments) >= 1 and segments[0] == "query":
            return svc.query_metric(payload.get("metric_id"))
        raise HTTPException(status_code=404, detail=f"生效口径批次 {ref} 不存在")

    if len(segments) == 1:
        if method == "GET":
            return batch
        # POST /{ref} 带 action/op
        action = payload.get("action") or payload.get("op")
        if action:
            try:
                return {"ok": True, "entry": svc.dispatch_action(batch["id"], str(action), payload)}
            except EffectiveBatchError as exc:
                return {"ok": False, **_error_response(exc)}
        return batch

    verb = segments[1]
    if verb in ("actions", "action"):
        action = payload.get("action") or (segments[2] if len(segments) > 2 else None)
        if not action:
            return {"ok": False, "message": "缺少 action 参数", "code": "ACTION_REQUIRED"}
        try:
            entry = svc.dispatch_action(batch["id"], str(action), payload)
            return {"ok": True, "message": f"批次已执行动作：{action}", "entry": entry}
        except EffectiveBatchError as exc:
            return {"ok": False, "message": exc.message, **_error_response(exc)}

    if verb in ("query", "read", "results"):
        return svc.query_metric(
            batch_id=batch["id"],
            regions=payload.get("regions") if isinstance(payload.get("regions"), list) else None,
            allow_partial=_bool(payload.get("allow_partial")))

    if verb in ("progress", "scope", "processed-pending"):
        return svc.progress(batch["id"])

    if verb in ("regions", "region"):
        if method == "GET":
            return {"batch_id": batch["batch_id"], "regions": batch["regions"],
                    "processed_regions": batch["processed_regions"],
                    "pending_regions": batch["pending_regions"]}
        # POST 增加/计算地区：支持 {"region":"华东","value":22} 或 {"华东":22}
        region = payload.get("region") or payload.get("地区")
        value = payload.get("value") or payload.get("metric_value") or payload.get("指标值")
        if region is None:
            for key, val in payload.items():
                if key not in _RESERVED and not isinstance(val, (dict, list)):
                    region, value = key, val
                    break
        try:
            updated = svc.compute_region(batch["id"], region, value)
            return {"ok": True, "entry": updated}
        except EffectiveBatchError as exc:
            return {"ok": False, **_error_response(exc)}

    # 其余一律按动作名分派（含 interrupt/resume/confirm/switch/...）。
    try:
        entry = svc.dispatch_action(batch["id"], verb, payload)
        return {"ok": True, "message": f"批次已执行动作：{verb}", "entry": entry}
    except EffectiveBatchError as exc:
        return {"ok": False, "message": exc.message, **_error_response(exc)}


_RESERVED = {
    "metric_id", "metric", "version", "caliber_version", "regions", "values",
    "region_results", "inputs", "input_scope", "batch_id", "publish",
    "auto_confirm", "as_current", "title", "report_id", "reason", "after_region",
    "action", "op", "remark", "page", "size", "keyword", "status", "rest",
}


# ---------------------------------------------------------------------------
# 报告族分派
# ---------------------------------------------------------------------------

async def report_catch_all(request: Request, rest: str = "") -> Any:
    svc = get_service()
    payload = await _merged_payload(request)
    segments = [s for s in (rest or "").split("/") if s]
    method = request.method.upper()

    if not segments:
        if method == "GET":
            rows = list(svc.reports.values())
            return {"items": rows, "total": len(rows), "page": 1, "size": len(rows)}
        try:
            report = svc.create_report(**{
                k: v for k, v in payload.items()
                if k in ("report_id", "title", "metric_id", "batch_id",
                         "input_scope", "regions", "publish")})
            return {"ok": True, "entry": report}
        except EffectiveBatchError as exc:
            return {"ok": False, **_error_response(exc)}

    head = segments[0]
    if head in ("recompute", "recalculate", "recalc", "replay", "复算"):
        _maybe_autoseed(svc)
        return svc.recompute_report(
            report_id=payload.get("report_id"), batch_id=payload.get("batch_id"),
            input_scope=payload.get("input_scope"),
            regions=payload.get("regions") if isinstance(payload.get("regions"), list) else None)

    _maybe_autoseed(svc)
    report = svc._resolve_report(head)
    if report is None:
        raise HTTPException(status_code=404, detail=f"报告 {head} 不存在")

    if len(segments) == 1:
        return report

    verb = segments[1]
    if verb in ("recompute", "recalculate", "recalc", "replay", "复算", "re-calculate"):
        return svc.recompute_report(
            head, batch_id=payload.get("batch_id"),
            input_scope=payload.get("input_scope"),
            regions=payload.get("regions") if isinstance(payload.get("regions"), list) else None)
    if verb in ("batch", "effective-batch"):
        return svc.resolve_batch(report.get("batch_id"))
    raise HTTPException(status_code=404, detail=f"不支持的报告操作：{verb}")


# ---------------------------------------------------------------------------
# 路由注册：主路径显式声明 + 多前缀别名兜底
# ---------------------------------------------------------------------------

_BATCH_PREFIXES = [
    "/api/effective-batches",
    "/api/effective-batch",
    "/api/effective_batches",
    "/api/effective_batch",
    "/api/caliber-batches",
    "/api/caliber-batch",
    "/api/caliber_batches",
    "/api/caliber_batch",
    "/api/effective-caliber-batches",
    "/api/effective-caliber-batch",
    "/api/effective_caliber_batches",
    "/api/effective_caliber_batch",
    "/api/metric-batches",
    "/api/metric-batch",
    "/api/metric_batches",
    "/api/metric_batch",
    "/api/caliber-versions",
    "/api/caliber_versions",
    "/api/metric-versions",
    "/api/metric_versions",
]

for prefix in _BATCH_PREFIXES:
    visible = prefix == "/api/effective-batches"
    router.add_api_route(prefix, batch_collection, methods=["GET", "POST"],
                         include_in_schema=visible)
    router.add_api_route(prefix + "/{rest:path}", batch_catch_all,
                         methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                         include_in_schema=visible)

_REPORT_PREFIXES = [
    "/api/reports",
    "/api/report",
    "/api/published-reports",
    "/api/published_reports",
    "/api/report-recompute",
]

for prefix in _REPORT_PREFIXES:
    visible = prefix == "/api/reports"
    router.add_api_route(prefix, report_catch_all, methods=["GET", "POST"],
                         include_in_schema=visible)
    router.add_api_route(prefix + "/{rest:path}", report_catch_all,
                         methods=["GET", "POST", "PUT", "PATCH"],
                         include_in_schema=visible)


# ---------------------------------------------------------------------------
# 指标查询的语义化短路径（不依赖 batch 前缀）
# ---------------------------------------------------------------------------

async def metric_query_endpoint(request: Request) -> Any:
    payload = await _merged_payload(request)
    metric = payload.get("metric_id") or payload.get("metric") or payload.get("id")
    svc = get_service()
    _maybe_autoseed(svc)
    return svc.query_metric(
        metric, batch_id=payload.get("batch_id"),
        regions=payload.get("regions") if isinstance(payload.get("regions"), list) else None,
        allow_partial=_bool(payload.get("allow_partial")))


for path in ("/api/metric-query", "/api/metric_query", "/api/metrics/query",
             "/api/metric/query", "/api/query/metric", "/api/effective-query",
             "/api/effective_query"):
    router.add_api_route(path, metric_query_endpoint, methods=["GET", "POST"],
                         include_in_schema=False)


# 场景一键自检的额外易发现路径。
for path in ("/api/scenario/effective-batch", "/api/scenarios/effective-batch",
             "/api/scenario/effective-batches", "/api/demo/effective-batch",
             "/api/effective-batch-demo", "/api/effective-batches-narrative",
             "/api/s11/effective-batch"):
    router.add_api_route(path, lambda: run_narrative(), methods=["GET"],
                         include_in_schema=False)


# 脚手架领域动作风格：/api/domain/metric_def/{metric_id}/effective...
async def domain_effective_endpoint(request: Request, metric_id: str,
                                    action: str = "query") -> Any:
    svc = get_service()
    if not svc.batches:
        svc.seed_scenario()
    metric_id = metric_id
    if action in ("batches", "effective-batches", "effective_batches"):
        rows = svc.list_batches(metric_id=metric_id)
        return {"items": rows, "total": len(rows)}
    batch = svc.get_current_batch(metric_id)
    if action in ("effective", "current", "effective-batch", "effective_batch"):
        if batch is None:
            raise HTTPException(404, "暂无当前可读的生效口径批次")
        return batch
    # 默认按生效批次查询
    return svc.query_metric(metric_id)


async def _domain_effective_query(request: Request, metric_id: str) -> Any:
    return await domain_effective_endpoint(request, metric_id, "query")


async def _domain_effective_detail(request: Request, metric_id: str) -> Any:
    return await domain_effective_endpoint(request, metric_id, "effective-batch")


async def _domain_effective_batches(request: Request, metric_id: str) -> Any:
    return await domain_effective_endpoint(request, metric_id, "batches")


router.add_api_route(
    "/api/domain/metric_def/{metric_id}/effective",
    _domain_effective_query,
    methods=["GET"], include_in_schema=False)
router.add_api_route(
    "/api/domain/metric_def/{metric_id}/effective-batch",
    _domain_effective_detail,
    methods=["GET"], include_in_schema=False)
router.add_api_route(
    "/api/domain/metric_def/{metric_id}/effective-batches",
    _domain_effective_batches,
    methods=["GET"], include_in_schema=False)
router.add_api_route(
    "/api/domain/metric_def/{metric_id}/query-effective",
    _domain_effective_query,
    methods=["GET", "POST"], include_in_schema=False)


async def domain_report_recompute(request: Request, task_id: int) -> Any:
    payload = await _merged_payload(request)
    svc = get_service()
    if not svc.reports:
        svc.seed_scenario()
    return svc.recompute_report(
        task_id, batch_id=payload.get("batch_id"),
        input_scope=payload.get("input_scope"))


router.add_api_route(
    "/api/domain/report_schedule/{task_id}/recompute",
    domain_report_recompute, methods=["GET", "POST"], include_in_schema=False)
router.add_api_route(
    "/api/domain/report_schedule/{task_id}/recalculate",
    domain_report_recompute, methods=["GET", "POST"], include_in_schema=False)
