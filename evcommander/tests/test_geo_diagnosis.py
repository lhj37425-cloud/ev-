"""GEO diagnosis plans and alerts.

Covers the diagnosis estimate/start/detail contract, AI analysis parsing and
failure refunds, the three-month plan data, diagnosis sharing, generated
alerts and their status changes, and tenant isolation.

Run from evcommander/:
    python -m unittest discover -s tests -v
"""

from contextvars import copy_context
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import geo
import geo_diagnosis
import geo_runs
import payments
import tenancy


PROJECT = {
    "brand_name": "云杉科技", "aliases": ["云杉"], "website": "https://example.com",
    "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
    "market": "cn", "locales": [],
}
QUESTIONS = [
    {"text": "企业软件哪家好？", "intent": "compare", "locale": "zh", "layer": "category"},
    {"text": "跨境工具选哪家？", "intent": "purchase", "locale": "zh"},
]
ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN", "EV_POWER_CHARGE")


class GeoDiagnosisBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["EV_PAY_MOCK"] = "0"
        os.environ.pop("EV_SEED_DEMO", None)
        os.environ.pop("EV_TRIAL_ORIGIN", None)
        os.environ.pop("EV_POWER_CHARGE", None)
        tenancy.activate(tenancy.local_identity())
        self.project_id = geo.write("POST", geo.PROJECTS, PROJECT)["project"]["id"]
        geo.write("POST", self.path("questions"), {"items": QUESTIONS})
        geo.write("POST", self.path("competitors"), {
            "items": [{"name": "甲科技", "aliases": ["甲"]}],
        })
        self.question_ids = {}
        with commander.database() as db:
            geo_runs.initialize(db)
            rows = db.execute(
                "SELECT id, text FROM geo_questions WHERE project_id=? AND tenant_id=?",
                (self.project_id, commander.tenant()),
            ).fetchall()
            self.question_ids = {row["text"]: row["id"] for row in rows}

    def tearDown(self):
        tenancy.activate(None)
        commander.DATA_DIR = self.old_dir
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def path(self, suffix=""):
        base = f"/api/geo/projects/{self.project_id}"
        return f"{base}/{suffix}" if suffix else base

    def add_run(self, answers, created_at=None, failed=0, status="done"):
        stamp = created_at or geo_runs.now()
        with commander.database() as db:
            geo_runs.initialize(db)
            run_id = db.execute(
                "INSERT INTO geo_runs (tenant_id,project_id,status,engines,samples,total,done,"
                "failed,credits,created_at,started_at,finished_at,method_note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (commander.tenant(), self.project_id, status, '["doubao","deepseek"]', 1,
                 len(answers) + failed, len(answers), failed, 0.4,
                 stamp, stamp, stamp, geo_runs.METHOD_NOTE),
            ).lastrowid
            for question, engine, text in answers:
                db.execute(
                    "INSERT INTO geo_answers (tenant_id,project_id,run_id,question_id,engine_key,"
                    "engine_name,attempt,status,text,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (commander.tenant(), self.project_id, run_id,
                     self.question_ids[question], engine, engine, 0, "ok", text, "", stamp),
                )
        return run_id

    def ai_payload(self):
        return json.dumps({
            "positioning": "云杉科技做企业协作软件。",
            "buyer": "中小企业负责人，关心价格和远程办公。",
            "criteria": [{"name": "价格", "detail": "预算敏感"},
                         {"name": "协作能力", "detail": "远程办公"}],
            "questions": [
                {"id": self.question_ids["企业软件哪家好？"], "intent": "选型比较"},
                {"id": self.question_ids["跨境工具选哪家？"], "intent": "采购决策"},
            ],
            "answer_shape": "AI 会同时推荐云杉科技和甲科技。",
            "summary": "先补跨境问题，再补品牌事实。",
        }, ensure_ascii=False)

    def start_and_wait(self, run_id, ai=None, force=False, side_effect=None):
        if side_effect is not None:
            patcher = mock.patch.object(commander, "generate", side_effect=side_effect)
        else:
            patcher = mock.patch.object(commander, "generate",
                                        return_value=[ai if ai is not None else self.ai_payload()])
        def inline_thread(*, target, args, daemon):
            # Keep the worker identity isolated as it would be in a real thread.
            return mock.Mock(start=lambda: copy_context().run(target, *args))

        with patcher, mock.patch.object(geo_diagnosis.threading, "Thread", inline_thread):
            started = geo.write("POST", self.path("diagnoses"),
                                {"run_id": run_id, "force": force})
            diagnosis_id = started["diagnosis"]["id"]
            detail = geo.read(f"/api/geo/diagnoses/{diagnosis_id}", {})["diagnosis"]
        self.assertNotEqual(detail["status"], "running")
        return started, detail


class DiagnosisData(GeoDiagnosisBase):
    def test_estimate_requires_completed_run(self):
        estimate = geo.read(self.path("diagnoses/estimate"), {})["estimate"]
        self.assertTrue(estimate["blockers"])
        self.assertFalse(estimate["enough"])
        running = self.add_run([("企业软件哪家好？", "doubao", "推荐云杉科技")],
                               status="running")
        estimate = geo.read(self.path("diagnoses/estimate"),
                            {"run_id": [str(running)]})["estimate"]
        self.assertTrue(estimate["blockers"])
        empty = self.add_run([])
        estimate = geo.read(self.path("diagnoses/estimate"),
                            {"run_id": [str(empty)]})["estimate"]
        self.assertIn("没有有效回答", " ".join(estimate["blockers"]))

    def test_start_generates_data_and_ai_sections(self):
        run_id = self.add_run([
            ("企业软件哪家好？", "doubao", "推荐云杉科技，甲科技也不错"),
            ("企业软件哪家好？", "deepseek", "云杉科技很稳"),
            ("跨境工具选哪家？", "doubao", "推荐甲科技和乙科技"),
        ])
        estimate = geo.read(self.path("diagnoses/estimate"),
                            {"run_id": [str(run_id)]})["estimate"]
        self.assertEqual(estimate["blockers"], [])
        self.assertTrue(estimate["enough"])
        self.assertGreater(estimate["credits"], 0)
        self.assertIsNone(estimate["existing"])

        started, detail = self.start_and_wait(run_id)
        self.assertEqual(started["diagnosis"]["status"], "running")
        self.assertEqual(detail["status"], "done")
        self.assertEqual(detail["ai_status"], "ok")
        self.assertIsNotNone(detail["data"])
        data = detail["data"]
        self.assertIn("诊断与优化方案", data["title"])
        self.assertEqual(data["scope"]["ok"], 3)
        self.assertEqual(data["scope"]["total"], 3)
        self.assertEqual(data["scope"]["questions"], 2)
        self.assertEqual(data["headline"]["mention_rate"], 0.6667)
        self.assertEqual(data["ai"]["positioning"], "云杉科技做企业协作软件。")
        self.assertEqual(len(data["ai"]["criteria"]), 2)
        self.assertEqual({item["id"] for item in data["ai"]["questions"]},
                         set(self.question_ids.values()))
        self.assertEqual(len(data["questions"]), 2)
        self.assertIn({"name": "甲科技"},
                      [item for row in data["questions"] for item in row["competitors"]])
        self.assertTrue(any(item["kind"] == "self" for item in data["candidates"]))
        self.assertTrue(any(item["kind"] == "competitor" for item in data["candidates"]))
        self.assertEqual(data["layers"]["layered"], 1)
        self.assertEqual(len(data["plan"]["months"]), 3)
        self.assertEqual(data["plan"]["volume"]["total_12w"], 36)
        self.assertEqual(data["targets"]["rows"][-1]["weeks"], 12)
        self.assertIsNone(data["targets"]["rows"][-1]["target"])
        self.assertTrue(data["gaps"])
        self.assertTrue(data["examples"])
        self.assertTrue(data["boundaries"])
        self.assertGreater(detail["credits"], 0)
        self.assertGreater(estimate["credits"], 0)

        listing = geo.read(self.path("diagnoses"), {"page": ["1"], "page_size": ["8"]})
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["status_name"], "已完成")
        estimate = geo.read(self.path("diagnoses/estimate"),
                            {"run_id": [str(run_id)]})["estimate"]
        self.assertIsNotNone(estimate["existing"])

    def test_ai_failure_still_returns_data_and_refunds(self):
        run_id = self.add_run([
            ("企业软件哪家好？", "doubao", "推荐云杉科技"),
            ("跨境工具选哪家？", "doubao", "推荐甲科技"),
        ])
        with commander.database() as db:
            before = payments.balance_in(db)
        started, detail = self.start_and_wait(
            run_id, side_effect=commander.ChatError("模型暂不可用"))
        self.assertEqual(detail["status"], "done")
        self.assertEqual(detail["ai_status"], "failed")
        self.assertIsNone(detail["data"]["ai"])
        self.assertIn("分析段", detail["data"]["ai_note"])
        self.assertEqual(detail["credits"], 0)
        with commander.database() as db:
            after = payments.balance_in(db)
        self.assertEqual(before, after)

    def test_existing_diagnosis_and_force(self):
        run_id = self.add_run([("企业软件哪家好？", "doubao", "推荐云杉科技")])
        _, first = self.start_and_wait(run_id)
        second = geo.write("POST", self.path("diagnoses"), {"run_id": run_id})
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "exists")
        started, third = self.start_and_wait(run_id, force=True)
        self.assertTrue(started["ok"])
        self.assertEqual(third["status"], "done")
        self.assertNotEqual(third["id"], first["id"])

    def test_running_diagnosis_is_reused(self):
        run_id = self.add_run([("企业软件哪家好？", "doubao", "推荐云杉科技")])
        with commander.database() as db:
            geo_diagnosis.initialize(db)
            key = db.execute(
                "INSERT INTO geo_diagnoses (tenant_id,project_id,run_id,status,created_at) "
                "VALUES (?,?,?,?,?)",
                (commander.tenant(), self.project_id, run_id, "running", geo_runs.now()),
            ).lastrowid
        result = geo.write("POST", self.path("diagnoses"), {"run_id": run_id})
        self.assertTrue(result["reused"])
        self.assertEqual(result["diagnosis"]["id"], key)

    def test_share_snapshot_and_public_access(self):
        run_id = self.add_run([("企业软件哪家好？", "doubao", "推荐云杉科技")])
        _, detail = self.start_and_wait(run_id)
        key = detail["id"]
        share = geo.write("POST", self.path("report-shares"), {
            "period": "diag", "days": 7, "diagnosis_id": key,
        })["share"]
        self.assertEqual(share["period_name"], "诊断方案")
        status, data = geo.public("/api/public/geo/report/" + share["token"])
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["report"]["kind"], "diagnosis")
        self.assertIn("诊断与优化方案", data["report"]["title"])

        with commander.database() as db:
            geo_diagnosis.initialize(db)
            pending = db.execute(
                "INSERT INTO geo_diagnoses (tenant_id,project_id,run_id,status,created_at) "
                "VALUES (?,?,?,?,?)",
                (commander.tenant(), self.project_id, run_id, "running", geo_runs.now()),
            ).lastrowid
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("report-shares"), {
                "period": "diag", "days": 7, "diagnosis_id": pending,
            })

    def test_alerts_from_drop_and_rising_competitor(self):
        previous = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        self.add_run([
            ("企业软件哪家好？", "doubao", "推荐云杉科技"),
            ("跨境工具选哪家？", "doubao", "云杉科技不错"),
            ("企业软件哪家好？", "deepseek", "云杉科技很稳"),
        ], created_at=previous)
        run_id = self.add_run([
            ("企业软件哪家好？", "doubao", "推荐甲科技"),
            ("企业软件哪家好？", "deepseek", "甲科技更合适"),
            ("跨境工具选哪家？", "doubao", "推荐云杉科技"),
            ("跨境工具选哪家？", "deepseek", "推荐云杉科技，甲科技也不错"),
        ])
        _, detail = self.start_and_wait(run_id)
        self.assertEqual(detail["status"], "done")
        alerts = geo.read(self.path("alerts"), {"status": ["open"]})
        kinds = {item["kind"]: item for item in alerts["items"]}
        self.assertIn("mention_drop", kinds)
        self.assertIn("competitor_up", kinds)
        self.assertEqual(kinds["mention_drop"]["level"], "danger")
        self.assertEqual(kinds["mention_drop"]["detail"]["prev"], 1.0)
        self.assertAlmostEqual(kinds["mention_drop"]["detail"]["cur"], 0.5)
        self.assertEqual(kinds["competitor_up"]["detail"]["items"][0]["name"], "甲科技")
        self.assertGreater(kinds["competitor_up"]["detail"]["items"][0]["delta"], 0.2)

        alert_id = kinds["mention_drop"]["id"]
        updated = geo.write("PUT", f"/api/geo/alerts/{alert_id}", {"status": "handled"})
        self.assertEqual(updated["alert"]["status_name"], "已处理")
        open_items = geo.read(self.path("alerts"), {"status": ["open"]})["items"]
        self.assertNotIn(alert_id, [item["id"] for item in open_items])
        handled = geo.read(self.path("alerts"), {"status": ["handled"]})["items"]
        self.assertIn(alert_id, [item["id"] for item in handled])
        with self.assertRaises(commander.ChatError):
            geo.write("PUT", f"/api/geo/alerts/{alert_id}", {"status": "nope"})

    def test_diagnoses_and_alerts_are_tenant_scoped(self):
        run_id = self.add_run([("企业软件哪家好？", "doubao", "推荐云杉科技")])
        _, detail = self.start_and_wait(run_id)
        self.assertEqual(detail["status"], "done")
        other = tenancy.create_tenant("诊断测试租户")
        with tenancy.scope(other):
            with self.assertRaises(commander.ChatError):
                geo.read(f"/api/geo/diagnoses/{detail['id']}", {})
            with self.assertRaises(commander.ChatError):
                geo.read(self.path("diagnoses"), {})
            with self.assertRaises(commander.ChatError):
                geo.read(self.path("alerts"), {})

    def test_route_claims(self):
        project = f"/api/geo/projects/{self.project_id}"
        self.assertTrue(geo_diagnosis.can_read(f"{project}/diagnoses"))
        self.assertTrue(geo_diagnosis.can_read(f"{project}/diagnoses/estimate"))
        self.assertTrue(geo_diagnosis.can_read(f"{project}/alerts"))
        self.assertTrue(geo_diagnosis.can_read("/api/geo/diagnoses/1"))
        self.assertTrue(geo_diagnosis.can_write(f"{project}/diagnoses"))
        self.assertTrue(geo_diagnosis.can_write("/api/geo/alerts/1"))
        self.assertFalse(geo_diagnosis.can_write("/api/geo/diagnoses/1"))


if __name__ == "__main__":
    unittest.main()
