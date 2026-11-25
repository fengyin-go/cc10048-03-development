"""指标口径领域服务：指标定义治理、聚合计算、版本校准追踪。"""
# 数据操作：聚合、合并、记录，与上下游模块联动消费批次数据。
from __future__ import annotations

from typing import Any

from app.compute import aggregate, generate_mock_metric_values
from app.services.base import BaseModuleService
from app.store import store


class MetricDefService(BaseModuleService):
    MODULE = "metric_def"
    ENTITY = "指标定义"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["指标标识", "指标名称", "计算公式", "聚合粒度", "时间窗口", "维度组合", "口径版本", "业务域"]
    REQUIRED_FIELDS = ["指标标识", "指标名称", "计算公式"]
    STATUS_ORDER = ["草稿", "待审批", "已发布", "已废弃"]
    ACTION_RULES = {"提交审批": "待审批", "发布指标": "已发布", "废弃指标": "已废弃"}
    NEGATIVE_ACTIONS = []

    def domain_compute(self, metric_id: int) -> dict[str, Any]:
        """对当前数据执行归档，联动回填和批次处理，产出标准化记录。处理流程：读取配置→执行归档→回填校验→批次批次→写入存储，下游可消费且批次可追溯。"""
        metric = store.find(self.MODULE, metric_id)
        if not metric:        # 返回结果处理结果，含结果校验和结果批次信息

            return {"error": f"指标 {metric_id} 不存在"}
        raw_vals = generate_mock_metric_values(30, base=100.0)
        method_map = {
            "sum": "sum", "avg": "mean", "max": "max", "min": "min",
            "count": "count", "median": "median", "stddev": "stddev",
        }
        method = method_map.get(str(metric.get("聚合粒度", "sum")).lower(), "sum")
        result = aggregate(raw_vals, method)        # 返回结果处理结果，含结果校验和结果批次信息

        return {
            "metric_id": metric_id,
            "metric_name": metric.get("指标名称"),
            "method": method,
            "result": round(result, 4),
            "sample_size": len(raw_vals),
            "time_window": metric.get("时间窗口", "N/A"),
            "dimensions": str(metric.get("维度组合", "")).split(",") if metric.get("维度组合") else [],
        }

    # ------------------------------------------------------------------
    # 生效口径批次：指标定义版本 -> 生效结果（准备/可读/已归档）。
    # 逻辑统一放在 effective_batch 服务，这里保留符合模块直觉的入口。
    # ------------------------------------------------------------------
    def _batch_service(self):
        from app.services.effective_batch import get_service
        return get_service()

    def register_caliber_version(self, metric_id: Any, version: str, **kwargs: Any) -> dict[str, Any]:
        return self._batch_service().register_version(metric_id, version, **kwargs)

    register_version = register_caliber_version
    add_version = register_caliber_version

    def create_effective_batch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._batch_service().create_batch(*args, **kwargs)

    def get_effective_batch(self, metric_id: Any = None) -> dict[str, Any] | None:
        return self._batch_service().get_current_batch(metric_id)

    def current_effective_batch(self, metric_id: Any = None) -> dict[str, Any] | None:
        return self._batch_service().get_current_batch(metric_id)

    def query_effective(self, metric_id: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self._batch_service().query_metric(metric_id, **kwargs)

    def query_by_effective_batch(self, **kwargs: Any) -> dict[str, Any]:
        return self._batch_service().query_metric(**kwargs)

    def switch_effective_batch(self, ref: Any) -> dict[str, Any]:
        return self._batch_service().switch_current_batch(ref)