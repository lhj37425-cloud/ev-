"""Digital-human and WhatsApp AI calls charge power and refund failures."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import digital_human
import operations
import payments
import tenancy
import whatsapp


ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN", "EV_POWER_CHARGE")


def gen(payload):
    def fake(*args, **kwargs):
        yield payload
    return fake


class MiscBillingBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["EV_PAY_MOCK"] = "0"
        for key in ("EV_SEED_DEMO", "EV_TRIAL_ORIGIN", "EV_POWER_CHARGE"):
            os.environ.pop(key, None)
        tenancy.activate(tenancy.local_identity())

    def tearDown(self):
        tenancy.activate(None)
        commander.DATA_DIR = self.old_dir
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def bills(self):
        with commander.database() as db:
            payments.initialize(db)
            return [row[0] for row in db.execute(
                "SELECT note FROM pay_bills WHERE kind='consume'").fetchall()]


class DigitalHumanBillingTests(MiscBillingBase):
    def test_propose_charges_power(self):
        payload = json.dumps(["这是第一条口播稿。", "这是第二条口播稿。"], ensure_ascii=False)
        with mock.patch.object(commander, "generate", return_value=[payload]):
            result = digital_human.propose({"topic": "周末露营"})
        self.assertEqual(len(result["proposals"]), 2)
        self.assertLess(payments.balance(), 100.0)
        self.assertTrue(any(note.startswith("数字人口播稿（") for note in self.bills()))

    def test_propose_failure_refunds(self):
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            with self.assertRaises(commander.ChatError):
                digital_human.propose({"topic": "周末露营"})
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(self.bills(), [])

    def test_translate_charges_and_failure_refunds(self):
        with mock.patch.object(commander, "generate", return_value=["Hello there."]):
            result = digital_human.translate({"text": "你好。", "target_lang": "英语"})
        self.assertEqual(result["text"], "Hello there.")
        self.assertLess(payments.balance(), 100.0)
        self.assertTrue(any(note.startswith("数字人翻译（") for note in self.bills()))
        before = payments.balance()
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            with self.assertRaises(commander.ChatError):
                digital_human.translate({"text": "你好。", "target_lang": "英语"})
        self.assertEqual(payments.balance(), before)


class WhatsAppBillingTests(MiscBillingBase):
    def test_ask_json_charges_power(self):
        with mock.patch.object(commander, "generate",
                               new=gen('{"text": "译文", "lang": "en"}')):
            parsed = whatsapp._ask_json("翻译", {"text": "你好"}, 800,
                                        label="WhatsApp 草稿翻译")
        self.assertEqual(parsed["text"], "译文")
        self.assertLess(payments.balance(), 100.0)
        self.assertTrue(any(note.startswith("WhatsApp 草稿翻译（")
                            for note in self.bills()))

    def test_ask_json_invalid_json_refunds(self):
        with mock.patch.object(commander, "generate", new=gen("not-json")):
            with self.assertRaises(commander.ChatError):
                whatsapp._ask_json("翻译", {"text": "你好"}, 800,
                                   label="WhatsApp 翻译")
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(self.bills(), [])

    def test_ask_json_failure_refunds(self):
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            with self.assertRaises(commander.ChatError):
                whatsapp._ask_json("翻译", {"text": "你好"}, 800,
                                   label="WhatsApp 翻译")
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(self.bills(), [])

    def test_draft_reply_charges_and_failure_refunds(self):
        role = {"id": 5, "persona_prompt": "", "persona_name": "小云",
                "kb_ids": "", "reply_max_len": 200}
        with mock.patch.object(commander, "generate",
                               new=gen("您好，请问有什么可以帮您？")):
            reply = whatsapp._draft_reply(role, "客户", "你好")
        self.assertIn("您好", reply)
        self.assertLess(payments.balance(), 100.0)
        self.assertTrue(any(note.startswith("WhatsApp 自动回复（")
                            for note in self.bills()))
        before = payments.balance()
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            with self.assertRaises(commander.ChatError):
                whatsapp._draft_reply(role, "客户", "你好")
        self.assertEqual(payments.balance(), before)


if __name__ == "__main__":
    unittest.main()


class OperationsBillingTests(MiscBillingBase):
    def test_ask_json_charges_power(self):
        with mock.patch.object(commander, "generate",
                               new=gen('{"title": "规划"}')):
            result = operations.ask_json("系统", {"a": 1}, 500,
                                         label="运营 AI：测试")
        self.assertEqual(result["title"], "规划")
        self.assertLess(payments.balance(), 100.0)
        self.assertTrue(any(note.startswith("运营 AI：测试（")
                            for note in self.bills()))

    def test_ask_json_failure_refunds(self):
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            with self.assertRaises(commander.ChatError):
                operations.ask_json("系统", {"a": 1}, 500, label="运营 AI：测试")
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(self.bills(), [])

    def test_ask_json_invalid_json_refunds(self):
        with mock.patch.object(commander, "generate", new=gen("not-json")):
            with self.assertRaises(commander.ChatError):
                operations.ask_json("系统", {"a": 1}, 500, label="运营 AI：测试")
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(self.bills(), [])
