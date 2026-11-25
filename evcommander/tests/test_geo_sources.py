"""GEO citation parsing: sources page, report citations and cited publications."""

import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import geo
import geo_content
import geo_hub
import geo_report
import geo_runs
import geo_sources
import tenancy


PROJECT = {
    "brand_name": "云杉科技", "aliases": ["云杉"], "website": "https://www.yunshan.com",
    "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
    "market": "cn", "locales": [],
}
PASS_BODY = ("# 企业软件哪家好\n\n云杉科技成立于 2016 年，总部在深圳。\n\n"
             "## 选择要点\n\n- 对比协作能力\n")
ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN")


class SourcesBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["EV_PAY_MOCK"] = "0"
        for key in ("EV_SEED_DEMO", "EV_TRIAL_ORIGIN"):
            os.environ.pop(key, None)
        tenancy.activate(tenancy.local_identity())
        self.project_id = geo.write("POST", geo.PROJECTS, PROJECT)["project"]["id"]
        self.path = lambda suffix="": (f"/api/geo/projects/{self.project_id}/{suffix}".rstrip("/")
                                       if suffix else f"/api/geo/projects/{self.project_id}")
        geo.write("POST", self.path("facts"), {
            "fact_key": "founded", "label": "成立时间", "value": "2016 年成立于深圳",
        })
        geo.write("POST", self.path("questions"), {"items": [
            {"text": "企业软件哪家好？", "intent": "compare", "layer": "category"},
            {"text": "远程办公怎么选？", "intent": "purchase", "layer": "scene"},
        ]})
        geo.write("POST", self.path("competitors"), {
            "items": [{"name": "甲科技", "website": "https://www.jiatech.com"}],
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

    def add_run(self, texts, engine="doubao", status="done"):
        with commander.database() as db:
            geo_runs.initialize(db)
            question = db.execute("SELECT id FROM geo_questions WHERE project_id=? ORDER BY id",
                                  (self.project_id,)).fetchone()[0]
            stamp = geo_runs.now()
            run_id = db.execute(
                "INSERT INTO geo_runs (tenant_id,project_id,status,engines,samples,total,done,"
                "credits,created_at,finished_at,method_note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (commander.tenant(), self.project_id, status, f'["{engine}"]', 1,
                 len(texts), len(texts), 0, stamp, stamp, geo_runs.METHOD_NOTE),
            ).lastrowid
            for text in texts:
                db.execute(
                    "INSERT INTO geo_answers (tenant_id,project_id,run_id,question_id,engine_key,"
                    "engine_name,attempt,status,text,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (commander.tenant(), self.project_id, run_id, question, engine,
                     geo_runs.ENGINES.get(engine, engine), 0, "ok", text, "", stamp),
                )
        return run_id

    def add_run_multi(self, rows, engine="doubao"):
        with commander.database() as db:
            geo_runs.initialize(db)
            questions = [row[0] for row in db.execute(
                "SELECT id FROM geo_questions WHERE project_id=? ORDER BY id",
                (self.project_id,)).fetchall()]
            stamp = geo_runs.now()
            run_id = db.execute(
                "INSERT INTO geo_runs (tenant_id,project_id,status,engines,samples,total,done,"
                "credits,created_at,finished_at,method_note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (commander.tenant(), self.project_id, "done", f'["{engine}"]', 1,
                 len(rows), len(rows), 0, stamp, stamp, geo_runs.METHOD_NOTE),
            ).lastrowid
            for question_index, text in rows:
                db.execute(
                    "INSERT INTO geo_answers (tenant_id,project_id,run_id,question_id,engine_key,"
                    "engine_name,attempt,status,text,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (commander.tenant(), self.project_id, run_id, questions[question_index],
                     engine, geo_runs.ENGINES.get(engine, engine), 0, "ok", text, "", stamp),
                )
        return run_id

    def add_publication(self, url, title="企业软件怎么选", platform="zhihu",
                        platform_name="知乎", status="published"):
        with commander.database() as db:
            geo_content.initialize(db)
            stamp = geo_runs.now()
            return db.execute(
                "INSERT INTO geo_publications (tenant_id,project_id,content_id,ticket_id,"
                "channel_kind,platform,platform_name,title,status,url,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (commander.tenant(), self.project_id, 1, 0, "community", platform,
                 platform_name, title, status, url, stamp, stamp),
            ).lastrowid


class CitationUnitTests(unittest.TestCase):
    def test_extract_links_scope(self):
        text = ("看 [官网介绍](https://www.yunshan.com/about)，"
                "再看 https://zhihu.com/question/1。"
                "重复 https://zhihu.com/question/1，"
                "忽略 zhihu.com/question/2 和 http://localhost:8080/x。")
        links = geo_sources.extract_links(text)
        urls = [url for url, _title in links]
        self.assertEqual(urls, ["https://yunshan.com/about", "https://zhihu.com/question/1"])
        self.assertEqual(links[0][1], "官网介绍")
        self.assertTrue(all(item[0].startswith("https://") for item in links))

    def test_source_types_and_own_competitor(self):
        own = {"yunshan.com"}
        competitors = {"jiatech.com"}
        rows = geo_sources.citations([
            {"id": 1, "text": "https://yunshan.com/a https://jiatech.com/b", "created_at": ""},
            {"id": 2, "text": "https://zhihu.com/q https://baike.baidu.com/x", "created_at": ""},
            {"id": 3, "text": "https://example.org/page", "created_at": ""},
        ], own, competitors)
        by_domain = {row["domain"]: row for row in rows}
        self.assertEqual(by_domain["yunshan.com"]["source_type"], "official")
        self.assertTrue(by_domain["yunshan.com"]["own"])
        self.assertTrue(by_domain["jiatech.com"]["competitor"])
        self.assertEqual(by_domain["zhihu.com"]["source_type"], "community")
        self.assertEqual(by_domain["baike.baidu.com"]["source_type"], "wiki")
        self.assertEqual(by_domain["example.org"]["source_type"], "other")
        summary = geo_sources.summarize(rows)
        self.assertEqual(summary["total_citations"], 5)
        self.assertEqual(len(summary["types"]), 4)


class SourcesPageTests(SourcesBase):
    def test_sources_page_and_filters(self):
        self.add_run([
            "推荐甲科技，参考 https://www.zhihu.com/question/1 和 [官网](https://www.yunshan.com/about)。",
            "见 https://baike.baidu.com/item/abc 和 https://www.zhihu.com/question/2。",
            "没有引用链接的回答。",
        ])
        data = geo.read(self.path("sources"), {"days": ["30"]})
        self.assertEqual(data["answers"], 3)
        self.assertEqual(data["total"], 3)
        types = {item["source_type"]: item["answers"] for item in data["types"]}
        self.assertEqual(types, {"official": 1, "community": 2, "wiki": 1})
        domains = {item["domain"]: item for item in data["items"]}
        self.assertEqual(domains["zhihu.com"]["answers"], 2)
        self.assertEqual(domains["zhihu.com"]["count"], 2)
        self.assertAlmostEqual(domains["zhihu.com"]["share"], 0.6667, places=3)
        self.assertEqual(len(domains["zhihu.com"]["samples"]), 2)
        filtered = geo.read(self.path("sources"), {"days": ["30"], "source_type": ["wiki"]})
        self.assertEqual([item["domain"] for item in filtered["items"]], ["baike.baidu.com"])
        paged = geo.read(self.path("sources"), {"days": ["30"], "page": ["2"], "page_size": ["2"]})
        self.assertEqual(paged["total"], 3)
        self.assertEqual(len(paged["items"]), 1)

        empty = geo.read(self.path("sources"), {"days": ["7"]})
        self.assertEqual(empty["answers"], 3)

    def test_sources_require_a_real_project(self):
        with self.assertRaises(commander.ChatError):
            geo.read("/api/geo/projects/9999/sources", {})


class ReportCitationTests(SourcesBase):
    def test_report_sources_and_cited_publications(self):
        cited_url = "https://zhuanlan.zhihu.com/p/999"
        new_url = "https://zhuanlan.zhihu.com/p/1000"
        self.add_publication(cited_url)
        self.add_publication(new_url, title="还没被引用的新文章")
        self.add_run([
            f"推荐看这篇 {cited_url} 和 [百科](https://baike.baidu.com/item/a)。",
            "没有链接。",
        ])
        report = geo.read(self.path("reports"), {"period": ["week"]})["report"]
        domains = {item["domain"]: item["answers"] for item in report["sources"]["domains"]}
        self.assertEqual(domains["zhuanlan.zhihu.com"], 1)
        self.assertEqual(domains["baike.baidu.com"], 1)
        self.assertEqual(report["cited"]["pubs"], 1)
        self.assertEqual(report["cited"]["answers"], 1)
        self.assertEqual(report["cited"]["questions"], 1)
        self.assertEqual(report["cited"]["new_pubs"], 1)
        cited = report["cited"]["items"]
        by_title = {item["title"]: item for item in cited}
        self.assertEqual(by_title["企业软件怎么选"]["answers"], 1)
        self.assertEqual(by_title["企业软件怎么选"]["platform_name"], "知乎")
        self.assertTrue(by_title["企业软件怎么选"]["new"])
        self.assertEqual(by_title["还没被引用的新文章"]["answers"], 0)

    def test_publication_cited_count_and_overview(self):
        self.add_publication("https://zhuanlan.zhihu.com/p/999")
        self.add_run(["见 https://zhuanlan.zhihu.com/p/999 。"])
        publications = geo.read(self.path("publications"), {})["items"]
        self.assertEqual(publications[0]["cited_count"], 1)
        overview = geo.read(self.path("overview"), {})
        self.assertEqual(overview["counts"]["cited_pubs"], 1)
        self.assertEqual(overview["cited"][0]["cited_count"], 1)
        self.assertIn("20", overview["cited"][0]["last_cited_at"])

    def test_hub_page_cited_count(self):
        content = geo.write("POST", self.path("contents"), {
            "question": "企业软件哪家好？", "genre": "qa_answer",
        })["content"]
        geo.write("PUT", f"/api/geo/contents/{content['id']}",
                  {"title": "企业软件怎么选", "body": PASS_BODY})
        checked = geo.write("POST", f"/api/geo/contents/{content['id']}/check", {})["content"]
        if checked["gate"]["decision"] not in {"pass", "approved"}:
            geo.write("POST", f"/api/geo/contents/{content['id']}/approve", {"reason": "验收放行"})
        page = geo.write("POST", self.path("hub/pages"), {
            "content_id": content["id"], "section": "answers",
        })["page"]
        geo.write("PUT", self.path("hub/domain"), {"domain": "help.yunshan.com"})
        with commander.database() as db:
            db.execute("UPDATE geo_hubs SET domain_status='active'")
            project = db.execute("SELECT * FROM geo_projects WHERE id=?",
                                 (self.project_id,)).fetchone()
            hub = db.execute("SELECT * FROM geo_hubs WHERE project_id=?",
                             (self.project_id,)).fetchone()
            geo_hub._refresh_hub_urls(db, project, hub)
        self.add_run([f"官方说明见 https://help.yunshan.com/p/{page['id']} 。"])
        pages = geo.read(self.path("hub/pages"), {})["items"]
        target = next(item for item in pages if item["id"] == page["id"])
        self.assertEqual(target["cited_count"], 1)
        self.assertEqual(target["url"], f"https://help.yunshan.com/p/{page['id']}")


class DiagnosisCitationTests(SourcesBase):
    def test_diagnosis_sources_and_engine_rows(self):
        run_id = self.add_run([
            "推荐甲科技，见 [知乎](https://zhihu.com/q/1) 和 https://yunshan.com/about。",
            "参考 https://baike.baidu.com/item/a。",
        ])
        with commander.database() as db:
            data = geo_report.diagnosis_data(db, self.project_id, run_id)
        self.assertEqual(data["scope"]["citations"], 3)
        self.assertEqual(data["scope"]["unique_domains"], 3)
        self.assertEqual(data["scope"]["avg_citations"], 1.5)
        self.assertEqual(data["sources"]["total"], 3)
        self.assertEqual(data["sources"]["own"], 1)
        types = {item["source_type"]: item["count"] for item in data["sources"]["types"]}
        self.assertEqual(types, {"official": 1, "community": 1, "wiki": 1})
        engine = data["engines"][0]
        self.assertEqual(engine["citations"], 3)
        self.assertTrue(engine["domains"])
        self.assertTrue(engine["focus"]["main"])
        self.assertIn("http(s)", data["boundaries"][2])

    def test_gaps_detail_lists_competitor_owned_questions(self):
        self.add_run_multi([
            (0, "推荐甲科技，价格更透明。"),
            (1, "推荐云杉科技，适合远程办公。"),
        ])
        data = geo.read(self.path("gaps"), {})
        self.assertTrue(data["run"])
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertIn("企业软件哪家好", item["question"])
        self.assertEqual(item["competitors"], [{"name": "甲科技", "answers": 1}])
        self.assertEqual(item["engines"], ["豆包（模拟）"])

    def test_cited_pages_analyze_is_explicitly_closed(self):
        with self.assertRaises(commander.ChatError) as caught:
            geo.write("POST", self.path("cited-pages/analyze"),
                      {"urls": ["https://zhihu.com/q/1"], "days": 30})
        self.assertIn("暂未开放", str(caught.exception))


class IsolationTests(SourcesBase):
    def test_citations_are_tenant_scoped(self):
        self.add_run(["见 https://zhihu.com/q/1 。"])
        other = tenancy.create_tenant("另一个客户")
        tenancy.activate({"tenant_id": other, "user_id": "u", "username": "u", "role": "member"})
        with self.assertRaises(commander.ChatError):
            geo.read(self.path("sources"), {})
        with self.assertRaises(commander.ChatError):
            geo.read(self.path("gaps"), {})
