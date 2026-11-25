"""pytest 全局夹具：保证生效口径批次共享单例在每个用例前处于干净状态。

- 直接实例化 ``EffectiveBatchService()`` 的纯领域测试不受影响（本就相互隔离）；
- 通过 TestClient 访问共享单例的接口测试，每个用例从空世界开始，
  需要题述场景时可调用 ``/api/effective-batches/scenario``、
  ``/api/effective-batches/demo/run``，或直接访问 ``/current``、
  ``/metric-query``（空世界会自愈播种标准华东/华南场景）。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_effective_batch_singleton():
    try:
        from app.services.effective_batch import get_service
        get_service().reset_empty()
    except Exception:
        # 夹具绝不能影响既有的 18 模块测试套件。
        pass
    yield
