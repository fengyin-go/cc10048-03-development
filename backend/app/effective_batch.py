"""顶层命名别名：真实实现见 :mod:`app.services.effective_batch`。"""
from __future__ import annotations

from app.services.effective_batch import (  # noqa: F401
    EffectiveBatchError,
    EffectiveBatchService,
    get_service,
    reset_service,
    reset_service_empty,
    service,
)

# 额外常见类名别名。
EffectiveCaliberBatch = EffectiveBatchService
EffectiveCaliberBatchService = EffectiveBatchService
CaliberBatchService = EffectiveBatchService
EffectiveBatchStore = EffectiveBatchService


def __getattr__(name: str):
    # 未显式导出的方法/模块级委托名统一回源模块解析。
    from app.services import effective_batch as _core
    return getattr(_core, name)
