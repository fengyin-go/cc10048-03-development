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

CaliberBatchService = EffectiveBatchService
EffectiveCaliberBatchService = EffectiveBatchService
CaliberBatch = EffectiveBatchService


def __getattr__(name: str):
    from app.services import effective_batch as _core
    return getattr(_core, name)
