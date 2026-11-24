"""GEO weekly/monthly reports and public report sharing.

Covers the report shape used by the report/print pages, period aggregation,
delta against the previous period, gaps/findings/excerpts, share lifecycle,
expiry, revocation, matrix snapshots, tenant isolation and public access.

Run from evcommander/:
    python -m unittest discover -s tests -v
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import geo
import geo_report
import geo_runs
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
ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN")


class GeoReportBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["EV_PAY_MOCK"] = "0"
        os.environ.pop("EV_SEED_DEMO", None)
        os.environ.pop("EV_TRIAL_ORIGIN", None)
        tenancy.activate(tenancy.local_identity())
        result = geo.write("POST", geo.PROJECTS, PROJECT)
        self.project_id = result["project"]["id"]
        geo.write("POST", self.path("questions"), {"items": QUESTIONS})
        geo.write("POST", self.path("competitors"), {
            "items": [{"name": "甲科技", "aliases": ["甲"]}],
        })

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

    def add_run(self, answers, created_at=None, credits=0.4, status="done"):
        stamp = created_at or geo_runs.now()
        with commander.database() as db:
            geo_runs.initialize(db)
            questions = {
                row["text"]: row["id"] for row in db.execute(
                    "SELECT id, text FROM geo_questions WHERE project_id=? AND tenant_id=?",
                    (self.project_id, commander.tenant()),
                ).fetchall()
            }
            run_id = db.execute(
                "INSERT INTO geo_runs (tenant_id,project_id,status,engines,samples,total,done,"
                "credits,created_at,started_at,finished_at,method_note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (commander.tenant(), self.project_id, status, '["doubao"]', 1,
                 len(answers), len(answers) if status == "done" else 0, credits,
                 stamp, stamp, stamp, geo_runs.METHOD_NOTE),
            ).lastrowid
            for question, text in answers:
                db.execute(
                    "INSERT INTO geo_answers (tenant_id,project_id,run_id,question_id,engine_key,"
                    "engine_name,attempt,status,text,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (commander.tenant(), self.project_id, run_id, questions[question], "doubao",
                     "豆包（模拟）", 0, "ok", text, "", stamp),
                )
        return run_id

    def report(self, period="week", offset=0):
        return geo.read(self.path("reports"), {
            "period": [period], "offset": [str(offset)],
        })["report"]

    def share(self, period="week", offset=0, days=7):
        return geo.write("POST", self.path("report-shares"), {
            "period": period, "offset": offset, "days": days,
        })["share"]


class ReportData(GeoReportBase):
    def test_empty_report_shape(self):
        report = self.report()
        self.assertEqual(report["runs"], 0)
        self.assertEqual(report["current"]["sample_n"], 0)
        self.assertEqual(len(report["trend"]), 7)
        self.assertEqual(report["engines"], [])
        self.assertEqual(report["questions"], [])
        self.assertEqual(report["intents"], [])
        self.assertIsNone(report["latest"])
        self.assertEqual(report["gaps"], [])
        self.assertEqual(report["gap_total"], 0)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["examples"], [])
        self.assertIn("模拟", report["note"])
        self.assertEqual(report["alerts"], {"total": 0, "kinds": []})
        self.assertIsNone(report["publishing"])
        self.assertIsNone(report["cited"])

    def test_week_report_aggregates_answers(self):
        self.add_run([
            ("企业软件哪家好？", "推荐云杉科技，甲科技也不错"),
            ("企业软件哪家好？", "云杉科技很好"),
            ("跨境工具选哪家？", "推荐甲科技和乙科技"),
        ])
        report = self.report("week", 0)
        self.assertEqual(report["runs"], 1)
        self.assertEqual(report["credits"], 0.4)
        self.assertEqual(report["project"]["brand_name"], "云杉科技")
        current = report["current"]
        self.assertEqual(current["sample_n"], 3)
        self.assertEqual(current["mentioned_n"], 2)
        self.assertEqual(current["mention_rate"], 0.6667)
        self.assertEqual(current["top1_rate"], 0.6667)
        self.assertEqual(current["sov"], 0.5)
        self.assertIsNone(current["accuracy"])
        self.assertIsNone(report["delta"]["mention_rate"])

        self.assertEqual(len(report["engines"]), 1)
        engine = report["engines"][0]
        self.assertEqual(engine["engine"]["key"], "doubao")
        self.assertEqual(engine["sample_n"], 3)
        self.assertEqual(engine["mention_rate"], 0.6667)
        self.assertEqual(engine["tier"]["level"], "ok")

        questions = {row["question"]: row for row in report["questions"]}
        self.assertEqual(questions["企业软件哪家好？"]["sample_n"], 2)
        self.assertEqual(questions["企业软件哪家好？"]["best_rank"], 1)
        self.assertEqual(questions["跨境工具选哪家？"]["mention_rate"], 0)
        self.assertIsNone(questions["跨境工具选哪家？"]["best_rank"])

        layers = report["layers"]
        self.assertEqual(layers["layered"], 1)
        self.assertEqual(layers["unlayered"], 1)
        self.assertEqual(layers["rows"][0]["layer"], "category")
        self.assertEqual(layers["rows"][0]["sample_n"], 2)

        intents = {row["intent"]: row for row in report["intents"]}
        self.assertEqual(intents["compare"]["sample_n"], 2)
        self.assertEqual(intents["purchase"]["sample_n"], 1)

        self.assertEqual(report["gap_total"], 1)
        self.assertEqual(report["gaps"][0]["question"], "跨境工具选哪家？")
        self.assertIn("竞品占住", " ".join(item["finding"] for item in report["findings"]))

        today = [item for item in report["trend"] if item["mention_rate"] is not None]
        self.assertEqual(len(today), 1)
        self.assertEqual(today[0]["date"], datetime.now(timezone.utc).date().isoformat())
        self.assertEqual(today[0]["mention_rate"], 0.6667)

        latest = report["latest"]
        self.assertEqual(latest["overall"]["sample_n"], 3)
        self.assertEqual(latest["questions"], 2)

        example = report["examples"][0]
        self.assertTrue(example["mentioned"])
        self.assertEqual(example["rank_no"], 1)
        self.assertIn("云杉科技", example["excerpt"]["text"])
        kinds = {mark["kind"] for mark in example["excerpt"]["marks"]}
        self.assertEqual(kinds, {"self", "competitor"})

        overview = geo.read(self.path("overview"), {})
        self.assertEqual(overview["current"]["sample_n"], 3)
        self.assertEqual(overview["current"]["mentioned_n"], 2)
        self.assertEqual(overview["counts"]["questions"], 2)
        self.assertEqual(overview["last_run"]["questions"], 2)
        self.assertEqual(len(overview["trend"]), 30)
        self.assertEqual(overview["engines"][0]["engine"]["key"], "doubao")
        self.assertEqual(overview["alerts_open"], 0)

    def test_report_delta_against_previous_week(self):
        previous = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        self.add_run([
            ("企业软件哪家好？", "推荐甲科技"),
            ("企业软件哪家好？", "甲科技更合适"),
            ("企业软件哪家好？", "推荐甲科技"),
        ], created_at=previous, credits=0.2)
        self.add_run([
            ("企业软件哪家好？", "推荐云杉科技"),
            ("企业软件哪家好？", "云杉科技很好"),
            ("企业软件哪家好？", "推荐云杉科技"),
        ])
        current = self.report("week", 0)
        self.assertEqual(current["current"]["mention_rate"], 1.0)
        self.assertEqual(current["delta"]["mention_rate"], 1.0)
        previous_report = self.report("week", 1)
        self.assertEqual(previous_report["current"]["mention_rate"], 0)
        self.assertEqual(previous_report["gap_total"], 1)

    def test_report_validation(self):
        with self.assertRaises(commander.ChatError):
            self.report("diag", 0)
        report = self.report("month", "not-a-number")
        self.assertEqual(report["period"], "month")
        month = self.report("week", 99)
        self.assertEqual(len(month["trend"]), 7)

    def test_diagnosis_placeholders(self):
        estimate = geo.read(self.path("diagnoses/estimate"), {})["estimate"]
        self.assertTrue(estimate["blockers"])
        self.assertFalse(estimate["enough"])
        self.assertEqual(estimate["credits"], 0)
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("diagnoses"), {"run_id": 1})


class ReportShares(GeoReportBase):
    def test_share_lifecycle_and_views(self):
        self.add_run([("企业软件哪家好？", "推荐云杉科技")])
        share = self.share("week", 0, 7)
        self.assertEqual(share["period_name"], "周报")
        self.assertEqual(share["status"], "active")
        self.assertTrue(share["token"])
        listing = geo.read(self.path("report-shares"), {"page": ["1"], "page_size": ["8"]})
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["token"], share["token"])
        self.assertEqual(listing["items"][0]["status_name"], "有效")

        status, data = geo.public("/api/public/geo/report/" + share["token"])
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["report"]["runs"], 1)
        self.assertEqual(data["report"]["current"]["sample_n"], 1)
        self.assertTrue(data["expire_at"])
        listing = geo.read(self.path("report-shares"), {})
        self.assertEqual(listing["items"][0]["views"], 1)

        revoked = geo.write("DELETE", f"/api/geo/report-shares/{share['id']}", {})
        self.assertTrue(revoked["ok"])
        status, data = geo.public("/api/public/geo/report/" + share["token"])
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        listing = geo.read(self.path("report-shares"), {})
        self.assertEqual(listing["items"][0]["status"], "revoked")
        self.assertEqual(listing["items"][0]["status_name"], "已撤销")

    def test_expired_share(self):
        share = self.share("month", 0, 1)
        with commander.database() as db:
            db.execute(
                "UPDATE geo_report_shares SET expire_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", share["id"]),
            )
        status, data = geo.public("/api/public/geo/report/" + share["token"])
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        listing = geo.read(self.path("report-shares"), {})
        self.assertEqual(listing["items"][0]["status"], "expired")
        self.assertEqual(listing["items"][0]["status_name"], "已过期")

    def test_matrix_share_snapshot(self):
        geo.write("PUT", self.path("kw-matrix"), {
            "roots": {"industry": [{"text": "企业软件", "tier": "S"}]},
        })
        share = self.share("matrix", 0, 7)
        status, data = geo.public("/api/public/geo/report/" + share["token"])
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        report = data["report"]
        self.assertEqual(report["kind"], "kw_matrix")
        self.assertIn("词库矩阵", report["title"])
        self.assertEqual(report["total"], 1)
        self.assertEqual(len(report["layers"]), 5)
        self.assertEqual(share["period_name"], "词库矩阵")

    def test_share_validation(self):
        with self.assertRaises(commander.ChatError):
            self.share("week", 0, 3)
        with self.assertRaises(commander.ChatError):
            self.share("diag", 0, 7)
        with self.assertRaises(commander.ChatError):
            self.share("year", 0, 7)

    def test_public_unknown_token(self):
        status, data = geo.public("/api/public/geo/report/" + "x" * 24)
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("不存在", data["error"])

    def test_shares_are_tenant_scoped(self):
        share = self.share("week", 0, 7)
        other = tenancy.create_tenant("报告测试租户")
        with tenancy.scope(other):
            self.assertEqual(geo.read(geo.PROJECTS, {})["total"], 0)
            with self.assertRaises(commander.ChatError):
                geo.read(self.path("report-shares"), {})
            with self.assertRaises(commander.ChatError):
                geo.write("DELETE", f"/api/geo/report-shares/{share['id']}", {})
        # Public links are deliberately cross-tenant: whoever has the token can read.
        status, data = geo.public("/api/public/geo/report/" + share["token"])
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])


if __name__ == "__main__":
    unittest.main()
