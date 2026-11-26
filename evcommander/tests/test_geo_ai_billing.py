"""GEO AI generation calls (questions/facts/competitors) charge power.

Each call reserves power before the model call, settles from usage (or local
estimates) on success, and refunds on failure. Billing-disabled deployments
return zero credits and leave the balance untouched.

Run from evcommander/:
    python -m unittest discover -s tests -v
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import geo
import geo_brand
import payments
import tenancy


PROJECT = {
    "brand_name": "云杉科技", "aliases": ["云杉"], "website": "https://yunshan.example",
    "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
    "market": "cn", "locales": [],
}
ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN", "EV_POWER_CHARGE")


class AiBillingBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["EV_PAY_MOCK"] = "0"
        for key in ("EV_SEED_DEMO", "EV_TRIAL_ORIGIN", "EV_POWER_CHARGE"):
            os.environ.pop(key, None)
        tenancy.activate(tenancy.local_identity())
        self.project_id = geo.write("POST", geo.PROJECTS, PROJECT)["project"]["id"]
        self.path = lambda suffix="": (
            f"/api/geo/projects/{self.project_id}/{suffix}".rstrip("/")
            if suffix else f"/api/geo/projects/{self.project_id}")

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

    def set_power(self, value):
        with commander.database() as db:
            payments.initialize(db)
            db.execute("UPDATE pay_account SET power=? WHERE tenant_id=?",
                       (value, commander.tenant()))


class QuestionGenerationBillingTests(AiBillingBase):
    def test_generation_charges_power(self):
        payload = '["企业软件哪家好？","协作软件怎么选？","远程办公工具推荐"]'
        with mock.patch.object(commander, "generate", return_value=[payload]):
            result = geo.write("POST", self.path("questions/generate"), {"count": 3})
        self.assertEqual(len(result["items"]), 3)
        self.assertGreater(result["credits"], 0)
        self.assertAlmostEqual(payments.balance(), 100 - result["credits"], places=4)
        self.assertTrue(any(note.startswith("GEO 问题生成（")
                            for note in self.bills()))

    def test_generation_failure_refunds_and_falls_back(self):
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            result = geo.write("POST", self.path("questions/generate"), {"count": 2})
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["credits"], 0)
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(self.bills(), [])

    def test_insufficient_balance_blocks_generation(self):
        self.set_power(0.001)
        with mock.patch.object(commander, "generate") as generate:
            with self.assertRaises(commander.ChatError) as caught:
                geo.write("POST", self.path("questions/generate"), {"count": 2})
        self.assertIn("余额不足", str(caught.exception))
        generate.assert_not_called()
        self.assertEqual(payments.balance(), 0.001)

    def test_billing_disabled_returns_zero_credits(self):
        os.environ["EV_POWER_CHARGE"] = "0"
        payload = '["问题一","问题二"]'
        with mock.patch.object(commander, "generate", return_value=[payload]):
            result = geo.write("POST", self.path("questions/generate"), {"count": 2})
        self.assertEqual(result["credits"], 0)
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(self.bills(), [])


class FactsAndCompetitorsBillingTests(AiBillingBase):
    def facts_payload(self):
        return ('[{"fact_key":"founded","label":"成立时间","value":"2016 年成立于深圳"},'
                '{"fact_key":"main_products","label":"主营产品","value":"企业协作软件"}]')

    def test_fact_extraction_charges_power(self):
        with mock.patch.object(commander, "generate",
                               return_value=[self.facts_payload()]):
            result = geo_brand.write("POST", self.path("facts/extract"), {})
        self.assertEqual(result["added"], 2)
        self.assertGreater(result["credits"], 0)
        self.assertAlmostEqual(payments.balance(), 100 - result["credits"], places=4)
        self.assertTrue(any(note.startswith("GEO 事实整理（")
                            for note in self.bills()))

    def test_fact_extraction_failure_refunds(self):
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            with self.assertRaises(commander.ChatError):
                geo_brand.write("POST", self.path("facts/extract"), {})
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(self.bills(), [])

    def test_competitor_suggestion_charges_power(self):
        payload = '[{"name":"甲科技","reason":"同品类"},{"name":"乙科技","reason":"同品类"}]'
        with mock.patch.object(commander, "generate", return_value=[payload]):
            result = geo_brand.write("POST", self.path("competitors/suggest"), {})
        self.assertEqual(len(result["items"]), 2)
        self.assertGreater(result["credits"], 0)
        self.assertAlmostEqual(payments.balance(), 100 - result["credits"], places=4)
        self.assertTrue(any(note.startswith("GEO 竞品推荐（")
                            for note in self.bills()))

    def test_competitor_suggestion_failure_refunds(self):
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            with self.assertRaises(commander.ChatError):
                geo_brand.write("POST", self.path("competitors/suggest"), {})
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(self.bills(), [])

    def test_extraction_without_sources_is_free(self):
        with commander.database() as db:
            db.execute("UPDATE geo_projects SET description='' WHERE id=? AND tenant_id=?",
                       (self.project_id, commander.tenant()))
        with mock.patch.object(commander, "generate") as generate:
            result = geo_brand.write("POST", self.path("facts/extract"), {})
        self.assertEqual(result["added"], 0)
        generate.assert_not_called()
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(self.bills(), [])


if __name__ == "__main__":
    unittest.main()
