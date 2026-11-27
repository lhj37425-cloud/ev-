"""Failure-path regression tests for the local modules.

Each writer must reject invalid input with ChatError (which the HTTP layer
turns into 400) instead of silently succeeding or raising an uncaught error.
No network, payment provider, or text model is called.

Run from evcommander/:
    python -m unittest discover -s tests -v
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import customer_import
import customers
import digital_human
import geo
import knowledge
import materials
import memory
import payments


class IsolatedData(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        os.environ["EV_PAY_MOCK"] = "0"
        os.environ.pop("EV_SEED_DEMO", None)

    def tearDown(self):
        commander.DATA_DIR = self.old_dir
        self.temp.cleanup()


GEO_PROJECT = {
    "brand_name": "失败路径品牌", "aliases": [], "website": "", "industry": "",
    "region": "", "description": "", "market": "cn", "locales": [],
}


class GeoFailureTests(IsolatedData):
    def test_project_limit_and_missing_project(self):
        for index in range(geo.LIMIT):
            result = geo.write("POST", geo.PROJECTS, {**GEO_PROJECT, "brand_name": f"品牌{index}"})
            self.assertTrue(result["ok"])
        with self.assertRaises(commander.ChatError):
            geo.write("POST", geo.PROJECTS, {**GEO_PROJECT, "brand_name": "超限"})
        with self.assertRaises(commander.ChatError):
            geo.read(geo.PROJECTS + "/999", {})
        with self.assertRaises(commander.ChatError):
            geo.write("PUT", geo.PROJECTS + "/999", GEO_PROJECT)
        with self.assertRaises(commander.ChatError):
            geo.write("DELETE", geo.PROJECTS + "/999", {})

    def test_project_payload_validation(self):
        with self.assertRaises(commander.ChatError):
            geo.write("POST", geo.PROJECTS, {**GEO_PROJECT, "brand_name": "   "})
        with self.assertRaises(commander.ChatError):
            geo.write("POST", geo.PROJECTS, {**GEO_PROJECT, "market": "mars"})


class PaymentFailureTests(IsolatedData):
    def _initialize(self):
        with commander.database() as db:
            db.execute("BEGIN IMMEDIATE")
            payments.initialize(db)

    def test_cancel_missing_and_paid_orders(self):
        self._initialize()
        with self.assertRaises(commander.ChatError):
            payments.write(payments.ORDER_CANCEL, {"order_no": "MISSING"})
        with commander.database() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO pay_orders (order_no,tenant_id,product_id,product_name,order_type,amount,power_amount,"
                "channel,channel_name,pay_status,created_at,target) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
                ("PAID-1", commander.tenant(), None, "test", 1, 1.0, 10, "alipay", "支付宝",
                 payments.now(), ""),
            )
        with self.assertRaises(commander.ChatError):
            payments.write(payments.ORDER_CANCEL, {"order_no": "PAID-1"})

    def test_card_exchange_errors(self):
        self._initialize()
        with self.assertRaises(commander.ChatError):
            payments.write(payments.CARD_EXCHANGE, {})
        with commander.database() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO pay_cards (code,power_amount,used) VALUES ('USED-1',10,1)")
        with self.assertRaises(commander.ChatError):
            payments.write(payments.CARD_EXCHANGE, {"code": "USED-1"})
        with self.assertRaises(commander.ChatError):
            payments.write(payments.CARD_EXCHANGE, {"code": "MISSING"})

    def test_recharge_errors(self):
        self._initialize()
        with self.assertRaises(commander.ChatError):
            payments.write(payments.RECHARGE, {})
        with self.assertRaises(commander.ChatError):
            payments.write(payments.RECHARGE, {"channel": "nope", "amount": 10})
        with self.assertRaises(commander.ChatError):
            payments.write(payments.RECHARGE, {"channel": "alipay", "product_id": 999})
        with self.assertRaises(commander.ChatError):
            payments.write(payments.RECHARGE, {"channel": "alipay", "amount": 0})
        with self.assertRaisesRegex(commander.ChatError, "充值金额无效"):
            payments.write(payments.RECHARGE, {"channel": "alipay", "amount": "nan"})


class KnowledgeMemoryFailureTests(IsolatedData):
    def test_knowledge_missing_and_empty(self):
        with self.assertRaises(commander.ChatError):
            knowledge.read("/api/kb/detail", {"id": ["999"]})
        with self.assertRaises(commander.ChatError):
            knowledge.write("/api/kb/create", {})
        with self.assertRaises(commander.ChatError):
            knowledge.write("/api/kb/resource/create-text",
                            {"kb_id": 999, "name": "资料", "content": "内容"})

    def test_memory_invalid_ids(self):
        with self.assertRaises(commander.ChatError):
            memory.write(memory.PREFIX + "delete", {})
        with self.assertRaises(commander.ChatError):
            memory.write(memory.PREFIX + "update", {"content": "x"})


class DigitalHumanFailureTests(IsolatedData):
    def test_missing_required_text(self):
        with self.assertRaises(commander.ChatError):
            digital_human.write(digital_human.PROPOSE, {})
        with self.assertRaises(commander.ChatError):
            digital_human.write(digital_human.TRANSLATE, {})
        with self.assertRaises(commander.ChatError):
            digital_human.write(digital_human.TRANSLATE, {"text": "   "})


class WorkspaceFailureTests(IsolatedData):
    def test_customer_and_lead_validation(self):
        with self.assertRaises(commander.ChatError):
            customers.write("POST", customers.CRM, {})
        with self.assertRaises(commander.ChatError):
            customers.write("POST", customers.LEADS + "/save", {"items": []})
        with self.assertRaises(commander.ChatError):
            customers.write("POST", customers.CRM, {"name": "客户", "tags": ["标签"] * 31})

    def test_import_rejects_bad_spreadsheets(self):
        with self.assertRaises(commander.ChatError):
            customer_import.preview(b"not a zip")
        with self.assertRaises(commander.ChatError):
            customer_import.preview(customer_import.template())

    def test_upload_rejects_bad_multipart(self):
        with self.assertRaises(commander.ChatError):
            materials.upload("multipart/form-data", b"garbage")
        with self.assertRaises(commander.ChatError):
            materials.upload(
                "multipart/form-data; boundary=x",
                b"--x\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.png\"\r\n\r\n\r\n--x--\r\n",
            )


if __name__ == "__main__":
    unittest.main()
