"""命名别名模块：真实实现见 :mod:`app.services.effective_batch`。"""
from app.services.effective_batch import *  # noqa: F401,F403
from app.services.effective_batch import (  # noqa: F401
    EffectiveBatchError, EffectiveBatchService, get_service, reset_service, service,
)

EffectiveCaliberService = EffectiveBatchService
EffectiveCaliberBatchService = EffectiveBatchService


def __getattr__(name):
    from app.services import effective_batch as _core
    return getattr(_core, name)
