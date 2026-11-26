"""Power consumption charging: chat, image and GEO runs.

Every charge must be recorded as a consume bill, failed work must be refunded,
and an empty wallet must be rejected before the model is called.

Run from evcommander/:
    python -m unittest discover -s tests -v
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import geo
import geo_runs
import llm_provider
import payments


ENV_KEYS = (
    "EV_PAY_MOCK", "EV_SEED_DEMO", "EV_POWER_CHARGE", "EV_TRIAL_ORIGIN",
    "EV_PRICE_INPUT_PER_1K", "EV_PRICE_OUTPUT_PER_1K",
    "EV_PRICE_IMAGE", "EV_PRICE_GEO_ANSWER",
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
        self.assertEqual(payments.input_token_price(), 0.05)
        self.assertEqual(payments.output_token_price(), 0.2)
        self.assertEqual(payments.image_price(), 2.0)
        self.assertEqual(payments.geo_answer_price(), 0.2)
        os.environ["EV_PRICE_INPUT_PER_1K"] = "0.1"
        os.environ["EV_PRICE_OUTPUT_PER_1K"] = "0.5"
        os.environ["EV_PRICE_IMAGE"] = "3"
        os.environ["EV_PRICE_GEO_ANSWER"] = "0.1"
        self.assertEqual(payments.input_token_price(), 0.1)
        self.assertEqual(payments.output_token_price(), 0.5)
        self.assertEqual(payments.image_price(), 3.0)
        self.assertEqual(payments.geo_answer_price(), 0.1)
        os.environ["EV_PRICE_INPUT_PER_1K"] = "-1"
        os.environ["EV_PRICE_OUTPUT_PER_1K"] = "bad"
        self.assertEqual(payments.input_token_price(), 0.0)
        self.assertEqual(payments.output_token_price(), 0.2)

    def test_token_estimator_handles_cjk_and_ascii(self):
        self.assertEqual(payments.estimate_text_tokens("你好"), 2)
        self.assertEqual(payments.estimate_text_tokens("abcd"), 1)
        self.assertEqual(payments.estimate_text_tokens(""), 0)
        messages = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "abcd"}]
        self.assertEqual(payments.estimate_message_tokens(messages), 2 + 4 + 1 + 4)

    def test_token_charge_and_settlement(self):
        self.assertAlmostEqual(payments.token_charge(1000, 1000), 0.25)
        messages = [{"role": "user", "content": "你好"}]
        bill_id = payments.chat_reserve(messages, 1024)
        self.assertTrue(bill_id)
        self.assertLess(payments.balance(), 100.0)
        payments.settle_chat(
            bill_id, {"prompt_tokens": 1000, "completion_tokens": 100}, messages, "你好",
        )
        actual = payments.token_charge(1000, 100)
        self.assertAlmostEqual(payments.balance(), 100.0 - actual)
        entry = payments.read("/api/bill/list", {})["list"][0]
        self.assertAlmostEqual(entry["power"], actual)
        self.assertIn("1000 入", entry["title"])
        self.assertIn("100 出", entry["title"])

    def test_settlement_falls_back_to_estimates(self):
        messages = [{"role": "user", "content": "你好"}]
        bill_id = payments.chat_reserve(messages, 1024)
        payments.settle_chat(bill_id, {}, messages, "你好")
        expected = payments.token_charge(
            payments.estimate_message_tokens(messages),
            payments.estimate_text_tokens("你好"),
        )
        self.assertAlmostEqual(payments.balance(), 100.0 - expected)


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


class _RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bodies = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _RelayHandler.bodies.append(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        events = (
            'data: {"choices":[{"delta":{"content":"你好"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":1000,"completion_tokens":100,"total_tokens":1100}}',
            "data: [DONE]",
        )
        for event in events:
            self.wfile.write((event + "\n\n").encode("utf-8"))
            self.wfile.flush()
        self.close_connection = True


class ProviderUsageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _RelayHandler.bodies = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _RelayHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.old_env = {
            key: os.environ.get(key) for key in (
                "EV_LLM_BASE_URL", "EV_LLM_MODEL", "EV_LLM_PROTOCOL",
                "EV_LLM_API_KEY", "EV_LLM_STREAM_USAGE",
            )
        }
        os.environ.update({
            "EV_LLM_BASE_URL": f"http://127.0.0.1:{cls.server.server_address[1]}/v1",
            "EV_LLM_MODEL": "stub-chat",
            "EV_LLM_PROTOCOL": "chat_completions",
            "EV_LLM_API_KEY": "test-key",
            "EV_LLM_STREAM_USAGE": "1",
        })

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_usage_after_finish_is_captured(self):
        usage = {}
        text = "".join(llm_provider.stream(
            [{"role": "user", "content": "hi"}], usage=usage,
        ))
        self.assertEqual(text, "你好")
        self.assertEqual(usage.get("prompt_tokens"), 1000)
        self.assertEqual(usage.get("completion_tokens"), 100)
        payload = json.loads(_RelayHandler.bodies[-1])
        self.assertTrue(payload["stream_options"]["include_usage"])

    def test_stream_usage_can_be_disabled(self):
        os.environ["EV_LLM_STREAM_USAGE"] = "0"
        try:
            "".join(llm_provider.stream([{"role": "user", "content": "hi"}]))
        finally:
            os.environ["EV_LLM_STREAM_USAGE"] = "1"
        payload = json.loads(_RelayHandler.bodies[-1])
        self.assertNotIn("stream_options", payload)


if __name__ == "__main__":
    unittest.main()


class ReconcileTests(BillingBase):
    def setUp(self):
        super().setUp()
        self.set_power(100)

    def test_running_run_is_refunded(self):
        bill = payments.charge(1.5, "GEO 体检 #99")
        with commander.database() as db:
            geo_runs.initialize(db)
            run_id = db.execute(
                "INSERT INTO geo_runs (tenant_id,project_id,status,total,charge_id,created_at) "
                "VALUES (?,1,'running',1,?,'2026-11-26T00:00:00+00:00')",
                (commander.tenant(), bill)).lastrowid
        self.assertEqual(payments.balance(), 98.5)
        self.assertEqual(geo_runs.reconcile_charges(), 1)
        with commander.database() as db:
            row = db.execute("SELECT status,error FROM geo_runs WHERE id=?",
                             (run_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("退回", row["error"])
        self.assertEqual(payments.balance(), 100.0)

    def test_done_run_is_untouched(self):
        bill = payments.charge(1.0, "GEO 体检 #1")
        with commander.database() as db:
            geo_runs.initialize(db)
            db.execute(
                "INSERT INTO geo_runs (tenant_id,project_id,status,total,charge_id,created_at) "
                "VALUES (?,1,'done',1,?,'x')", (commander.tenant(), bill))
        self.assertEqual(geo_runs.reconcile_charges(), 0)
        self.assertEqual(payments.balance(), 99.0)

    def test_running_diagnosis_is_refunded(self):
        import geo_diagnosis
        bill = payments.charge(0.8, "GEO 诊断 #7")
        with commander.database() as db:
            geo_diagnosis.initialize(db)
            diagnosis_id = db.execute(
                "INSERT INTO geo_diagnoses (tenant_id,project_id,run_id,status,charge_id,"
                "created_at) VALUES (?,1,1,'running',?,'x')",
                (commander.tenant(), bill)).lastrowid
        self.assertEqual(geo_diagnosis.reconcile_charges(), 1)
        with commander.database() as db:
            row = db.execute("SELECT status,error FROM geo_diagnoses WHERE id=?",
                             (diagnosis_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("退回", row["error"])
        self.assertEqual(payments.balance(), 100.0)

    def test_orphan_chat_reservation_is_refunded(self):
        bill = payments.charge(0.4, "对话消费")
        self.assertEqual(payments.balance(), 99.6)
        self.assertEqual(payments.reconcile_reservations(), 0)
        with commander.database() as db:
            db.execute("UPDATE pay_bills SET created_at='2026-01-01T00:00:00+00:00' "
                       "WHERE id=?", (bill,))
        self.assertEqual(payments.reconcile_reservations(), 1)
        self.assertEqual(payments.balance(), 100.0)
        settled = payments.charge(0.4, "对话消费")
        with commander.database() as db:
            db.execute("UPDATE pay_bills SET note='对话消费（10 入 / 20 出）', "
                       "created_at='2026-01-01T00:00:00+00:00' WHERE id=?", (settled,))
        self.assertEqual(payments.reconcile_reservations(), 0)
        self.assertEqual(payments.balance(), 99.6)

    def test_image_reservations_are_marked_or_refunded(self):
        done = payments.charge(2.0, "生图消费：蓝底商品图")
        payments.mark_image_done(done)
        with commander.database() as db:
            note = db.execute("SELECT note FROM pay_bills WHERE id=?", (done,)).fetchone()[0]
        self.assertEqual(note, "生图消费（已完成）：蓝底商品图")
        with commander.database() as db:
            db.execute("UPDATE pay_bills SET created_at='2026-01-01T00:00:00+00:00' "
                       "WHERE id=?", (done,))
        self.assertEqual(payments.reconcile_reservations(), 0)
        self.assertEqual(payments.balance(), 98.0)
        orphan = payments.charge(2.0, "生图消费：中断的图")
        with commander.database() as db:
            db.execute("UPDATE pay_bills SET created_at='2026-01-01T00:00:00+00:00' "
                       "WHERE id=?", (orphan,))
        self.assertEqual(payments.reconcile_reservations(), 1)
        self.assertEqual(payments.balance(), 98.0)
