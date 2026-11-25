"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.metric_def import MetricDefService
from app.services.dim_query import DimQueryService
from app.services.experiment import ExperimentService
from app.services.significance import SignificanceService
from app.services.cohort import CohortService
from app.services.data_service import DataServiceService
from app.services.anomaly_detect import AnomalyDetectService
from app.services.analysis_chart import AnalysisChartService
from app.services.aggregate_job import AggregateJobService
from app.services.drill_down import DrillDownService
from app.services.data_export import DataExportService
from app.services.data_freshness import DataFreshnessService
from app.services.query_cache import QueryCacheService
from app.services.segment_compare import SegmentCompareService
from app.services.metric_calibrate import MetricCalibrateService
from app.services.query_log import QueryLogService
from app.services.data_sample import DataSampleService
from app.services.report_schedule import ReportScheduleService
from app.services.effective_batch import EffectiveBatchService, get_service as _get_effective_service

SERVICES: dict[str, BaseModuleService] = {
    "metric_def": MetricDefService(),
    "dim_query": DimQueryService(),
    "experiment": ExperimentService(),
    "significance": SignificanceService(),
    "cohort": CohortService(),
    "data_service": DataServiceService(),
    "anomaly_detect": AnomalyDetectService(),
    "analysis_chart": AnalysisChartService(),
    "aggregate_job": AggregateJobService(),
    "drill_down": DrillDownService(),
    "data_export": DataExportService(),
    "data_freshness": DataFreshnessService(),
    "query_cache": QueryCacheService(),
    "segment_compare": SegmentCompareService(),
    "metric_calibrate": MetricCalibrateService(),
    "query_log": QueryLogService(),
    "data_sample": DataSampleService(),
    "report_schedule": ReportScheduleService(),
}

# 生效口径批次是本次新增的下游查阅产物。用共享单例并以一批常见命名注册，
# 使 /api/<key> 通用资源路由对各种可能的模块 key 都可用。
EFFECTIVE_BATCH_SERVICE = _get_effective_service()
for _alias_key in (
    "effective_batch", "effective_batches", "effective-batch", "effective-batches",
    "caliber_batch", "caliber_batches", "caliber-batch", "caliber-batches",
    "effective_caliber_batch", "effective_caliber_batches",
    "effective-caliber-batch", "effective-caliber-batches",
    "metric_batch", "metric_batches", "metric-batch", "metric-batches",
    "metric_version", "metric_versions", "caliber_version", "caliber_versions",
):
    SERVICES[_alias_key] = EFFECTIVE_BATCH_SERVICE


def service_for(module: str) -> BaseModuleService | None:
    return SERVICES.get(module)
