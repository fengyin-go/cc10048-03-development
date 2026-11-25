"""生效口径批次核心不变量回归测试。

完整复现题述场景：
- 旧版 v1：华东 20、华南 40，当前生效；
- 新版 v2 只算出华东 22 后中断，华南待做；
- 中断期查询只能读到同一旧批次的完整地区结果；
- 恢复时只补算 pending 范围，齐备后批次可读、发布时点固定；
- 最后切换后当前查询整批来自新版，旧版归档；
- 已发布旧报告仍按其锚定批次与输入范围复算为 20/40。
"""
from __future__ import annotations

import pytest

from app.services.effective_batch import (
    STATUS_ARCHIVED,
    STATUS_PREPARING,
    STATUS_READABLE,
    EffectiveBatchError,
    EffectiveBatchService,
)


@pytest.fixture()
def svc():
    service = EffectiveBatchService()
    service.seed_scenario()
    return service


class TestInterruptedReadConsistency:
    def test_seed_progress(self, svc):
        progress = svc.progress("new")
        assert progress["processed_regions"] == ["华东"]
        assert progress["pending_regions"] == ["华南"]
        assert progress["interrupted"] is True
        assert str(progress["status"]) == STATUS_PREPARING

    def test_query_serves_only_complete_current_batch(self, svc):
        result = svc.query_metric(metric_id="M-REGION")
        assert result["ok"] is True
        assert result["batch_id"] == "EB-V1-20261125"
        assert result["regions"] == {"华东": 20, "华南": 40}
        # 每个地区行都来自同一个批次，杜绝新旧拼接。
        assert {row["batch_id"] for row in result["rows"]} == {"EB-V1-20261125"}
        assert result["same_batch"] is True
        assert 22 not in result["regions"].values()

    def test_preparing_batch_not_exposable(self, svc):
        result = svc.query_metric(batch_id="EB-V2-20261125")
        assert result["ok"] is False
        assert result["code"] == "BATCH_NOT_READABLE"
        assert result["pending_regions"] == ["华南"]
        # 显式 allow_partial 时才能看到半份结果。
        partial = svc.query_metric(batch_id=2, allow_partial=True)
        assert partial["regions"] == {"华东": 22}

    def test_cannot_switch_incomplete_batch(self, svc):
        with pytest.raises(EffectiveBatchError):
            svc.switch_current_batch("new")
        # 当前批次不受影响。
        assert svc.get_current_batch("M-REGION")["batch_id"] == "EB-V1-20261125"


class TestResumeAndSwitch:
    def test_resume_only_pending_and_confirm(self, svc):
        old_published = svc.resolve_batch("new")["published_at"]
        assert old_published is None  # 准备中的新批次还没有实际发布时点

        result = svc.resume_aggregation("new", values={"华南": 40})
        assert result["computed_regions"] == ["华南"]
        assert result["skipped_regions"] == ["华东"]

        new_batch = svc.resolve_batch("new")
        assert new_batch["regions"] == {"华东": 22, "华南": 40}
        assert str(new_batch["status"]) == STATUS_READABLE
        assert new_batch["published_at"]
        published_at = new_batch["published_at"]

        # 再次恢复是幂等的，不重算华东，也不改写发布时点。
        again = svc.resume_aggregation("new")
        assert again["computed_regions"] == []
        assert again["skipped_regions"] == ["华东", "华南"]
        assert new_batch["published_at"] == published_at

    def test_current_stays_old_before_switch(self, svc):
        svc.resume_aggregation("new", values={"华南": 40})
        current = svc.query_metric("M-REGION")
        assert current["batch_id"] == "EB-V1-20261125"

    def test_switch_makes_atomic_new_reads_and_archives_old(self, svc):
        svc.resume_aggregation("new", values={"华南": 40})
        svc.switch_current_batch("new")

        result = svc.query_metric("M-REGION")
        assert result["batch_id"] == "EB-V2-20261125"
        assert result["regions"]["华东"] == 22
        assert result["regions"]["华南"] == 40
        assert len({row["batch_id"] for row in result["rows"]}) == 1

        old = svc.resolve_batch("old")
        assert str(old["status"]) == STATUS_ARCHIVED
        assert old["is_current"] is False

    def test_archived_batch_still_queryable_by_id(self, svc):
        svc.resume_aggregation("new", values={"华南": 40})
        svc.switch_current_batch("new")
        old_result = svc.query_metric(batch_id="EB-V1-20261125")
        assert old_result["ok"] is True
        assert old_result["regions"] == {"华东": 20, "华南": 40}


class TestReportRecompute:
    def test_old_report_uses_pinned_batch_after_switch(self, svc):
        svc.resume_aggregation("new", values={"华南": 99})
        svc.switch_current_batch("new")

        recomputed = svc.recompute_report("R-OLD")
        assert recomputed["ok"] is True
        assert recomputed["batch_id"] == "EB-V1-20261125"
        assert recomputed["regions"] == {"华东": 20, "华南": 40}
        assert recomputed["total"] == 60

    def test_recompute_respects_input_scope(self, svc):
        # 只复算报告输入范围内的华东。
        result = svc.recompute_report(
            batch_id="EB-V1-20261125",
            input_scope={"regions": ["华东"], "date": "2026-11-25"})
        assert result["regions"] == {"华东": 20}
        assert result["total"] == 20


class TestStatusAliases:
    def test_chinese_and_english_equality(self, svc):
        assert svc.resolve_batch("old")["status"] == "readable"
        assert svc.resolve_batch("old")["status"] == "可读"
        svc.resume_aggregation("new", values={"华南": 40})
        assert svc.resolve_batch("new")["status"] == "readable"
        svc.switch_current_batch("new")
        assert svc.resolve_batch("old")["status"] == "archived"
        assert svc.resolve_batch("old")["status"] == "已归档"
        assert svc.resolve_batch("new")["status"] != STATUS_ARCHIVED


class TestPersistenceRestart:
    def test_published_at_survives_restart(self, tmp_path):
        path = tmp_path / "batches.json"
        first = EffectiveBatchService(persist_path=path)
        first.seed_scenario()
        first.resume_aggregation("new", values={"华南": 40})
        published_at = first.resolve_batch("new")["published_at"]
        first.switch_current_batch("new")
        first.save()

        restarted = EffectiveBatchService.recover(path)
        assert restarted.resolve_batch("new")["published_at"] == published_at
        current = restarted.query_metric("M-REGION")
        assert current["batch_id"] == "EB-V2-20261125"
        assert restarted.recompute_report("R-OLD")["total"] == 60


class TestDimensionDataIngestion:
    def test_group_raw_dimension_rows(self):
        svc = EffectiveBatchService()
        batch = svc.create_batch(metric_id="M1", version="v1",
                                  regions=["华东", "华南"])
        svc.ingest_dimension_data(batch["id"], [
            {"地区": "华东", "金额": 8},
            {"地区": "华东", "金额": 12},
            {"地区": "华南", "金额": 15},
            {"地区": "华南", "金额": 25},
        ])
        assert batch["regions"] == {"华东": 20, "华南": 40}
        assert batch["all_regions_ready"] is True
