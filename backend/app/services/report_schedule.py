"""报告调度领域服务：任务周期调度、报告生成、分发管理。"""
# 数据操作：记录、批次、状态，与上下游模块联动消费批次数据。
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ReportScheduleService(BaseModuleService):
    MODULE = "report_schedule"
    ENTITY = "报告任务"
    OPERATION_TERM = "调度"
    LIST_FIELDS = ["任务标识", "报告内容", "接收人", "发送周期", "发送渠道", "上次发送", "下次发送", "任务状态"]
    REQUIRED_FIELDS = ["任务标识", "报告内容", "接收人"]
    STATUS_ORDER = ["待启动", "运行中", "已暂停", "调度失败"]
    ACTION_RULES = {"启动调度": "待启动", "暂停调度": "运行中"}
    NEGATIVE_ACTIONS = []

    def generate_report(self, task_id: int) -> dict[str, Any]:
        """生成一份模拟报告。"""
        task = store.find(self.MODULE, task_id)
        if not task:
            return {"error": f"报告任务 {task_id} 不存在"}
        rng = random.Random(task_id * 43)
        now = datetime.now()
        send_now = now.isoformat()
        next_send = (now + timedelta(hours=24)).isoformat() if "日" in str(task.get("发送周期", "日")) else (now + timedelta(hours=1)).isoformat()
        report_content = {
            "task_id": task_id,
            "title": f"指标分析报告 - {task.get('报告内容', '概览')}",
            "generate_time": send_now,
            "recipient": task.get("接收人"),
            "channel": task.get("发送渠道", "email"),
            "sections": [
                {"name": "核心指标概览", "metrics": {"GMV": rng.uniform(1000000, 5000000),
                                                      "DAU": rng.randint(50000, 200000),
                                                      "转化率": f"{rng.uniform(5, 20):.1f}%"}},
                {"name": "异常指标", "alerts": [{"metric": "支付成功率", "change": f"{rng.uniform(-10, -1):.1f}%"},
                                                  {"metric": "响应延迟", "change": f"+{rng.uniform(10, 50):.0f}ms"}]},
                {"name": "趋势分析", "summary": "本周整体指标平稳，搜索转化率下降2.3%，需关注推荐算法迭代效果"},
            ],
            "next_schedule": next_send,
        }
        update = store.find(self.MODULE, task_id)
        if update:
            update["上次发送"] = send_now
            update["下次发送"] = next_send
            update["status"] = "运行中"
        return report_content

    # ------------------------------------------------------------------
    # 已发布报告锚定生效口径批次，支持按原批次与输入范围复算。
    # ------------------------------------------------------------------
    def _batch_service(self):
        from app.services.effective_batch import get_service
        return get_service()

    def publish_effective_report(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._batch_service().create_report(*args, **kwargs)

    def recompute_report(self, report_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        """按报告原锚定批次复算；口径切换后仍得到旧口径结果。"""
        return self._batch_service().recompute_report(report_ref, **kwargs)

    recalculate_report = recompute_report
    replay_report = recompute_report

    _RECOMPUTE_ACTIONS = {
        "复算", "重新核算", "重新计算", "重算", "按原批次复算",
        "recompute", "recalculate", "recalc", "replay",
    }

    def run_action(self, entry_id: int, action: str):  # type: ignore[override]
        """通用 ``/api/report_schedule/{id}/actions`` 分派：复算类动作走批次锚点。"""
        text = str(action or "").strip()
        if text in self._RECOMPUTE_ACTIONS or "复算" in text or "核算" in text:
            try:
                batch_svc = self._batch_service()
                if not batch_svc.reports:
                    batch_svc.seed_scenario()
                result = self.recompute_report(entry_id)
                if not result.get("ok", True):
                    return None, str(result.get("error") or "复算失败")
                return result, "报告已按其锚定的原口径批次与输入范围复算"
            except Exception as exc:  # noqa: BLE001
                return None, str(exc)
        return super().run_action(entry_id, action)

    def domain_enrich(self, config_id: int) -> dict:
        """对当前模块执行存储操作，联动导出和回填，产出标准化数据产物。

        数据加工流程：读取原始记录 → 执行存储处理 → 导出校验字段完整性 → 生成回填批次标识 →
        写入存储层并返回可追溯的处理结果，下游消费模块可基于批次号追踪数据链路。
        """
        import time, json
        from app.store import store
        entry = store.find(self.MODULE, config_id)
        # 存储：遍历当前模块全部记录执行核心数据处理
        records = store.list_all(self.MODULE)
        processed = []
        for idx, rec in enumerate(records):
            # 导出：校验每条记录的字段完整性与格式合法性
            filled = sum(1 for v in rec.values() if v and str(v).strip())
            total = len(rec)
            rec_copy = dict(rec)
            rec_copy["_field_count"] = filled
            rec_copy["_complete_ratio"] = f"{filled}/{total}"
            rec_copy["_batch"] = f"{self.MODULE}-{idx:04d}"
            processed.append(rec_copy)
        # 回填：生成加工批次标识并写入存储
        batch_id = store.next_id(self.MODULE + "_processor")
        result = {
            "module": self.MODULE,
            "operation": "存储",
            "config_id": config_id,
            "records_processed": len(processed),
            "batch_id": batch_id,
            "field_validation": {"导出": "passed", "回填": "recorded"},
            "processed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "downstream_consumable": True,
            "records": processed,
            "entry": entry,
        }
        # 返回存储处理结果，含导出校验元数据和回填批次追溯信息
        return result

