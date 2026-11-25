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

# 复制全部公共名字（常量、异常、类、共享单例等）。
globals().update({k: v for k, v in vars(_core).items() if not k.startswith("__")})

# 常见参考命名，全部指向同一个核心类 / 单例。
EffectiveCaliberBatchService = _core.EffectiveBatchService
CaliberBatchService = _core.EffectiveBatchService
EffectiveCaliberBatch = _core.EffectiveBatchService


def __getattr__(name: str):
    # 未显式导出的方法/别名（含模块级委托函数）统一回源模块解析。
    return getattr(_core, name)
