"""命名别名模块：真实实现见 :mod:`app.services.effective_batch`。"""
from __future__ import annotations

from app.services import effective_batch as _core
from app.services.effective_batch import (  # noqa: F401
    EffectiveBatchError,
    EffectiveBatchService,
    get_service,
    reset_service,
    service,
)

globals().update({k: v for k, v in vars(_core).items() if not k.startswith("__")})

EffectiveCaliberBatchService = _core.EffectiveBatchService
CaliberBatchService = _core.EffectiveBatchService
EffectiveCaliberBatch = _core.EffectiveBatchService


def __getattr__(name: str):
    return getattr(_core, name)
