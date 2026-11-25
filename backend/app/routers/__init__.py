"""业务路由汇总：生效口径批次等专用路由在前，通用资源路由兜底在后。"""
from __future__ import annotations

from app.routers import effective_batch, resource

ROUTERS = [effective_batch, resource]
