"""Power consumption charging: chat, image and GEO runs.

Every charge must be recorded as a consume bill, failed work must be refunded,
and an empty wallet must be rejected before the model is called.

Run from evcommander/:
    python -m unittest discover -s tests -v
"""

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import geo
import geo_runs
import payments


ENV_KEYS = (
    "EV_PAY_MOCK", "EV_SEED_DEMO", "EV_POWER_CHARGE", "EV_TRIAL_ORIGIN",
    "EV_PRICE_CHAT", "EV_PRICE_IMAGE", "EV_PRICE_GEO_ANSWER",
)


class BillingBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["EV_PAY_MOCK"] = "0"
        for key in ENV_KEYS[1:]:
            os.environ.pop(key, None)

    def tearDown(self):
        commander.DATA_DIR = self.old_dir
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def set_power(self, value):
        with commander.database() as db:
            payments.initialize(db)
            db.execute("UPDATE pay_account SET power=? WHERE tenant_id=?",
                       (value, commander.tenant()))

    def project(self):
        result = geo.write("POST", geo.PROJECTS, {
            "brand_name": "TestBrand", "aliases": [], "website": "https://example.com",
            "industry": "露营", "region": "", "description": "", "market": "cn", "locales": [],
        })
        return result["project"]["id"]

    def wait_run(self, run_id, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = geo_runs.read(f"/api/geo/runs/{run_id}", {})["run"]
            if run["status"] in {"done", "failed", "cancelled"}:
                return run
            time.sleep(0.02)
        self.fail("体检任务没有在限定时间内结束。")


class LedgerTests(BillingBase):
    def test_charge_and_refund_are_recorded(self):
        self.assertEqual(payments.balance(), 100.0)
        bill_id = payments.charge(0.5, "测试消费")
        self.assertTrue(bill_id)
        self.assertEqual(payments.balance(), 99.5)
        bills = payments.read("/api/bill/list", {})
        self.assertEqual(bills["total_consume"], 0.05)
        self.assertEqual(bills["today_used"], 0.5)
        entry = bills["list"][0]
        self.assertEqual(entry["type"], "consume")
        self.assertEqual(entry["direction"], "expense")
        self.assertEqual(entry["power"], 0.5)
        self.assertEqual(entry["amount"], 0.05)
        payments.refund(bill_id)
        self.assertEqual(payments.balance(), 100.0)
        bills = payments.read("/api/bill/list", {})
        self.assertEqual(bills["total"], 0)
        self.assertEqual(bills["total_consume"], 0)

    def test_insufficient_balance_is_rejected(self):
        self.set_power(0.1)
        with self.assertRaises(commander.ChatError) as caught:
            payments.charge(0.2, "测试消费")
        self.assertIn("余额不足", str(caught.exception))
        self.assertEqual(payments.balance(), 0.1)
        self.assertEqual(payments.read("/api/bill/list", {})["total"], 0)

    def test_switch_and_trial_mode_disable_charging(self):
        os.environ["EV_POWER_CHARGE"] = "0"
        self.assertEqual(payments.charge(0.2, "测试消费"), 0)
        self.assertEqual(payments.balance(), 100.0)
        os.environ.pop("EV_POWER_CHARGE")
        os.environ["EV_TRIAL_ORIGIN"] = "https://trial.example.com"
        self.assertEqual(payments.charge(0.2, "测试消费"), 0)
        self.assertEqual(payments.balance(), 100.0)

    def test_prices_come_from_environment(self):
        self.assertEqual(payments.chat_price(), 0.2)
        self.assertEqual(payments.image_price(), 2.0)
        self.assertEqual(payments.geo_answer_price(), 0.2)
        os.environ["EV_PRICE_CHAT"] = "0.5"
        os.environ["EV_PRICE_IMAGE"] = "3"
        os.environ["EV_PRICE_GEO_ANSWER"] = "0.1"
        self.assertEqual(payments.chat_price(), 0.5)
        self.assertEqual(payments.image_price(), 3.0)
        self.assertEqual(payments.geo_answer_price(), 0.1)
        os.environ["EV_PRICE_CHAT"] = "-1"
        os.environ["EV_PRICE_IMAGE"] = "bad"
        self.assertEqual(payments.chat_price(), 0.0)
        self.assertEqual(payments.image_price(), 2.0)


class GeoBillingTests(BillingBase):
    def add_questions(self, project_id, count=2):
        geo_runs.write("POST", f"/api/geo/projects/{project_id}/questions", {
            "items": [{"text": f"问题 {index}"} for index in range(count)],
        })

    def test_estimate_counts_all_engines_and_balance(self):
        project_id = self.project()
        self.add_questions(project_id)
        estimate = geo_runs.write(
            "POST", f"/api/geo/projects/{project_id}/runs/estimate",
            {"engines": ["doubao", "deepseek"], "samples": 1},
        )["estimate"]
        self.assertAlmostEqual(estimate["credits"], 0.8)
        self.assertEqual(estimate["balance"], 100.0)
        self.assertTrue(estimate["enough"])
        self.set_power(0.1)
        estimate = geo_runs.write(
            "POST", f"/api/geo/projects/{project_id}/runs/estimate",
            {"engines": ["doubao", "deepseek"], "samples": 1},
        )["estimate"]
        self.assertAlmostEqual(estimate["credits"], 0.8)
        self.assertEqual(estimate["balance"], 0.1)
        self.assertFalse(estimate["enough"])
        with self.assertRaises(commander.ChatError):
            geo_runs.write("POST", f"/api/geo/projects/{project_id}/runs",
                           {"engines": ["doubao"], "samples": 1})
        runs = geo_runs.read(f"/api/geo/projects/{project_id}/runs", {})
        self.assertEqual(runs["total"], 0)
        self.assertEqual(payments.balance(), 0.1)

    def test_successful_answers_are_charged(self):
        project_id = self.project()
        self.add_questions(project_id)

        def fake(messages, **kwargs):
            time.sleep(0.05)
            yield "回答内容"

        with mock.patch.object(commander, "generate", fake):
            started = geo_runs.write("POST", f"/api/geo/projects/{project_id}/runs",
                                     {"engines": ["doubao"], "samples": 1})["run"]
            run = self.wait_run(started["id"])
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["done"], 2)
        self.assertEqual(run["failed"], 0)
        self.assertAlmostEqual(run["credits"], 0.4)
        self.assertAlmostEqual(payments.balance(), 99.6)
        bills = payments.read("/api/bill/list", {})
        self.assertEqual(bills["total"], 1)
        self.assertEqual(bills["list"][0]["type"], "consume")
        self.assertAlmostEqual(bills["list"][0]["power"], 0.4)
        self.assertIn("GEO 体检", bills["list"][0]["title"])

    def test_failed_answers_are_refunded(self):
        project_id = self.project()
        self.add_questions(project_id)

        def boom(messages, **kwargs):
            raise commander.ChatError("上游模型失败")
            yield  # pragma: no cover

        with mock.patch.object(commander, "generate", boom):
            started = geo_runs.write("POST", f"/api/geo/projects/{project_id}/runs",
                                     {"engines": ["doubao"], "samples": 1})["run"]
            run = self.wait_run(started["id"])
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["done"], 0)
        self.assertEqual(run["failed"], 2)
        self.assertEqual(run["credits"], 0)
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(payments.read("/api/bill/list", {})["total"], 0)

    def test_only_successful_answers_remain_charged(self):
        project_id = self.project()
        self.add_questions(project_id)
        calls = {"count": 0}

        def flaky(messages, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise commander.ChatError("上游模型失败")
            time.sleep(0.02)
            yield "回答内容"

        with mock.patch.object(commander, "generate", flaky):
            started = geo_runs.write("POST", f"/api/geo/projects/{project_id}/runs",
                                     {"engines": ["doubao"], "samples": 1})["run"]
            run = self.wait_run(started["id"])
        self.assertEqual(run["done"], 1)
        self.assertEqual(run["failed"], 1)
        self.assertAlmostEqual(run["credits"], 0.2)
        self.assertAlmostEqual(payments.balance(), 99.8)
        bills = payments.read("/api/bill/list", {})
        self.assertEqual(bills["total"], 1)
        self.assertAlmostEqual(bills["list"][0]["power"], 0.2)


if __name__ == "__main__":
    unittest.main()
