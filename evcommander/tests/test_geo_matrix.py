"""GEO keyword matrix.

Covers the five-layer term library used by the archived Questions view:
root groups, term expansion, tier overrides, manual terms, AI drafting,
rewriting terms into questions, document export, empty-body compatibility
and tenant isolation.

Run from evcommander/:
    python -m unittest discover -s tests -v
"""

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
import geo_matrix
import geo_runs
import tenancy


PROJECT = {
    "brand_name": "云杉科技", "aliases": ["云杉"], "website": "https://example.com",
    "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
    "market": "cn", "locales": [],
}
ROOTS = {
    "region": [{"text": "深圳", "tier": "A"}],
    "industry": [{"text": "企业软件", "tier": "S"}],
    "merchant": [{"text": "公司", "tier": "B"}],
    "scene": [{"text": "远程办公", "tier": "B"}],
    "slang": [{"text": "哪家好", "tier": "B"}],
}
ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN")


class GeoMatrixBase(unittest.TestCase):
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
        base = f"/api/geo/projects/{self.project_id}/kw-matrix"
        return f"{base}/{suffix}" if suffix else base

    def terms(self, **query):
        params = {"page": ["1"], "page_size": ["100"]}
        params.update({key: [value] for key, value in query.items()})
        return geo.read(self.path("terms"), params)

    def term_texts(self):
        return {item["text"]: item for item in self.terms()["items"]}


class MatrixBasics(GeoMatrixBase):
    def test_initial_matrix_shape(self):
        result = geo.read(self.path(), {})
        self.assertTrue(result["ok"])
        self.assertFalse(result["has_matrix"])
        self.assertTrue(result["has_profile"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["docs"], 0)
        self.assertEqual([layer["key"] for layer in result["layers"]],
                         ["entity", "category", "scene", "slang", "question"])
        self.assertEqual([tier["key"] for tier in result["tiers"]], ["S", "A", "B"])
        self.assertEqual([group["key"] for group in result["groups"]],
                         ["region", "industry", "merchant", "scene", "slang"])
        self.assertEqual(set(result["roots"]), {"region", "industry", "merchant", "scene", "slang"})
        self.assertEqual(result["draft"]["blockers"], [])
        self.assertIsNone(result["draft"]["credits"])

    def test_missing_profile_blocks_draft(self):
        bare = geo.write("POST", geo.PROJECTS, {"brand_name": "空档案", "market": "cn"})
        base = f"/api/geo/projects/{bare['project']['id']}/kw-matrix"
        result = geo.read(base, {})
        self.assertFalse(result["has_profile"])
        self.assertTrue(result["draft"]["blockers"])
        with self.assertRaises(commander.ChatError):
            geo.write("POST", f"{base}/draft", {})

    def test_save_roots_expands_terms(self):
        result = geo.write("PUT", self.path(), {"roots": ROOTS})
        self.assertTrue(result["has_matrix"])
        self.assertEqual(result["total"], 8)
        layers = {layer["key"]: layer for layer in result["layers"]}
        self.assertEqual(layers["category"]["counts"], {"S": 1, "A": 0, "B": 0})
        self.assertEqual(layers["entity"]["counts"], {"S": 0, "A": 1, "B": 2})
        self.assertEqual(layers["scene"]["counts"], {"S": 0, "A": 0, "B": 3})
        self.assertEqual(layers["slang"]["counts"], {"S": 0, "A": 0, "B": 1})
        texts = self.term_texts()
        self.assertEqual(texts["企业软件"]["layer"], "category")
        self.assertEqual(texts["企业软件"]["tier"], "S")
        self.assertEqual(texts["深圳企业软件"]["layer"], "entity")
        self.assertEqual(texts["深圳企业软件"]["tier"], "A")
        self.assertEqual(texts["深圳企业软件公司"]["layer"], "entity")
        self.assertEqual(texts["深圳企业软件公司"]["tier"], "B")
        self.assertEqual(texts["企业软件远程办公"]["layer"], "scene")
        self.assertEqual(texts["哪家好"]["layer"], "slang")
        self.assertEqual(texts["哪家好"]["source"], "rule")

    def test_save_validation(self):
        with self.assertRaises(commander.ChatError):
            geo.write("PUT", self.path(), {"roots": "not-a-dict"})
        with self.assertRaises(commander.ChatError):
            # An empty body must not silently wipe the saved roots.
            geo.write("PUT", self.path(), {})
        result = geo.write("PUT", self.path(), {
            "roots": {"industry": [
                {"text": "企业软件", "tier": "S"},
                {"text": "企业软件", "tier": "B"},
                {"text": "x", "tier": "A"},
                {"text": "  ", "tier": "A"},
            ]},
        })
        self.assertEqual(result["total"], 1)
        self.assertEqual(self.term_texts()["企业软件"]["tier"], "S")

    def test_unknown_project_is_rejected(self):
        with self.assertRaises(commander.ChatError):
            geo.read("/api/geo/projects/999999/kw-matrix", {})
        with self.assertRaises(commander.ChatError):
            geo.write("PUT", "/api/geo/projects/999999/kw-matrix", {"roots": {}})

    def test_layer_judgement_thresholds(self):
        self.assertIsNone(geo_matrix.judge_layer(0.9, 2))
        self.assertEqual(geo_matrix.judge_layer(0.7, 5)["level"], "ok")
        self.assertEqual(geo_matrix.judge_layer(0.4, 5)["level"], "warm")
        self.assertEqual(geo_matrix.judge_layer(0.1, 5)["level"], "err")


class MatrixTerms(GeoMatrixBase):
    def setUp(self):
        super().setUp()
        geo.write("PUT", self.path(), {"roots": {
            "industry": [{"text": "企业软件", "tier": "S"}],
            "slang": [{"text": "哪家好", "tier": "B"}],
        }})

    def test_add_terms_and_duplicates(self):
        result = geo.write("POST", self.path("terms"), {
            "layer": "question", "tier": "A",
            "texts": ["云杉科技怎么样", "哪家好", "哪家好"],
        })
        self.assertEqual(result["added"], 1)
        self.assertIn("跳过", result["message"])
        terms = self.term_texts()
        self.assertIn("云杉科技怎么样", terms)
        self.assertEqual(terms["云杉科技怎么样"]["source"], "manual")
        self.assertEqual(terms["哪家好"]["source"], "rule")
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("terms"), {"layer": "nope", "texts": ["有效词"]})
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("terms"), {"layer": "entity", "texts": ["x"]})

    def test_batch_tier_and_delete(self):
        rows = self.terms()["items"]
        target = next(row for row in rows if row["text"] == "企业软件")
        updated = geo.write("POST", self.path("terms/batch"), {
            "ids": [target["id"]], "action": "tier", "tier": "A",
        })
        self.assertEqual(updated["count"], 1)
        self.assertEqual(self.term_texts()["企业软件"]["tier"], "A")
        self.assertTrue(self.term_texts()["企业软件"]["tier_set"])
        deleted = geo.write("POST", self.path("terms/batch"), {
            "ids": [target["id"]], "action": "delete",
        })
        self.assertEqual(deleted["count"], 1)
        self.assertNotIn("企业软件", self.term_texts())
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("terms/batch"), {"ids": [], "action": "delete"})
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("terms/batch"), {"ids": [1], "action": "nope"})

    def test_terms_filters_and_pagination(self):
        geo.write("PUT", self.path(), {"roots": ROOTS})
        entity = self.terms(layer="entity")["items"]
        self.assertTrue(entity)
        self.assertTrue(all(item["layer"] == "entity" for item in entity))
        strong = self.terms(tier="S")["items"]
        self.assertEqual([item["text"] for item in strong], ["企业软件"])
        keyword = self.terms(keyword="深圳")["items"]
        self.assertTrue(keyword)
        self.assertTrue(all("深圳" in item["text"] for item in keyword))
        page = geo.read(self.path("terms"), {"page": ["2"], "page_size": ["2"]})
        self.assertEqual(len(page["items"]), 2)
        self.assertGreater(page["total"], 4)
        self.assertEqual(self.terms(state="converted")["total"], 0)
        self.assertEqual(self.terms(state="covered")["total"], 0)

    def test_save_keeps_manual_tier_and_converted_terms(self):
        manual = geo.write("POST", self.path("terms"), {
            "layer": "entity", "tier": "B", "texts": ["手工词"],
        })
        self.assertEqual(manual["added"], 1)
        target = next(row for row in self.terms()["items"] if row["text"] == "企业软件")
        geo.write("POST", self.path("terms/batch"), {
            "ids": [target["id"]], "action": "tier", "tier": "B",
        })
        question_term = geo.write("POST", self.path("terms"), {
            "layer": "question", "tier": "A", "texts": ["云杉科技怎么样"],
        })
        self.assertEqual(question_term["added"], 1)
        term = next(row for row in self.terms()["items"] if row["text"] == "云杉科技怎么样")
        with mock.patch.object(commander, "generate",
                               return_value=[json.dumps(["云杉科技怎么样？"], ensure_ascii=False)]):
            converted = geo.write("POST", self.path("convert"), {"ids": [term["id"]]})
        geo.write("POST", self.path("questions"), {
            "items": [{"term_id": term["id"], "text": converted["items"][0]["text"]}],
        })
        result = geo.write("PUT", self.path(), {"roots": {
            "industry": [{"text": "协作工具", "tier": "S"}],
        }})
        texts = self.term_texts()
        self.assertIn("协作工具", texts)
        self.assertIn("手工词", texts)
        self.assertIn("企业软件", texts)
        self.assertIn("云杉科技怎么样", texts)
        self.assertNotIn("哪家好", texts)
        self.assertEqual(texts["企业软件"]["tier"], "B")
        self.assertTrue(texts["云杉科技怎么样"]["converted"])
        self.assertEqual(result["total"], 4)


class MatrixAI(GeoMatrixBase):
    def test_draft_uses_model_and_saves_roots(self):
        payload = json.dumps({
            "roots": {
                "region": [{"text": "深圳", "tier": "A"}],
                "industry": [{"text": "企业软件", "tier": "S"}],
                "merchant": [], "scene": [],
                "slang": [{"text": "哪家好", "tier": "B"}],
            },
            "layers": {
                "entity": {"pain": "找不到本地服务商", "service": "本地部署和上门服务"},
                "category": {"pain": "不知道选哪类工具", "service": "一站式协作平台"},
            },
        }, ensure_ascii=False)
        with mock.patch.object(commander, "generate", return_value=[payload]):
            result = geo.write("POST", self.path("draft"), {})
        self.assertEqual(result["total"], 3)
        self.assertTrue(result["has_matrix"])
        self.assertGreater(result["credits"], 0)
        layers = {layer["key"]: layer for layer in result["layers"]}
        self.assertEqual(layers["entity"]["pain"], "找不到本地服务商")
        self.assertEqual(layers["entity"]["service"], "本地部署和上门服务")
        with commander.database() as db:
            state = db.execute(
                "SELECT drafted_at FROM geo_kw_state WHERE project_id=? AND tenant_id=?",
                (self.project_id, commander.tenant()),
            ).fetchone()
        self.assertTrue(state["drafted_at"])

    def test_draft_rejects_invalid_model_output(self):
        with mock.patch.object(commander, "generate", return_value=["not json"]):
            with self.assertRaises(commander.ChatError):
                geo.write("POST", self.path("draft"), {})
        with mock.patch.object(commander, "generate",
                               return_value=[json.dumps({"roots": {}})]):
            with self.assertRaises(commander.ChatError):
                geo.write("POST", self.path("draft"), {})
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            with self.assertRaises(commander.ChatError):
                geo.write("POST", self.path("draft"), {})

    def test_convert_rewrites_terms_and_reports_impact(self):
        geo.write("PUT", self.path(), {"roots": {
            "industry": [{"text": "企业软件", "tier": "S"}],
            "slang": [{"text": "哪家好", "tier": "B"}],
        }})
        ids = [row["id"] for row in self.terms()["items"]]
        with mock.patch.object(commander, "generate",
                               return_value=[json.dumps(["企业软件哪家好？", "哪家好？"],
                                                        ensure_ascii=False)]):
            result = geo.write("POST", self.path("convert"), {"ids": ids, "locale": "zh"})
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["term"], "企业软件")
        self.assertEqual(result["items"][0]["layer_name"], "找品类")
        self.assertFalse(result["items"][0]["exists"])
        self.assertGreater(result["credits"], 0)
        impact = result["impact"]
        for key in ("limit", "active", "per_question", "runs_per_month", "confirm_run",
                    "confirm_month", "engines", "samples", "run_now", "schedule_name"):
            self.assertIn(key, impact)
        self.assertEqual(impact["limit"], 200)
        self.assertGreater(impact["per_question"], 0)
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("convert"), {"ids": []})
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            with self.assertRaises(commander.ChatError):
                geo.write("POST", self.path("convert"), {"ids": [ids[0]]})

    def test_convert_limit(self):
        ids = list(range(1, 32))
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("convert"), {"ids": ids})

    def test_add_questions_sets_layer_and_marks_converted(self):
        geo.write("PUT", self.path(), {"roots": {
            "industry": [{"text": "企业软件", "tier": "S"}],
        }})
        term = self.terms()["items"][0]
        with mock.patch.object(commander, "generate",
                               return_value=[json.dumps(["企业软件哪家好？"], ensure_ascii=False)]):
            converted = geo.write("POST", self.path("convert"), {"ids": [term["id"]]})
        item = converted["items"][0]
        added = geo.write("POST", self.path("questions"), {
            "items": [{"term_id": term["id"], "text": item["text"],
                       "intent": item["intent"], "locale": item["locale"]}],
        })
        self.assertEqual(added["added"], 1)
        questions = geo_runs.read(f"/api/geo/projects/{self.project_id}/questions",
                                  {"page": ["1"], "page_size": ["20"]})
        self.assertEqual([layer["key"] for layer in questions["layers"]],
                         ["entity", "category", "scene", "slang", "question"])
        self.assertEqual(questions["items"][0]["text"], item["text"])
        self.assertEqual(questions["items"][0]["layer"], "category")
        self.assertEqual(questions["items"][0]["layer_name"], "找品类")
        converted_term = next(row for row in self.terms()["items"] if row["id"] == term["id"])
        self.assertTrue(converted_term["converted"])
        again = geo.write("POST", self.path("questions"), {
            "items": [{"term_id": term["id"], "text": item["text"]}],
        })
        self.assertEqual(again["added"], 0)
        self.assertIn("跳过", again["message"])
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("questions"), {"items": []})

    def test_layer_stats_and_delete_resets_converted(self):
        geo.write("PUT", self.path(), {"roots": {
            "industry": [{"text": "企业软件", "tier": "S"}],
        }})
        term = self.terms()["items"][0]
        with mock.patch.object(commander, "generate",
                               return_value=[json.dumps(["企业软件哪家好？"], ensure_ascii=False)]):
            converted = geo.write("POST", self.path("convert"), {"ids": [term["id"]]})
        geo.write("POST", self.path("questions"), {
            "items": [{"term_id": term["id"], "text": converted["items"][0]["text"]}],
        })
        with commander.database() as db:
            question = db.execute(
                "SELECT id FROM geo_questions WHERE project_id=? AND tenant_id=?",
                (self.project_id, commander.tenant()),
            ).fetchone()
        stats = geo_runs.read(f"/api/geo/projects/{self.project_id}/layers", {})
        self.assertEqual(stats["layered"], 1)
        self.assertEqual(stats["unlayered"], 0)
        self.assertEqual(stats["rows"][0]["layer"], "category")
        self.assertEqual(stats["rows"][0]["questions"], 1)
        self.assertEqual(stats["rows"][0]["sample_n"], 0)
        self.assertIsNone(stats["rows"][0]["tier"])

        with commander.database() as db:
            run_id = db.execute(
                "INSERT INTO geo_runs (tenant_id,project_id,status,total,done,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (commander.tenant(), self.project_id, "done", 3, 3, geo_runs.now()),
            ).lastrowid
            for text in ("云杉科技很稳", "推荐云杉科技", "云杉科技和甲科技都可以"):
                db.execute(
                    "INSERT INTO geo_answers (tenant_id,project_id,run_id,question_id,engine_key,"
                    "engine_name,attempt,status,text,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (commander.tenant(), self.project_id, run_id, question["id"], "doubao",
                     "豆包（模拟）", 0, "ok", text, "", geo_runs.now()),
                )
        stats = geo_runs.read(f"/api/geo/projects/{self.project_id}/layers",
                              {"run_id": [str(run_id)]})
        row = stats["rows"][0]
        self.assertEqual(row["sample_n"], 3)
        self.assertEqual(row["mention_rate"], 1.0)
        self.assertEqual(row["tier"]["level"], "ok")
        self.assertIsNone(row["top1_rate"])

        deleted = geo.write("POST", f"/api/geo/projects/{self.project_id}/questions/delete",
                            {"ids": [question["id"]]})
        self.assertEqual(deleted["count"], 1)
        self.assertFalse(self.term_texts()["企业软件"]["converted"])

    def test_doc_export_shape(self):
        geo.write("PUT", self.path(), {"roots": ROOTS, "layers": {
            "entity": {"pain": "找不到本地服务商", "service": "本地部署"},
        }})
        doc = geo.read(self.path("doc"), {})["doc"]
        self.assertIn("云杉科技", doc["title"])
        self.assertEqual(doc["total"], 8)
        self.assertEqual(len(doc["layers"]), 5)
        self.assertEqual(len(doc["notes"]), 4)
        self.assertEqual(doc["project"]["industry"], "企业软件")
        entity = doc["layers"][0]
        self.assertEqual(entity["pain"], "找不到本地服务商")
        self.assertEqual(entity["service"], "本地部署")
        self.assertEqual(sum(len(layer["terms"]) for layer in doc["layers"]), 8)

    def test_layer_content_creates_draft(self):
        result = geo.write("POST", self.path("layers/entity/content"), {})
        self.assertTrue(result["ok"])
        self.assertTrue(result["content"]["id"])
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("layers/nope/content"), {})


class MatrixRouting(GeoMatrixBase):
    def test_empty_post_whitelist(self):
        base = f"/api/geo/projects/{self.project_id}/kw-matrix"
        self.assertTrue(geo.empty_post(f"{base}/draft"))
        self.assertTrue(geo.empty_post("/agent-api" + f"{base}/draft"))
        self.assertTrue(geo.empty_post(f"{base}/layers/entity/content"))
        self.assertTrue(geo.empty_post("/agent-api" + f"{base}/layers/entity/content"))
        for path in (base, f"{base}/terms", f"{base}/terms/batch",
                     f"{base}/convert", f"{base}/questions"):
            with self.subTest(path=path):
                self.assertFalse(geo.empty_post(path))

    def test_matrix_is_tenant_scoped(self):
        geo.write("PUT", self.path(), {"roots": ROOTS})
        other = tenancy.create_tenant("词库测试租户")
        with tenancy.scope(other):
            self.assertEqual(geo.read(geo.PROJECTS, {})["total"], 0)
            with self.assertRaises(commander.ChatError):
                geo.read(self.path(), {})
            with self.assertRaises(commander.ChatError):
                geo.write("PUT", self.path(), {"roots": {}})


if __name__ == "__main__":
    unittest.main()
