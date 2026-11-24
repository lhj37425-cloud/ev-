"""GEO content workshop, scoring/checking, platform variants and manual publishing."""

import json
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
import geo_content
import geo_runs
import tenancy


PROJECT = {
    "brand_name": "云杉科技", "aliases": ["云杉"], "website": "https://example.com",
    "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
    "market": "cn", "locales": [],
}
ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN")


class ContentBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["EV_PAY_MOCK"] = "0"
        os.environ.pop("EV_SEED_DEMO", None)
        os.environ.pop("EV_TRIAL_ORIGIN", None)
        tenancy.activate(tenancy.local_identity())
        self.project_id = geo.write("POST", geo.PROJECTS, PROJECT)["project"]["id"]
        self.path = lambda suffix="": (f"/api/geo/projects/{self.project_id}/{suffix}".rstrip("/")
                                       if suffix else f"/api/geo/projects/{self.project_id}")
        geo.write("POST", self.path("facts"), {
            "fact_key": "founded", "label": "成立时间", "value": "2016 年成立于深圳",
        })
        geo.write("POST", self.path("questions"), {
            "items": [{"text": "企业软件哪家好？", "intent": "compare", "layer": "category"}],
        })
        geo.write("POST", self.path("competitors"), {"items": [{"name": "甲科技"}]})

    def tearDown(self):
        tenancy.activate(None)
        commander.DATA_DIR = self.old_dir
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def create(self, **extra):
        body = {"question": "企业软件哪家好？", "genre": "qa_answer",
                "platforms": ["website", "zhihu"]}
        body.update(extra)
        return geo.write("POST", self.path("contents"), body)["content"]

    def content(self, content_id):
        return geo.read(f"/api/geo/contents/{content_id}", {})["content"]

    def wait_idle(self, content_id, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = self.content(content_id)
            if not item["busy"]:
                return item
            time.sleep(0.05)
        self.fail("content worker did not finish")

    def write_body(self, content_id, body, title="企业软件怎么选"):
        return geo.write("PUT", f"/api/geo/contents/{content_id}", {
            "title": title, "body": body,
        })["content"]

    def pass_check(self, content_id):
        body = ("# 企业软件哪家好\n\n云杉科技成立于 2016 年，总部在深圳。\n\n"
                "## 选择要点\n\n- 对比协作能力\n- 对比部署方式\n")
        self.write_body(content_id, body)
        return geo.write("POST", f"/api/geo/contents/{content_id}/check", {})["content"]

    def add_run(self):
        with commander.database() as db:
            geo_runs.initialize(db)
            question = db.execute("SELECT id FROM geo_questions").fetchone()[0]
            stamp = geo_runs.now()
            run_id = db.execute(
                "INSERT INTO geo_runs (tenant_id,project_id,status,engines,samples,total,done,"
                "credits,created_at,finished_at,method_note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (commander.tenant(), self.project_id, "done", '["doubao"]', 1, 2, 2,
                 0.4, stamp, stamp, geo_runs.METHOD_NOTE),
            ).lastrowid
            for text in ("推荐甲科技", "推荐甲科技和乙科技"):
                db.execute(
                    "INSERT INTO geo_answers (tenant_id,project_id,run_id,question_id,"
                    "engine_key,engine_name,attempt,status,text,error,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (commander.tenant(), self.project_id, run_id, question, "doubao",
                     "豆包（模拟）", 0, "ok", text, "", stamp),
                )
        return run_id


class ContentBasics(ContentBase):
    def test_meta_and_create_list(self):
        meta = geo.read("/api/geo/content-meta", {"market": ["cn"]})
        self.assertEqual(meta["genres"][0]["key"], "qa_answer")
        self.assertTrue(any(item["key"] == "zhihu" for item in meta["targets"]))
        self.assertTrue(any(item["kind"] == "channel" for item in meta["channel_kinds"]))
        self.assertEqual(meta["models"]["write"], commander.MODEL)

        content = self.create()
        self.assertEqual(content["status"], "draft")
        self.assertEqual([item["key"] for item in content["platforms"]], ["website", "zhihu"])
        self.assertFalse(content["can_publish"])
        listing = geo.read(self.path("contents"), {"page": ["1"], "page_size": ["20"]})
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["title"], "企业软件哪家好？")
        self.assertEqual(listing["items"][0]["chars"], 0)

        updated = geo.write("PUT", f"/api/geo/contents/{content['id']}", {
            "title": "改过的标题", "body": "正文", "summary": "摘要", "brief": "要求",
        })["content"]
        self.assertEqual(updated["title"], "改过的标题")
        archived = geo.write("PUT", f"/api/geo/contents/{content['id']}",
                             {"status": "archived"})["content"]
        self.assertEqual(archived["status"], "archived")
        listing = geo.read(self.path("contents"), {})
        self.assertEqual(listing["total"], 0)
        restored = geo.write("PUT", f"/api/geo/contents/{content['id']}",
                             {"status": "draft"})["content"]
        self.assertEqual(restored["status"], "draft")
        geo.write("DELETE", f"/api/geo/contents/{content['id']}", {})
        with self.assertRaises(commander.ChatError):
            self.content(content["id"])

    def test_create_requires_question_and_valid_platforms(self):
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("contents"), {"question": ""})
        content = self.create(platforms=["zhihu", "nope"])
        self.assertEqual([item["key"] for item in content["platforms"]], ["zhihu"])
        question = geo.read(self.path("questions"), {})["items"][0]
        from_question = self.create(question="", question_id=question["id"])
        self.assertEqual(from_question["question"], question["text"])
        self.assertEqual(from_question["question_id"], question["id"])

    def test_generate_worker_and_failure(self):
        content = self.create()
        payload = json.dumps({
            "title": "AI 标题", "body": "# AI 正文\n\n云杉科技成立于 2016 年。",
            "summary": "AI 摘要", "zh_outline": [],
        }, ensure_ascii=False)
        with mock.patch.object(commander, "generate", return_value=[payload]):
            started = geo.write("POST", f"/api/geo/contents/{content['id']}/generate", {})["content"]
            self.assertEqual(started["busy"], "generate")
            item = self.wait_idle(content["id"])
        self.assertEqual(item["title"], "AI 标题")
        self.assertIn("AI 正文", item["body"])
        self.assertEqual(item["version"], 2)
        self.assertTrue(item["evidence"])
        self.assertTrue(any(entry["id"] == "F1" for entry in item["evidence"]))
        self.assertFalse(item["error"])

        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            geo.write("POST", f"/api/geo/contents/{content['id']}/generate", {})
            item = self.wait_idle(content["id"])
        self.assertIn("初稿没写出来", item["error"])

    def test_score_and_compliance(self):
        content = self.create()
        item = self.write_body(content["id"], "# 标题\n\n" + "云杉科技成立于 2016 年。" * 30 +
                               "\n\n- 列表一\n- 列表二\n")
        scored = geo.write("POST", f"/api/geo/contents/{content['id']}/score", {})["content"]
        self.assertIsNotNone(scored["score_total"])
        self.assertTrue(scored["score_fresh"])
        self.assertGreaterEqual(scored["score"]["total"], 0)
        self.assertEqual(len(scored["score"]["items"]), 6)

        risky = self.write_body(content["id"], "云杉科技是行业第一，成立于 2016 年。")
        scored = geo.write("POST", f"/api/geo/contents/{content['id']}/score", {})["content"]
        self.assertTrue(any(entry["minus"] > 0 for entry in scored["score"]["penalties"]))

    def test_check_pass_review_block_and_approve(self):
        content = self.create()
        passed = self.pass_check(content["id"])
        self.assertEqual(passed["gate"]["decision"], "pass")
        self.assertTrue(passed["can_publish"])
        self.assertEqual(passed["status"], "ready")
        self.assertTrue(passed["gate_detail"]["valid"])

        edited = geo.write("PUT", f"/api/geo/contents/{content['id']}",
                           {"body": "改过之后正文"})["content"]
        self.assertFalse(edited["gate"]["valid"])
        self.assertFalse(edited["can_publish"])

        self.write_body(content["id"], "云杉科技提供免费试用。云杉科技成立于 2016 年。")
        review = geo.write("POST", f"/api/geo/contents/{content['id']}/check", {})["content"]
        self.assertEqual(review["gate"]["decision"], "review")
        self.assertFalse(review["can_publish"])
        approved = geo.write("POST", f"/api/geo/contents/{content['id']}/approve",
                             {"reason": "验收放行"})["content"]
        self.assertEqual(approved["gate"]["decision"], "approved")
        self.assertTrue(approved["can_publish"])
        self.assertEqual(approved["gate_detail"]["approved_reason"], "验收放行")

        self.write_body(content["id"], "云杉科技是行业第一，成立于 2016 年。")
        blocked = geo.write("POST", f"/api/geo/contents/{content['id']}/check", {})["content"]
        self.assertEqual(blocked["gate"]["decision"], "block")
        self.assertTrue(blocked["gate_detail"]["compliance"])

    def test_apply_suggestion(self):
        content = self.create()
        self.write_body(content["id"], "云杉科技提供免费试用。云杉科技成立于 2016 年。")
        checked = geo.write("POST", f"/api/geo/contents/{content['id']}/check", {})["content"]
        claims = checked["gate_detail"]["claims"]
        index = next(i for i, item in enumerate(claims) if item["verdict"] == "unverified")
        changed = geo.write("POST", f"/api/geo/contents/{content['id']}/apply-suggestion",
                            {"kind": "claim", "index": index, "text": "云杉科技成立于 2016 年。"})["content"]
        self.assertNotIn(claims[index]["quote"], changed["body"])
        self.assertIsNone(changed["gate"])
        self.assertFalse(changed["can_publish"])

    def test_adapt_and_variants(self):
        content = self.create()
        self.pass_check(content["id"])
        payload = json.dumps({
            "zhihu": {"title": "知乎版", "body": "知乎正文，云杉科技成立于 2016 年。",
                      "tags": ["企业软件", "协作"]},
        }, ensure_ascii=False)
        with mock.patch.object(commander, "generate", return_value=[payload]):
            geo.write("POST", f"/api/geo/contents/{content['id']}/adapt",
                      {"platforms": ["zhihu"]})
            item = self.wait_idle(content["id"])
        self.assertIn("zhihu", item["variants"])
        self.assertFalse(item["variants"]["zhihu"]["stale"])
        self.assertGreater(item["variants"]["zhihu"]["similarity"], 0)

        updated = geo.write("PUT", f"/api/geo/contents/{content['id']}/variants/zhihu",
                            {"title": "手改标题", "body": "手改正文", "tags": ["a"]})["content"]
        self.assertEqual(updated["variants"]["zhihu"]["title"], "手改标题")
        removed = geo.write("PUT", f"/api/geo/contents/{content['id']}/variants/zhihu",
                            {"remove": True})["content"]
        self.assertNotIn("zhihu", removed["variants"])

    def test_tenant_isolation(self):
        content = self.create()
        other = tenancy.create_tenant("内容测试租户")
        with tenancy.scope(other):
            with self.assertRaises(commander.ChatError):
                self.content(content["id"])
            with self.assertRaises(commander.ChatError):
                geo.read(self.path("contents"), {})

    def test_empty_post_allowlist(self):
        base = f"/api/geo/contents/1"
        self.assertTrue(geo.empty_post(f"{base}/generate"))
        self.assertTrue(geo.empty_post(f"{base}/score"))
        self.assertTrue(geo.empty_post(f"{base}/check"))
        self.assertFalse(geo.empty_post(f"{base}/adapt"))
        self.assertFalse(geo.empty_post(f"{base}/approve"))
        self.assertFalse(geo.empty_post(f"{base}/tickets"))


class Publishing(ContentBase):
    def test_manual_ticket_creates_publication_and_coverage(self):
        content = self.create()
        checked = self.pass_check(content["id"])
        self.assertTrue(checked["can_publish"])
        ticket = geo.write("POST", f"/api/geo/contents/{content['id']}/tickets",
                           {"platform": "zhihu"})["ticket"]
        self.assertEqual(ticket["status"], "todo")
        self.assertIn("知乎", ticket["platform_name"])
        self.assertTrue(ticket["draft_html"])
        listing = geo.read(self.path("tickets"), {"status": ["todo"]})
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["todo"], 1)

        done = geo.write("PUT", f"/api/geo/tickets/{ticket['id']}", {
            "status": "done", "url": "https://zhuanlan.zhihu.com/p/1",
        })["ticket"]
        self.assertEqual(done["status"], "done")
        publications = geo.read(self.path("publications"), {})
        self.assertEqual(publications["total"], 1)
        self.assertEqual(publications["items"][0]["platform_name"], "知乎")
        self.assertEqual(publications["items"][0]["url"], "https://zhuanlan.zhihu.com/p/1")
        detail = self.content(content["id"])
        self.assertEqual(detail["status"], "published")
        self.assertEqual(len(detail["publications"]), 1)
        with self.assertRaises(commander.ChatError):
            geo.write("DELETE", f"/api/geo/contents/{content['id']}", {})

        geo.write("PUT", f"/api/geo/publications/{publications['items'][0]['id']}/url",
                  {"url": "https://zhuanlan.zhihu.com/p/2"})
        self.assertEqual(geo.read(self.path("publications"), {})["items"][0]["url"],
                         "https://zhuanlan.zhihu.com/p/2")
        with self.assertRaises(commander.ChatError):
            geo.write("PUT", f"/api/geo/publications/{publications['items'][0]['id']}/url",
                      {"url": "not-a-url"})

    def test_ticket_requires_publishable(self):
        content = self.create()
        with self.assertRaises(commander.ChatError):
            geo.write("POST", f"/api/geo/contents/{content['id']}/tickets",
                      {"platform": "zhihu"})
        self.pass_check(content["id"])
        with self.assertRaises(commander.ChatError):
            geo.write("POST", f"/api/geo/contents/{content['id']}/tickets",
                      {"platform": "nope"})

    def test_report_publishing_and_matrix_coverage(self):
        content = self.create()
        self.pass_check(content["id"])
        ticket = geo.write("POST", f"/api/geo/contents/{content['id']}/tickets",
                           {"platform": "zhihu"})["ticket"]
        geo.write("PUT", f"/api/geo/tickets/{ticket['id']}", {
            "status": "done", "url": "https://zhuanlan.zhihu.com/p/1",
        })
        report = geo.read(self.path("reports"), {"period": ["week"]})["report"]
        self.assertIsNotNone(report["publishing"])
        self.assertEqual(report["publishing"]["total"], 1)
        self.assertEqual(report["publishing"]["domains"][0]["platform_name"], "知乎")

        geo.write("PUT", self.path("kw-matrix"), {
            "roots": {"industry": [{"text": "企业软件", "tier": "S"}]},
        })
        matrix = geo.read(self.path("kw-matrix"), {})
        self.assertEqual(matrix["docs"], 1)
        terms = geo.read(self.path("kw-matrix/terms"), {"state": ["covered"]})
        self.assertEqual(terms["total"], 1)
        self.assertTrue(terms["items"][0]["covered"])

    def test_opportunities_and_layer_draft(self):
        self.add_run()
        opportunities = geo.read(self.path("opportunities"), {})
        self.assertTrue(opportunities["has_run"])
        self.assertTrue(any(item["kind"] == "gap" for item in opportunities["items"]))
        geo.write("PUT", self.path("kw-matrix"), {
            "roots": {"industry": [{"text": "企业软件", "tier": "S"}]},
        })
        draft = geo.write("POST", f"{self.path('kw-matrix')}/layers/category/content", {})
        self.assertIn("id", draft["content"])
        detail = self.content(draft["content"]["id"])
        self.assertEqual(detail["source_kind"], "matrix")
        self.assertEqual(detail["source_ref"]["layer"], "category")


if __name__ == "__main__":
    unittest.main()
