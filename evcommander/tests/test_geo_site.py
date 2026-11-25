"""GEO website audit and AI-readiness generators."""

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
import geo_content
import geo_site
import tenancy


PROJECT = {
    "brand_name": "云杉科技", "aliases": ["云杉"], "website": "https://yunshan.example",
    "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
    "market": "cn", "locales": [],
}
GOOD_HTML = """<!doctype html><html lang="zh-CN"><head>
<title>云杉科技 - 企业协作软件</title>
<meta name="description" content="云杉科技成立于 2016 年，总部深圳，主营企业协作软件。">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="canonical" href="https://yunshan.example/">
<script type="application/ld+json">{"@context":"https://schema.org"}</script>
</head><body><h1>云杉科技</h1><h2>产品能力</h2><h2>客户案例</h2>
<ul><li>远程协作</li><li>本地部署</li></ul>
<p>""" + ("云杉科技帮助企业把沟通、文档和任务放在一套工具里，支持远程办公和本地部署。" * 12) + """</p>
</body></html>"""
BAD_HTML = """<html><head><title>短</title>
<meta name="robots" content="noindex"></head>
<body><script>document.body.innerHTML = "全部内容由脚本渲染";</script><div id="root"></div></body></html>"""
GOOD_ROBOTS = """User-agent: *
Allow: /
Sitemap: https://yunshan.example/sitemap.xml

User-agent: GPTBot
Disallow: /
"""
BAD_ROBOTS = """User-agent: *
Disallow: /

User-agent: Googlebot
Disallow: /
"""
GOOD_LLMS = "# 云杉科技\n\n> 企业协作软件。\n\n## 页面\n- [官网](https://yunshan.example/)\n"
SITEMAP = ('<?xml version="1.0"?><urlset>'
           '<url><loc>https://yunshan.example/a</loc></url>'
           '<url><loc>https://yunshan.example/b</loc></url></urlset>')
ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN",
            "EV_SITE_AUDIT_ALLOW_PRIVATE", "EV_SITE_AUDIT_DAILY_LIMIT")


def fake_fetch(kind):
    def fetch(url, base_host, timeout=10):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "/" in url.split("://", 1)[1] else "/"
        if path.endswith("/robots.txt"):
            return geo_site._Fetched(200, url, BAD_ROBOTS if kind == "bad" else GOOD_ROBOTS)
        if path.endswith("/llms.txt"):
            return geo_site._Fetched(404 if kind == "bad" else 200, url,
                                     "" if kind == "bad" else GOOD_LLMS)
        if path.endswith("/sitemap.xml"):
            return geo_site._Fetched(404 if kind == "bad" else 200, url,
                                     "" if kind == "bad" else SITEMAP)
        return geo_site._Fetched(200, url, BAD_HTML if kind == "bad" else GOOD_HTML)
    return fetch


class SiteBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["EV_PAY_MOCK"] = "0"
        os.environ["EV_SITE_AUDIT_ALLOW_PRIVATE"] = "1"
        for key in ("EV_SEED_DEMO", "EV_TRIAL_ORIGIN", "EV_SITE_AUDIT_DAILY_LIMIT"):
            os.environ.pop(key, None)
        tenancy.activate(tenancy.local_identity())
        self.project_id = geo.write("POST", geo.PROJECTS, PROJECT)["project"]["id"]
        self.path = lambda suffix="": (f"/api/geo/projects/{self.project_id}/{suffix}".rstrip("/")
                                       if suffix else f"/api/geo/projects/{self.project_id}")
        geo.write("POST", self.path("facts"), {
            "fact_key": "founded", "label": "成立时间", "value": "2016 年成立于深圳",
        })
        geo.write("POST", self.path("facts"), {
            "fact_key": "phone", "label": "联系电话", "value": "0755-12345678",
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

    def audit(self, kind="good", url=None):
        body = {} if url is None else {"url": url}
        with mock.patch.object(geo_site, "_fetch", side_effect=fake_fetch(kind)):
            return geo.write("POST", self.path("site-audits"), body)


class SiteAuditTests(SiteBase):
    def test_good_site_audit_list_detail_and_reuse(self):
        result = self.audit()
        audit = result["audit"]
        self.assertFalse(result["reused"])
        self.assertEqual(audit["status"], "done")
        self.assertEqual(audit["status_name"], "已完成")
        self.assertGreaterEqual(audit["score"], 80)
        self.assertEqual(audit["page"]["title"], "云杉科技 - 企业协作软件")
        self.assertGreaterEqual(audit["page"]["chars"], 300)
        self.assertEqual(audit["summary"]["pass"] + audit["summary"]["warn"]
                         + audit["summary"]["info"] + audit["summary"]["fail"], 14)
        checks = {item["key"]: item for item in audit["checks"]}
        self.assertEqual(checks["https"]["status"], "pass")
        self.assertEqual(checks["robots_ai"]["status"], "pass")
        self.assertEqual(checks["llms_txt"]["status"], "pass")
        self.assertEqual(checks["sitemap"]["status"], "pass")
        self.assertEqual(checks["meta_robots"]["status"], "pass")
        bots = {item["agent"]: item for item in audit["bots"]}
        self.assertTrue(bots["Googlebot"]["allowed"])
        self.assertFalse(bots["GPTBot"]["allowed"])
        self.assertEqual(bots["GPTBot"]["purpose"], "training")

        listing = geo.read(self.path("site-audits"), {"page": ["1"], "page_size": ["8"]})
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["id"], audit["id"])
        detail = geo.read(f"/api/geo/site-audits/{audit['id']}", {})
        self.assertEqual(detail["audit"]["id"], audit["id"])
        replay = self.audit()
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["audit"]["id"], audit["id"])

    def test_bad_site_scores_low_and_flags_blockers(self):
        audit = self.audit("bad", url="http://yunshan.example/bad")["audit"]
        self.assertEqual(audit["status"], "done")
        self.assertLess(audit["score"], 60)
        checks = {item["key"]: item for item in audit["checks"]}
        self.assertEqual(checks["robots_ai"]["status"], "fail")
        self.assertEqual(checks["meta_robots"]["status"], "fail")
        self.assertEqual(checks["https"]["status"], "fail")
        self.assertEqual(checks["content"]["status"], "fail")
        self.assertEqual(checks["llms_txt"]["status"], "info")
        self.assertEqual(checks["sitemap"]["status"], "warn")
        self.assertGreaterEqual(audit["summary"]["fail"], 3)
        bots = {item["agent"]: item for item in audit["bots"]}
        self.assertFalse(bots["Googlebot"]["allowed"])

    def test_target_must_stay_on_project_website(self):
        with self.assertRaises(commander.ChatError):
            self.audit(url="https://other.example/page")
        with mock.patch.object(geo_site, "_private", return_value=True):
            with self.assertRaises(commander.ChatError):
                self.audit()

    def test_fetch_failure_becomes_failed_audit(self):
        with mock.patch.object(geo_site, "_fetch",
                               side_effect=commander.ChatError("抓取官网失败，请检查网址或稍后重试。")):
            audit = geo.write("POST", self.path("site-audits"), {})["audit"]
        self.assertEqual(audit["status"], "failed")
        self.assertIn("抓取官网失败", audit["error"])

    def test_daily_limit_and_stale_recovery(self):
        os.environ["EV_SITE_AUDIT_DAILY_LIMIT"] = "1"
        self.audit()
        with self.assertRaises(commander.ChatError):
            self.audit(url="https://yunshan.example/another")
        os.environ.pop("EV_SITE_AUDIT_DAILY_LIMIT", None)
        with commander.database() as db:
            geo_site.initialize(db)
            stamp = "2026-01-01T00:00:00+00:00"
            db.execute(
                "INSERT INTO geo_site_audits (tenant_id,project_id,url,status,created_at) "
                "VALUES (?,?,?,?,?)",
                (commander.tenant(), self.project_id, "https://yunshan.example/stale",
                 "running", stamp),
            )
        listing = geo.read(self.path("site-audits"), {})
        stale = next(item for item in listing["items"]
                     if item["url"].endswith("/stale"))
        self.assertEqual(stale["status"], "failed")

    def test_cross_tenant_isolation(self):
        self.audit()
        other = tenancy.create_tenant("另一个客户")
        tenancy.activate({"tenant_id": other, "user_id": "u", "username": "u", "role": "member"})
        with self.assertRaises(commander.ChatError):
            geo.read(self.path("site-audits"), {})


class SiteGeneratorTests(SiteBase):
    def generate(self, kind, body=None):
        return geo.write("POST", self.path(f"generators/{kind}"), body or {})["result"]

    def test_robots_generator(self):
        result = self.generate("robots", {"policy": {"training": "deny"}, "with_sitemap": True})
        self.assertEqual(result["kind"], "robots")
        self.assertIsInstance(result["missing"], list)
        text = result["text"]
        self.assertIn("User-agent: Googlebot", text)
        self.assertIn("User-agent: GPTBot", text)
        self.assertIn("Disallow: /", text)
        self.assertIn("Sitemap: https://yunshan.example/sitemap.xml", text)
        allowed = self.generate("robots", {"policy": {"training": "allow"}})
        self.assertNotIn("Disallow: /", allowed["text"])

    def test_llms_generator(self):
        result = self.generate("llms")
        self.assertEqual(result["file"], "llms.txt")
        self.assertIn("# 云杉科技", result["text"])
        self.assertIn("成立时间", result["text"])
        self.assertIn("https://yunshan.example", result["text"])

    def test_schema_and_entity_generators(self):
        organization = self.generate("schema", {"type": "organization"})
        self.assertIn('"@type": "Organization"', organization["text"])
        self.assertIn("云杉科技", organization["text"])
        faq = self.generate("schema", {"type": "faq"})
        self.assertIn('"@type": "FAQPage"', faq["text"])
        self.assertIn("成立时间是什么？", faq["text"])
        with self.assertRaises(commander.ChatError):
            self.generate("schema", {"type": "article"})
        directories = self.generate("entity", {"type": "directories"})
        self.assertIn("名称（一字不差）：云杉科技", directories["text"])
        self.assertIn("0755-12345678", directories["text"])
        self.assertEqual(len(directories["items"]), 6)
        self.assertTrue(all(item["status"] == "todo" for item in directories["items"]))
        wikidata = self.generate("entity", {"type": "wikidata"})
        self.assertIn('LAST|P856|"https://yunshan.example"', wikidata["text"])
        with self.assertRaises(commander.ChatError):
            self.generate("unknown")

    def test_schema_article_from_content(self):
        content = geo.write("POST", self.path("contents"), {
            "question": "企业软件哪家好？", "genre": "qa_answer",
        })["content"]
        geo.write("PUT", f"/api/geo/contents/{content['id']}", {
            "title": "企业软件怎么选",
            "body": "# 企业软件怎么选\n\n云杉科技成立于 2016 年，主营企业协作软件。\n",
        })
        article = self.generate("schema", {"type": "article", "content_id": content["id"]})
        self.assertIn('"@type": "Article"', article["text"])
        self.assertIn("企业软件怎么选", article["text"])

    def test_generators_are_free_and_tenant_scoped(self):
        before = geo.read("/api/geo/projects", {})["total"]
        self.generate("llms")
        after = geo.read("/api/geo/projects", {})["total"]
        self.assertEqual(before, after)
        other = tenancy.create_tenant("另一个客户")
        tenancy.activate({"tenant_id": other, "user_id": "u", "username": "u", "role": "member"})
        with self.assertRaises(commander.ChatError):
            self.generate("llms")
