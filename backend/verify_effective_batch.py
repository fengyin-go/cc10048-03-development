#!/usr/bin/env python3
"""生效口径批次场景自检脚本。

直接运行::

    cd backend
    .venv/bin/python verify_effective_batch.py

它会严格按题述顺序验证：

1. 聚合中断后取数：只能读到旧批次的完整地区结果（华东 20 / 华南 40），
   新批次仅有华东 22 且状态为“准备”，已处理=[华东]、待做=[华南]；
2. 恢复后、切换前：续算只补华南，新批次齐备后转为“可读”，当前查询仍旧版；
3. 最后切换后：当前查询的全部地区来自新批次（华东 22），旧批次“已归档”；
4. 原报告始终按其锚定的旧批次复算为 华东20/华南40/合计60。

若环境安装了 httpx，还会额外通过 FastAPI TestClient 验证 HTTP 接口。
"""
from __future__ import annotations

import sys

from app.services.effective_batch import EffectiveBatchService


def run_core_checks() -> None:
    svc = EffectiveBatchService()
    svc.seed_scenario()

    print("== 阶段0 种子场景 ==")
    print("旧批次:", svc.resolve_batch("old")["batch_id"],
          svc.resolve_batch("old")["regions"], "status=",
          str(svc.resolve_batch("old")["status"]))
    print("新批次:", svc.resolve_batch("new")["batch_id"],
          svc.resolve_batch("new")["regions"], "status=",
          str(svc.resolve_batch("new")["status"]))

    print("\n== 阶段1 聚合中断后取数 ==")
    q1 = svc.query_metric("M-REGION")
    p1 = svc.progress("new")
    print("当前查询:", q1["batch_id"], q1["regions"])
    print("批次进度: processed=", p1["processed_regions"],
          "pending=", p1["pending_regions"])
    assert q1["batch_id"] == "EB-V1-20261125"
    assert q1["regions"] == {"华东": 20, "华南": 40}
    assert len({row["batch_id"] for row in q1["rows"]}) == 1
    assert p1["processed_regions"] == ["华东"]
    assert p1["pending_regions"] == ["华南"]
    # 新版半份结果不能作为生效结果读出。
    assert svc.query_metric(batch_id="new")["ok"] is False

    print("\n== 阶段2 恢复（只补待做的华南），切换前再取数 ==")
    resumed = svc.resume_aggregation("new", values={"华南": 40})
    print("本次计算:", resumed["computed_regions"],
          "跳过已处理:", resumed["skipped_regions"])
    q2 = svc.query_metric("M-REGION")
    new_batch = svc.resolve_batch("new")
    print("新批次状态:", str(new_batch["status"]),
          "published_at=", new_batch["published_at"])
    print("切换前当前查询仍是:", q2["batch_id"], q2["regions"])
    assert resumed["computed_regions"] == ["华南"]
    assert resumed["skipped_regions"] == ["华东"]
    assert str(new_batch["status"]) == "可读"
    assert q2["batch_id"] == "EB-V1-20261125"

    print("\n== 阶段3 最后切换后取数 ==")
    svc.switch_current_batch("new")
    q3 = svc.query_metric("M-REGION")
    old = svc.resolve_batch("old")
    print("当前查询:", q3["batch_id"], q3["regions"])
    print("各地区行批次:", [row["batch_id"] for row in q3["rows"]])
    print("旧批次状态:", str(old["status"]))
    assert q3["batch_id"] == "EB-V2-20261125"
    assert q3["regions"] == {"华东": 22, "华南": 40}
    assert len({row["batch_id"] for row in q3["rows"]}) == 1
    assert str(old["status"]) == "已归档"

    print("\n== 复算原报告（固定旧批次与输入范围） ==")
    report = svc.recompute_report("R-OLD")
    print("报告批次:", report["batch_id"], report["regions"],
          "total=", report["total"])
    assert report["batch_id"] == "EB-V1-20261125"
    assert report["regions"] == {"华东": 20, "华南": 40}
    assert report["total"] == 60

    print("\n[PASS] 服务层：中断一致性 / 断点续算 / 原子切换 / 旧报告复算 全部成立")


def run_http_checks() -> None:
    try:
        from fastapi.testclient import TestClient
        from app.main import app
    except Exception as exc:  # noqa: BLE001
        print(f"\n[SKIP] HTTP 自检依赖未就绪：{exc}")
        return

    client = TestClient(app)
    resp = client.get("/api/effective-batches/demo/run")
    resp.raise_for_status()
    data = resp.json()
    if not data.get("all_passed"):
        failed = [k for k, ok in data.get("checks", {}).items() if not ok]
        raise AssertionError(f"HTTP 场景检查未通过：{failed}")
    print("[PASS] HTTP 层：/api/effective-batches/demo/run 全部检查成立")

    # 关键别名路径也应可达。
    for method, path in [
        ("GET", "/api/effective_batch/current"),
        ("GET", "/api/caliber-batches/current"),
        ("GET", "/api/metric-query"),
        ("GET", "/api/effective-batches/2/progress"),
        ("POST", "/api/reports/R-OLD/recompute"),
        ("POST", "/api/domain/report_schedule/1/recompute"),
    ]:
        r = client.request(method, path)
        assert r.status_code == 200, (method, path, r.status_code, r.text[:200])
    print("[PASS] HTTP 别名路径（下划线/单数/caliber/domain 风格）均可达")


if __name__ == "__main__":
    run_core_checks()
    run_http_checks()
    print("\n全部自检通过 ✅")
    sys.exit(0)
