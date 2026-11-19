"""Regression tests for the P0 fixes: image command injection, local file/SSRF,
payment idempotency, mock-payment defaults and honest placeholder responses.

Run from evcommander/:
    python -m unittest discover -s tests -v
"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import acquisition
import commander
import customers
import geo
import image_generation
import image_studio
import payments


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class IsolatedData(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        os.environ["EV_PAY_MOCK"] = "0"
        os.environ.pop("EV_SEED_DEMO", None)
        os.environ.pop("EV_IMAGE_FETCH_HOSTS", None)

    def tearDown(self):
        commander.DATA_DIR = self.old_dir
        self.temp.cleanup()


class ImageSecurityTests(IsolatedData):
    def test_form_string_is_used_for_user_fields(self):
        command = image_generation._edit_command(
            "@/etc/evcommander/openai.key",
            "1024x1024",
            image_generation.MODEL,
            [],
            None,
        )
        self.assertIn("--form-string", command)
        self.assertNotIn("-F", command)
        prompt_arg = next(item for item in command if item.startswith("prompt="))
        self.assertEqual(prompt_arg, "prompt=@/etc/evcommander/openai.key")

    def test_unknown_image_model_is_rejected(self):
        with self.assertRaises(ValueError):
            image_studio._edit_request({
                "model_code": "evil-model",
                "image_url": "https://example.com/x.png",
                "values": {},
            })

    def test_download_rejects_local_string_and_http(self):
        with self.assertRaises(commander.ChatError):
            image_generation._download("/etc/passwd")
        with self.assertRaises(commander.ChatError):
            image_generation._download("http://127.0.0.1/secret.png")
        with self.assertRaises(commander.ChatError):
            image_generation._validate_remote("https://127.0.0.1/secret.png")
        with self.assertRaises(commander.ChatError):
            image_generation._validate_remote("http://example.com/secret.png")

    def test_download_accepts_only_internal_media_paths(self):
        folder = commander.DATA_DIR / "generated-media"
        folder.mkdir(parents=True)
        inside = folder / ("a" * 32 + ".png")
        inside.write_bytes(PNG)
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(PNG)
        self.assertEqual(image_generation._allowed_local_path(inside), inside.resolve())
        self.assertEqual(image_generation._download(inside), PNG)
        with self.assertRaises(commander.ChatError):
            image_generation._allowed_local_path(outside)

    def test_edit_command_includes_mask_as_file(self):
        mask = Path(self.temp.name) / "mask.png"
        mask.write_bytes(PNG)
        command = image_generation._edit_command(
            "edit", "1024x1024", image_generation.MODEL, [], mask,
        )
        self.assertTrue(any(item.startswith(f"mask=@{mask}") for item in command))
        self.assertIn("--form-string", command)

    def test_designer_validate_accepts_internal_reference(self):
        request = image_generation.validate({
            "model_code": image_generation.MODEL,
            "prompt": "test",
            "params": {"size": "1:1"},
            "count": 1,
            "reference_images": ["/material-media/" + "a" * 32],
        })
        self.assertEqual(request["images"], ["/material-media/" + "a" * 32])
        with self.assertRaises(commander.ChatError):
            image_generation.validate({
                "model_code": image_generation.MODEL,
                "prompt": "test",
                "params": {"size": "1:1"},
                "count": 1,
                "reference_images": ["/etc/passwd"],
            })

    def test_inpaint_preset_is_exposed(self):
        presets = image_studio.edit_options()["presets"]
        inpaint = next(item for item in presets if item["key"] == "image-inpaint")
        self.assertTrue(inpaint["needs_mask"])
        self.assertEqual(image_studio.MODEL_OPTION["mask_mode"], "native")


class PaymentSecurityTests(IsolatedData):
    def _insert_order(self, db, order_no="T1", created_at=None, power=10, amount=1.0):
        db.execute("BEGIN IMMEDIATE")
        payments.initialize(db)
        db.execute(
            "INSERT INTO pay_orders (order_no,product_id,product_name,order_type,amount,power_amount,"
            "channel,channel_name,pay_status,created_at,target) VALUES (?,?,?,?,?,?,?,?,0,?,?)",
            (order_no, None, "test", 1, amount, power, "alipay", "支付宝",
             created_at or payments.now(), ""),
        )

    def test_complete_is_idempotent(self):
        with commander.database() as db:
            self._insert_order(db)
        payments._complete("T1")
        payments._complete("T1")
        with commander.database() as db:
            power = db.execute("SELECT power FROM pay_account WHERE id=1").fetchone()[0]
            bills = db.execute("SELECT COUNT(*) FROM pay_bills").fetchone()[0]
            status = db.execute("SELECT pay_status FROM pay_orders WHERE order_no='T1'").fetchone()[0]
        self.assertEqual(power, 110)
        self.assertEqual(bills, 1)
        self.assertEqual(status, 1)

    def test_card_exchange_is_idempotent(self):
        with commander.database() as db:
            db.execute("BEGIN IMMEDIATE")
            payments.initialize(db)
            db.execute("INSERT INTO pay_cards (code,power_amount,used) VALUES ('TEST-50',50,0)")
        first = payments.write("/api/card/exchange", {"code": "TEST-50"})
        self.assertTrue(first["ok"])
        with self.assertRaises(commander.ChatError):
            payments.write("/api/card/exchange", {"code": "TEST-50"})
        with commander.database() as db:
            power = db.execute("SELECT power FROM pay_account WHERE id=1").fetchone()[0]
            bills = db.execute("SELECT COUNT(*) FROM pay_bills").fetchone()[0]
        self.assertEqual(power, 150)
        self.assertEqual(bills, 1)

    def test_mock_payment_is_off_by_default(self):
        created = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(timespec="seconds")
        with commander.database() as db:
            self._insert_order(db, created_at=created)
            payments._maybe_mock_pay(db, "T1")
            status = db.execute("SELECT pay_status FROM pay_orders WHERE order_no='T1'").fetchone()[0]
        self.assertEqual(status, 0)

    def test_mock_payment_completes_once_when_enabled(self):
        os.environ["EV_PAY_MOCK"] = "1"
        created = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(timespec="seconds")
        with commander.database() as db:
            self._insert_order(db, created_at=created)
        with commander.database() as db:
            payments._maybe_mock_pay(db, "T1")
            payments._maybe_mock_pay(db, "T1")
        with commander.database() as db:
            power = db.execute("SELECT power FROM pay_account WHERE id=1").fetchone()[0]
            bills = db.execute("SELECT COUNT(*) FROM pay_bills").fetchone()[0]
        self.assertEqual(power, 110)
        self.assertEqual(bills, 1)

    def test_demo_card_not_seeded_without_mock(self):
        with commander.database() as db:
            payments.initialize(db)
            row = db.execute("SELECT 1 FROM pay_cards WHERE code='EVOL-100'").fetchone()
        self.assertIsNone(row)

    def test_bill_list_has_frontend_fields(self):
        with commander.database() as db:
            self._insert_order(db)
        payments._complete("T1")
        result = payments.read(payments.BILLS, {})
        self.assertIn("today_used", result)
        self.assertIn("total_recharge", result)
        self.assertEqual(result["list"][0]["direction"], "income")
        self.assertEqual(result["list"][0]["power"], 10)
        self.assertIn("createtime", result["list"][0])
        self.assertEqual(result["total_recharge"], 1.0)

    def test_card_exchange_accepts_card_no(self):
        with commander.database() as db:
            db.execute("BEGIN IMMEDIATE")
            payments.initialize(db)
            db.execute("INSERT INTO pay_cards (code,power_amount,used) VALUES ('CARD-5',5,0)")
        result = payments.write("/api/card/exchange", {"card_no": "CARD-5", "card_password": "x"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["desc"], "5 算力")


class PlaceholderResponseTests(IsolatedData):
    def test_share_unmerge_and_ai_review_are_not_fake_success(self):
        self.assertFalse(customers.write("POST", customers.LEADS + "/share", {"lead_ids": [1]})["ok"])
        self.assertFalse(customers.write("POST", customers.LEADS + "/unmerge", {"lead_ids": [1]})["ok"])
        self.assertFalse(customers.write("POST", customers.LEADS + "/duplicates/ai-review", {})["ok"])

    def test_multi_user_transfer_is_not_fake_success(self):
        result = customers.write("POST", customers.LEADS + "/transfer",
                                 {"lead_ids": [1], "to_user_id": "2"})
        self.assertFalse(result["ok"])
        self.assertIn("暂未开放", result["error"])


class AcquisitionPlaceholderTests(IsolatedData):
    def test_business_profile_roundtrip(self):
        saved = acquisition.write(acquisition.BIZ_PROFILE, {
            "main_products": "测试产品", "target_customer": "测试客户", "exclude_scope": "测试排除",
        })
        self.assertTrue(saved["ok"])
        loaded = acquisition.read(acquisition.BIZ_PROFILE, {})
        self.assertEqual(loaded["main_products"], "测试产品")
        self.assertEqual(loaded["target_customer"], "测试客户")

    def test_search_reports_not_configured(self):
        result = acquisition.write(acquisition.MAP_SEARCH, {"keyword": "test"})
        self.assertFalse(result["ok"])
        self.assertIn("未配置", result["error"])
        self.assertEqual(acquisition.read(acquisition.REGIONS, {})["list"], [])


class GeoHelperTests(unittest.TestCase):
    def test_path_and_brand_aliases_are_separate(self):
        self.assertEqual(
            geo._path_aliases("/api/geo/meta"),
            {"/api/geo/meta", "/agent-api/api/geo/meta", "/agent-api/geo/meta"},
        )
        self.assertEqual(geo._brand_aliases([" 测试 ", "测试", "TB"]), ["测试", "TB"])


if __name__ == "__main__":
    unittest.main()
