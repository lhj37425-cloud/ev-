"""GEO brand facts and competitors.

Covers the facts/competitors contract used by the archived brand page:
manual entry, bulk review, AI extraction, competitor CRUD, AI suggestions,
share of voice from stored answers, limits and tenant isolation.

Run from evcommander/:
    python -m unittest discover -s tests -v
"""

from datetime import datetime, timedelta, timezone
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
import geo_runs
import tenancy


PROJECT = {
    "brand_name": "云杉科技", "aliases": ["云杉"], "website": "https://example.com",
    "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
    "market": "cn", "locales": [],
}
ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN")


class GeoBrandBase(unittest.TestCase):
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

    def add_answer(self, text, created_at=None):
        with commander.database() as db:
            geo_runs.initialize(db)
            db.execute(
                "INSERT INTO geo_answers (tenant_id,project_id,run_id,question_id,engine_key,"
                "engine_name,attempt,status,text,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (commander.tenant(), self.project_id, 0, 0, "doubao", "豆包（模拟）", 0,
                 "ok", text, "", created_at or geo_brand.now()),
            )


class FactTests(GeoBrandBase):
    def test_fact_add_update_list_and_counts(self):
        added = geo_brand.write("POST", self.path("facts"), {
            "fact_key": "founded", "label": "成立时间", "value": "2015 年成立于深圳",
        })
        self.assertTrue(added["ok"])
        fact = added["item"]
        self.assertEqual(fact["status"], "confirmed")
        self.assertEqual(fact["source_name"], "手动录入")

        listing = geo_brand.read(self.path("facts"), {})
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["value"], "2015 年成立于深圳")
        self.assertIn("founded", [item["key"] for item in listing["keys"]])
        self.assertIn("other", [item["key"] for item in listing["keys"]])

        updated = geo_brand.write("PUT", self.path(f"facts/{fact['id']}"), {
            "fact_key": "founded", "label": "成立时间", "value": "2016 年成立于深圳",
        })
        self.assertEqual(updated["item"]["value"], "2016 年成立于深圳")

        detail = geo.read(self.path(), {})
        self.assertEqual(detail["project"]["counts"]["facts_confirmed"], 1)

        # Saving the same label again updates instead of duplicating.
        again = geo_brand.write("POST", self.path("facts"), {
            "fact_key": "founded", "label": "成立时间", "value": "2017 年",
        })
        self.assertEqual(again["item"]["id"], fact["id"])
        self.assertEqual(geo_brand.read(self.path("facts"), {})["total"], 1)

    def test_fact_requires_value_and_unknown_key_falls_back(self):
        with self.assertRaises(commander.ChatError):
            geo_brand.write("POST", self.path("facts"), {"fact_key": "founded", "value": "  "})
        item = geo_brand.write("POST", self.path("facts"), {
            "fact_key": "not-a-key", "label": "自定义", "value": "品牌资料",
        })["item"]
        self.assertEqual(item["fact_key"], "other")

    def test_extract_facts_uses_model_and_dedupes(self):
        payload = (
            '[{"fact_key":"founded","label":"成立时间","value":"2016 年成立于深圳"},'
            '{"fact_key":"main_products","label":"主营产品","value":"企业协作软件"}]'
        )
        with mock.patch.object(commander, "generate", return_value=[payload]):
            first = geo_brand.write("POST", self.path("facts/extract"), {})
            second = geo_brand.write("POST", self.path("facts/extract"), {})
        self.assertEqual(first["added"], 2)
        self.assertEqual(second["added"], 0)
        listing = geo_brand.read(self.path("facts"), {})
        self.assertEqual(listing["total"], 2)
        self.assertTrue(all(item["status"] == "pending" for item in listing["items"]))
        self.assertEqual(listing["items"][0]["source_name"], "AI 整理自知识库")

        ids = [item["id"] for item in listing["items"]]
        confirmed = geo_brand.write("POST", self.path("facts/review"),
                                    {"ids": ids, "action": "confirm"})
        self.assertEqual(confirmed["count"], 2)
        self.assertEqual(geo.read(self.path(), {})["project"]["counts"]["facts_confirmed"], 2)

        rejected = geo_brand.write("POST", self.path("facts/review"),
                                   {"ids": [ids[0]], "action": "reject"})
        self.assertEqual(rejected["count"], 1)
        deleted = geo_brand.write("POST", self.path("facts/review"),
                                  {"ids": [ids[1]], "action": "delete"})
        self.assertEqual(deleted["count"], 1)
        self.assertEqual(geo_brand.read(self.path("facts"), {})["total"], 1)

    def test_extract_facts_model_failure_is_reported(self):
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            with self.assertRaises(commander.ChatError):
                geo_brand.write("POST", self.path("facts/extract"), {})

    def test_extract_facts_without_sources(self):
        with commander.database() as db:
            db.execute("UPDATE geo_projects SET description='' WHERE id=? AND tenant_id=?",
                       (self.project_id, commander.tenant()))
        result = geo_brand.write("POST", self.path("facts/extract"), {})
        self.assertEqual(result["added"], 0)

    def test_unknown_fact_and_review_validation(self):
        with self.assertRaises(commander.ChatError):
            geo_brand.write("PUT", self.path("facts/999"), {"value": "x"})
        with self.assertRaises(commander.ChatError):
            geo_brand.write("POST", self.path("facts/review"), {"ids": [], "action": "confirm"})
        with self.assertRaises(commander.ChatError):
            geo_brand.write("POST", self.path("facts/review"), {"ids": [1], "action": "nope"})


class CompetitorTests(GeoBrandBase):
    def test_competitor_crud_and_limit(self):
        result = geo_brand.write("POST", self.path("competitors"), {
            "items": [{"name": "甲科技"}, {"name": "乙科技"}], "source": "discovered",
        })
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["source"], "discovered")
        self.assertEqual(result["items"][0]["source_name"], "体检发现")

        duplicate = geo_brand.write("POST", self.path("competitors"),
                                    {"items": [{"name": "甲科技"}]})
        self.assertEqual(duplicate["items"], [])
        self.assertIn("已经在竞品列表", duplicate["message"])

        competitor_id = result["items"][0]["id"]
        updated = geo_brand.write("PUT", self.path(f"competitors/{competitor_id}"), {
            "name": "甲科技", "aliases": ["甲"], "website": "https://a.example",
        })
        self.assertEqual(updated["item"]["aliases"], ["甲"])
        self.assertEqual(updated["item"]["website"], "https://a.example")

        for index in range(12):
            geo_brand.write("POST", self.path("competitors"),
                            {"items": [{"name": f"竞品{index}"}]})
        listing = geo_brand.read(self.path("competitors"), {})
        self.assertLessEqual(listing["total"], geo_brand.COMPETITOR_LIMIT)
        self.assertEqual(geo.read(self.path(), {})["project"]["counts"]["competitors"],
                         listing["total"])

        geo_brand.write("DELETE", self.path(f"competitors/{competitor_id}"), {})
        names = [item["name"] for item in
                 geo_brand.read(self.path("competitors"), {})["items"]]
        self.assertNotIn("甲科技", names)

    def test_suggest_filters_self_and_existing(self):
        geo_brand.write("POST", self.path("competitors"), {"items": [{"name": "甲科技"}]})
        payload = (
            '[{"name":"甲科技","reason":"已存在"},{"name":"丙科技","reason":"同品类"},'
            '{"name":"云杉科技","reason":"自己"}]'
        )
        with mock.patch.object(commander, "generate", return_value=[payload]):
            result = geo_brand.write("POST", self.path("competitors/suggest"), {})
        self.assertEqual([item["name"] for item in result["items"]], ["丙科技"])
        self.assertEqual(result["names"], ["丙科技"])

    def test_suggest_model_failure_is_reported(self):
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            with self.assertRaises(commander.ChatError):
                geo_brand.write("POST", self.path("competitors/suggest"), {})

    def test_unknown_competitor(self):
        with self.assertRaises(commander.ChatError):
            geo_brand.write("PUT", self.path("competitors/999"), {"name": "x"})
        with self.assertRaises(commander.ChatError):
            geo_brand.write("DELETE", self.path("competitors/999"), {})


class ShareOfVoiceTests(GeoBrandBase):
    def test_share_of_voice_from_answers(self):
        geo_brand.write("POST", self.path("competitors"),
                        {"items": [{"name": "甲科技", "aliases": ["甲"]}, {"name": "乙科技"}]})
        self.add_answer("推荐云杉科技和「甲科技」，另外「新锐品牌」也不错")
        self.add_answer("甲 和云杉都好，另外「新锐品牌」值得看")
        self.add_answer("「新锐品牌」性价比不错")
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
        self.add_answer("云杉科技 甲科技 「旧品牌」", created_at=old)

        sov = geo_brand.read(self.path("competitors/sov"), {"days": ["30"]})
        self.assertEqual(sov["total_answers"], 3)
        self.assertEqual(sov["brand"]["answers"], 2)

        by_name = {item["name"]: item for item in sov["items"]}
        self.assertEqual(by_name["甲科技"]["answers"], 2)
        self.assertEqual(by_name["乙科技"]["answers"], 0)
        self.assertEqual(by_name["甲科技"]["mention_rate"], 0.6667)
        self.assertEqual(sov["others"][0]["name"], "新锐品牌")
        self.assertEqual(sov["others"][0]["answers"], 3)
        self.assertEqual(sov["others"][0]["mention_rate"], 1.0)

    def test_share_of_voice_empty(self):
        sov = geo_brand.read(self.path("competitors/sov"), {})
        self.assertEqual(sov["total_answers"], 0)
        self.assertEqual(sov["brand"]["mention_rate"], 0)
        self.assertEqual(sov["items"], [])
        self.assertEqual(sov["others"], [])


class IsolationTests(GeoBrandBase):
    def test_empty_body_post_allowlist(self):
        base = f"/api/geo/projects/{self.project_id}"
        for suffix in ("resume", "facts/extract", "competitors/suggest"):
            with self.subTest(suffix=suffix):
                self.assertTrue(geo.empty_post(f"{base}/{suffix}"))
                self.assertTrue(geo.empty_post("/agent-api" + f"{base}/{suffix}"))
        self.assertTrue(geo.empty_post("/api/geo/runs/1/cancel"))
        self.assertTrue(geo.empty_post("/api/geo/runs/1/retry/estimate"))
        for path in (f"{base}/facts", f"{base}/competitors",
                     "/api/memory/clear", "/api/acquisition/business-profile"):
            with self.subTest(path=path):
                self.assertFalse(geo.empty_post(path))

    def test_facts_and_competitors_are_tenant_scoped(self):
        geo_brand.write("POST", self.path("facts"),
                        {"fact_key": "founded", "value": "2016 年"})
        geo_brand.write("POST", self.path("competitors"), {"items": [{"name": "甲科技"}]})
        other = tenancy.create_tenant("品牌测试租户")
        with tenancy.scope(other):
            self.assertEqual(geo.read(geo.PROJECTS, {})["total"], 0)
            with self.assertRaises(commander.ChatError):
                geo_brand.read(self.path("facts"), {})
            with self.assertRaises(commander.ChatError):
                geo_brand.read(self.path("competitors"), {})


if __name__ == "__main__":
    unittest.main()
