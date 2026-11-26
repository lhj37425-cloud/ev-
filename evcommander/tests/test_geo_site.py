"""GEO website audit and AI-readiness generators."""

from datetime import datetime, timedelta, timezone
import gzip
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


def _log_stamp(day, hour="08:00:00"):
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return "%02d/%s/%d:%s +0800" % (day.day, months[day.month - 1], day.year, hour)


class CrawlLogTests(SiteBase):
    def setUp(self):
        super().setUp()
        self.today = datetime.now(timezone.utc).date()
        self.yesterday = self.today - timedelta(days=1)

    def upload(self, content, filename="access.log"):
        boundary = "TestBoundary"
        body = (
            "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
            "Content-Type: text/plain\r\n\r\n" % (boundary, filename)
        ).encode() + content + ("\r\n--%s--\r\n" % boundary).encode()
        return geo_site.upload_crawl_log(
            self.project_id, "multipart/form-data; boundary=" + boundary, body)

    def nginx_log(self):
        return "\n".join([
            '203.0.113.10 - - [%s] "GET /pricing HTTP/1.1" 200 512 "-" '
            '"Mozilla/5.0 (compatible; GPTBot/1.2; +https://openai.com/gptbot)"'
            % _log_stamp(self.today),
            '203.0.113.11 - - [%s] "GET /missing HTTP/1.1" 404 128 "-" '
            '"Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"'
            % _log_stamp(self.today, "08:01:00"),
            '203.0.113.12 - - [%s] "GET /blog HTTP/1.1" 200 300 "-" '
            '"Mozilla/5.0 (compatible; PerplexityBot/1.0)"' % _log_stamp(self.yesterday),
            '203.0.113.13 - - [%s] "GET / HTTP/1.1" 200 300 "-" '
            '"Mozilla/5.0 (Windows NT 10.0) Chrome/120"' % _log_stamp(self.yesterday),
        ]) + "\n"

    def test_upload_nginx_log_and_stats(self):
        result = self.upload(self.nginx_log().encode())
        self.assertTrue(result["ok"])
        self.assertIn("3 次", result["message"])
        uploads = geo.read(self.path("crawl-logs"), {})
        self.assertEqual(uploads["total"], 1)
        item = uploads["items"][0]
        self.assertEqual(item["filename"], "access.log")
        self.assertEqual(item["lines"], 4)
        self.assertEqual(item["parsed"], 3)
        self.assertEqual(item["range_start"], self.yesterday.isoformat())
        self.assertEqual(item["range_end"], self.today.isoformat())
        stats = geo.read(self.path("crawl-stats"), {"days": ["30"]})
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["uploads"], 1)
        bots = {bot["bot"]: bot for bot in stats["bots"]}
        self.assertEqual(set(bots), {"GPTBot", "Googlebot", "PerplexityBot"})
        self.assertEqual(bots["Googlebot"]["purpose"], "search")
        self.assertEqual(bots["GPTBot"]["purpose"], "training")
        purposes = {item["key"]: item["hits"] for item in stats["purposes"]}
        self.assertEqual(purposes, {"search": 1, "user": 0, "engine": 1, "training": 1})
        self.assertEqual(len(stats["error_paths"]), 1)
        self.assertEqual(stats["error_paths"][0]["path"], "/missing")
        self.assertEqual(stats["error_paths"][0]["status"], 404)
        self.assertEqual(stats["error_paths"][0]["bots"], ["Googlebot"])
        series = {item["date"]: item for item in stats["series"]}
        self.assertEqual(series[self.today.isoformat()]["search"], 1)
        self.assertEqual(series[self.today.isoformat()]["training"], 1)
        self.assertEqual(series[self.yesterday.isoformat()]["engine"], 1)

    def test_json_lines_and_gzip(self):
        lines = "\n".join([
            json.dumps({"time": self.today.isoformat() + "T08:00:00+08:00",
                        "path": "/json", "status": 403,
                        "http_user_agent": "ChatGPT-User/1.0"}),
            json.dumps({"time": self.today.isoformat() + "T08:01:00+08:00",
                        "request": "GET /j2 HTTP/1.1", "status": 200,
                        "user_agent": "ClaudeBot/1.0"}),
        ]) + "\n"
        result = self.upload(gzip.compress(lines.encode()), "access.log.gz")
        self.assertIn("2 次", result["message"])
        stats = geo.read(self.path("crawl-stats"), {})
        self.assertEqual(stats["total"], 2)
        bots = {bot["bot"]: bot for bot in stats["bots"]}
        self.assertEqual(bots["ChatGPT-User"]["purpose"], "user")
        self.assertEqual(bots["ClaudeBot"]["purpose"], "training")

    def test_duplicate_upload_does_not_recount(self):
        raw = self.nginx_log().encode()
        self.upload(raw)
        again = self.upload(raw, "copy.log")
        self.assertTrue(again["duplicate"])
        self.assertIn("已经传过", again["message"])
        self.assertEqual(geo.read(self.path("crawl-logs"), {})["total"], 1)
        self.assertEqual(geo.read(self.path("crawl-stats"), {})["total"], 3)

    def test_log_without_ai_crawler(self):
        line = ('203.0.113.13 - - [%s] "GET / HTTP/1.1" 200 300 "-" '
                '"Mozilla/5.0 (Windows NT 10.0) Chrome/120"') % _log_stamp(self.today)
        result = self.upload((line + "\n").encode())
        self.assertIn("没识别出", result["message"])
        stats = geo.read(self.path("crawl-stats"), {})
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["uploads"], 1)

    def test_days_window_and_delete(self):
        recent = ('203.0.113.10 - - [%s] "GET /a HTTP/1.1" 200 1 "-" "GPTBot/1.2"'
                  % _log_stamp(self.today))
        old_day = self.today - timedelta(days=20)
        old = ('203.0.113.10 - - [%s] "GET /b HTTP/1.1" 200 1 "-" "GPTBot/1.2"'
               % _log_stamp(old_day))
        self.upload((recent + "\n").encode(), "recent.log")
        self.upload((old + "\n").encode(), "old.log")
        self.assertEqual(geo.read(self.path("crawl-stats"), {"days": ["7"]})["total"], 1)
        self.assertEqual(geo.read(self.path("crawl-stats"), {"days": ["30"]})["total"], 2)
        logs = geo.read(self.path("crawl-logs"), {})
        self.assertEqual(logs["total"], 2)
        geo.write("DELETE", f"/api/geo/crawl-logs/{logs['items'][0]['id']}", {})
        self.assertEqual(geo.read(self.path("crawl-logs"), {})["total"], 1)
        self.assertEqual(geo.read(self.path("crawl-stats"), {"days": ["30"]})["total"], 1)

    def test_invalid_uploads(self):
        with self.assertRaises(commander.ChatError):
            geo_site.upload_crawl_log(self.project_id, "application/json", b"{}")
        boundary = "B"
        empty = ("--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.log\"\r\n"
                 "\r\n\r\n--B--\r\n").encode()
        with self.assertRaises(commander.ChatError):
            geo_site.upload_crawl_log(self.project_id, "multipart/form-data; boundary=B", empty)
        with mock.patch.object(geo_site, "MAX_LOG_BYTES", 10):
            with self.assertRaises(commander.ChatError):
                self.upload(b"x" * 50)

    def test_tenant_scope(self):
        self.upload(self.nginx_log().encode())
        other = tenancy.create_tenant("另一个客户")
        tenancy.activate({"tenant_id": other, "user_id": "u", "username": "u", "role": "member"})
        with self.assertRaises(commander.ChatError):
            geo.read(self.path("crawl-stats"), {})


class BeaconTests(SiteBase):
    def beacon(self, origin="http://127.0.0.1:8766"):
        return geo.read(self.path("beacon"), {"origin": [origin]})

    def token(self):
        import re
        snippet = self.beacon()["site"]["snippet"]
        return re.search(r"k=([A-Za-z0-9_-]+)", snippet).group(1)

    def hit(self, token, source="chatgpt", page="/pricing"):
        return geo_site.beacon_response(
            "POST", "/geo-beacon/hit",
            json.dumps({"k": token, "s": source, "p": page}).encode())

    def lead(self, token, phone="13800000001", source="perplexity"):
        return geo_site.beacon_response(
            "POST", "/geo-beacon/lead",
            json.dumps({"k": token, "s": source, "p": "/contact",
                        "phone": phone}).encode())

    def test_beacon_site_and_script(self):
        site = self.beacon()["site"]
        self.assertEqual(site["project_id"], self.project_id)
        self.assertFalse(site["installed"])
        self.assertTrue(site["leads_enabled"])
        self.assertIn("http://127.0.0.1:8766/geo-beacon.js?k=", site["snippet"])
        data = self.beacon()
        self.assertEqual(len(data["sources"]), 15)
        status, script, content_type, headers = geo_site.beacon_response(
            "GET", "/geo-beacon.js", b"")
        self.assertEqual(status, 200)
        self.assertIn("javascript", content_type)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        text = script.decode("utf-8")
        self.assertIn("send('/geo-beacon/hit'", text)
        self.assertIn("chatgpt.com", text)
        self.assertIn("document.addEventListener('submit'", text)

    def test_beacon_hit_and_stats(self):
        token = self.token()
        status, _, _, _ = self.hit(token, "chatgpt", "/pricing")
        self.assertEqual(status, 204)
        self.hit(token, "other", "/about")
        self.hit(token, "direct", "/")
        stats = geo.read(self.path("beacon/stats"), {"days": ["30"]})
        self.assertEqual(stats["total_visits"], 3)
        self.assertEqual(stats["ai_visits"], 1)
        self.assertEqual(stats["ai_leads"], 0)
        self.assertEqual(stats["sources"],
                         [{"key": "chatgpt", "name": "ChatGPT", "visits": 1, "leads": 0}])
        self.assertEqual(stats["pages"], [{"path": "/pricing", "visits": 1}])
        today = stats["series"][-1]
        self.assertEqual(today["ai"], 1)
        site = self.beacon()["site"]
        self.assertTrue(site["installed"])
        self.assertTrue(site["last_hit_at"])

    def test_beacon_lead_writes_customer_once(self):
        token = self.token()
        self.hit(token, "perplexity", "/contact")
        status, _, _, _ = self.lead(token)
        self.assertEqual(status, 204)
        with commander.database() as db:
            rows = db.execute("SELECT * FROM crm_customers WHERE tenant_id=?",
                              (commander.tenant(),)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("13800000001", rows[0]["details"])
        stats = geo.read(self.path("beacon/stats"), {"days": ["30"]})
        self.assertEqual(stats["ai_leads"], 1)
        self.assertEqual(stats["sources"][0]["leads"], 1)
        self.lead(token, "13800000001")
        stats = geo.read(self.path("beacon/stats"), {"days": ["30"]})
        self.assertEqual(stats["ai_leads"], 1)

    def test_beacon_lead_disabled_and_invalid(self):
        token = self.token()
        import customers
        geo.write("PUT", self.path("beacon"), {"leads_enabled": False})
        self.lead(token)
        with commander.database() as db:
            customers.initialize(db)
            count = db.execute("SELECT COUNT(*) FROM crm_customers WHERE tenant_id=?",
                               (commander.tenant(),)).fetchone()[0]
        self.assertEqual(count, 0)
        geo.write("PUT", self.path("beacon"), {"leads_enabled": True})
        self.lead(token, "abc")
        with commander.database() as db:
            customers.initialize(db)
            count = db.execute("SELECT COUNT(*) FROM crm_customers WHERE tenant_id=?",
                               (commander.tenant(),)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_beacon_reset_code(self):
        before = self.beacon()["site"]["snippet"]
        old = self.token()
        updated = geo.write("PUT", self.path("beacon"),
                            {"reset_code": True, "origin": "http://127.0.0.1:8766"})
        self.assertNotEqual(updated["site"]["snippet"], before)
        new = self.token()
        self.assertNotEqual(old, new)
        self.hit(old)
        stats = geo.read(self.path("beacon/stats"), {})
        self.assertEqual(stats["total_visits"], 0)
        self.hit(new)
        stats = geo.read(self.path("beacon/stats"), {})
        self.assertEqual(stats["total_visits"], 1)

    def test_beacon_invalid_payloads(self):
        status, _, _, _ = geo_site.beacon_response("POST", "/geo-beacon/hit", b"not-json")
        self.assertEqual(status, 400)
        status, _, _, _ = geo_site.beacon_response("POST", "/geo-beacon/hit", b"{}")
        self.assertEqual(status, 204)
        status, _, _, _ = geo_site.beacon_response(
            "POST", "/geo-beacon/hit", b"x" * (geo_site.MAX_BEACON_BODY + 1))
        self.assertEqual(status, 413)
        token = self.token()
        self.hit(token, "evil-source", "/x")
        stats = geo.read(self.path("beacon/stats"), {})
        self.assertEqual(stats["ai_visits"], 0)
        self.assertEqual(stats["total_visits"], 1)

    def test_beacon_is_tenant_scoped(self):
        self.beacon()
        other = tenancy.create_tenant("另一个客户")
        tenancy.activate({"tenant_id": other, "user_id": "u", "username": "u", "role": "member"})
        with self.assertRaises(commander.ChatError):
            geo.read(self.path("beacon"), {})


class ImportTests(SiteBase):
    GSC_PAGES = ('Top pages,Clicks,Impressions,CTR,Position\n'
                 'https://yunshan.example/pricing,120,"2,000",6%,3.5\n'
                 'https://yunshan.example/blog,80,1000,8%,12\n')

    def import_file(self, content, filename="report.csv", source="",
                    start="", end=""):
        boundary = "ImportBoundary"
        parts = [
            ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
             'Content-Type: text/csv\r\n\r\n' % (boundary, filename)).encode() + content + b"\r\n",
        ]
        for name, value in (("source", source), ("period_start", start),
                            ("period_end", end)):
            if value:
                parts.append(
                    ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                     % (boundary, name, value)).encode())
        parts.append(("--%s--\r\n" % boundary).encode())
        return geo_site.upload_import(
            self.project_id, "multipart/form-data; boundary=" + boundary, b"".join(parts))

    def test_gsc_pages_import_and_detail(self):
        result = self.import_file(self.GSC_PAGES.encode())
        self.assertTrue(result["ok"])
        self.assertIn("1 份报表", result["message"])
        listing = geo.read(self.path("imports"), {})
        self.assertEqual(listing["total"], 1)
        item = listing["items"][0]
        self.assertEqual(item["source"], "gsc")
        self.assertEqual(item["source_name"], "Google Search Console")
        self.assertEqual(item["kind"], "pages")
        self.assertEqual(item["kind_name"], "按页面")
        self.assertEqual(item["rows"], 2)
        self.assertEqual(item["main"], "clicks")
        self.assertEqual(item["totals"]["clicks"], 200)
        self.assertEqual(item["totals"]["impressions"], 3000)
        self.assertAlmostEqual(item["totals"]["ctr"], 0.066667, places=4)
        detail = geo.read(f"/api/geo/imports/{item['id']}", {})["import"]
        self.assertEqual(len(detail["items"]), 2)
        self.assertEqual(detail["items"][0]["key"], "https://yunshan.example/pricing")
        self.assertEqual(detail["items"][0]["impressions"], 2000)
        self.assertAlmostEqual(detail["items"][0]["ctr"], 0.06)
        self.assertEqual(detail["metric_names"]["ctr"], "点击率")

    def test_daily_and_bing_ai_sources(self):
        daily = ('Date,Clicks,Impressions,CTR,Position\n'
                 '2026-11-24,10,100,10%,2\n'
                 '2026-11-25,20,200,10%,3\n')
        self.import_file(daily.encode(), "daily.csv")
        item = geo.read(self.path("imports"), {})["items"][0]
        self.assertEqual(item["kind"], "daily")
        self.assertEqual(item["period_start"], "2026-11-24")
        self.assertEqual(item["period_end"], "2026-11-25")
        ai = ('Date,Page,Citations\n'
              '2026-11-25,https://yunshan.example/pricing,5\n')
        self.import_file(ai.encode(), "ai.csv")
        item = geo.read(self.path("imports"), {})["items"][0]
        self.assertEqual(item["source"], "bing_ai")
        self.assertEqual(item["main"], "citations")
        self.assertEqual(item["totals"]["citations"], 5)

    def test_bing_queries_and_chinese_columns(self):
        queries = ('Query,Clicks,Impressions,CTR,Average Position\n'
                   '企业协作软件,15,300,5%,4.2\n')
        self.import_file(queries.encode(), "queries.csv")
        item = geo.read(self.path("imports"), {})["items"][0]
        self.assertEqual(item["source"], "bing")
        self.assertEqual(item["kind"], "queries")
        detail = geo.read(f"/api/geo/imports/{item['id']}", {})["import"]
        self.assertEqual(detail["items"][0]["key"], "企业协作软件")
        chinese = '日期,点击次数,展示次数,点击率,平均排名\n2026-11-25,3,30,10%,1.5\n'
        self.import_file(chinese.encode(), "zh.csv")
        item = geo.read(self.path("imports"), {})["items"][0]
        self.assertEqual(item["kind"], "daily")
        self.assertEqual(item["main"], "clicks")
        self.assertEqual(item["totals"]["clicks"], 3)

    def test_zip_multi_files_and_manual_period(self):
        import zipfile
        buffer = __import__("io").BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Pages.csv", self.GSC_PAGES)
            archive.writestr("Queries.csv", "Query,Clicks\n企业协作软件,15\n")
        result = self.import_file(buffer.getvalue(), "export.zip")
        self.assertIn("2 份报表", result["message"])
        self.assertEqual(geo.read(self.path("imports"), {})["total"], 2)
        result = self.import_file(self.GSC_PAGES.encode(), "pages.csv",
                                  start="2026-11-01", end="2026-11-30")
        listing = geo.read(self.path("imports"), {})
        self.assertEqual(listing["total"], 3)
        latest = listing["items"][0]
        self.assertEqual(latest["period_start"], "2026-11-01")
        self.assertEqual(latest["period_end"], "2026-11-30")

    def test_pages_match_published_content(self):
        with commander.database() as db:
            geo_content.initialize(db)
            stamp = "2026-11-25T00:00:00+00:00"
            db.execute(
                "INSERT INTO geo_publications (tenant_id,project_id,content_id,ticket_id,"
                "channel_kind,platform,platform_name,title,status,url,created_at,updated_at) "
                "VALUES (?,?,1,0,'channel','wordpress','官网','测试','published',?,?,?)",
                (commander.tenant(), self.project_id,
                 "https://yunshan.example/pricing", stamp, stamp))
        self.import_file(self.GSC_PAGES.encode())
        detail = geo.read(f"/api/geo/imports/1", {})["import"]
        ours = {item["key"]: item["ours"] for item in detail["items"]}
        self.assertTrue(ours["https://yunshan.example/pricing"])
        self.assertFalse(ours["https://yunshan.example/blog"])

    def test_source_filter_delete_and_invalid(self):
        self.import_file(self.GSC_PAGES.encode())
        self.import_file(self.GSC_PAGES.encode(), "report2.csv", source="bing")
        self.assertEqual(geo.read(self.path("imports"), {"source": ["bing"]})["total"], 1)
        geo.write("DELETE", "/api/geo/imports/1", {})
        self.assertEqual(geo.read(self.path("imports"), {})["total"], 1)
        with self.assertRaises(commander.ChatError):
            geo.write("DELETE", "/api/geo/imports/999", {})
        with self.assertRaises(commander.ChatError):
            self.import_file(b"foo,bar\n1,2\n")
        with self.assertRaises(commander.ChatError):
            self.import_file(b"", "empty.csv")

    def test_imports_are_tenant_scoped(self):
        self.import_file(self.GSC_PAGES.encode())
        other = tenancy.create_tenant("另一个客户")
        tenancy.activate({"tenant_id": other, "user_id": "u", "username": "u", "role": "member"})
        with self.assertRaises(commander.ChatError):
            geo.read(self.path("imports"), {})
        with self.assertRaises(commander.ChatError):
            geo.read("/api/geo/imports/1", {})


class ProductTests(SiteBase):
    def setUp(self):
        super().setUp()
        PROJECT_GLOBAL = {**PROJECT, "market": "global"}
        # SiteBase already created a cn project; add a global one for products.
        self.global_project = geo.write("POST", geo.PROJECTS, PROJECT_GLOBAL)["project"]
        self.gid = self.global_project["id"]
        self.gpath = lambda suffix="": (
            f"/api/geo/projects/{self.gid}/{suffix}".rstrip("/") if suffix
            else f"/api/geo/projects/{self.gid}")

    def product(self, **extra):
        body = {"sku": "SKU-1", "title": "云杉协作标准版", "price": 99.0,
                "currency": "USD", "availability": "in_stock",
                "url": "https://yunshan.example/p/1",
                "image_url": "https://yunshan.example/p/1.png",
                "brand": "云杉", "gtin": "1234567890123", "mpn": "YS-1",
                "category": "协作软件", "condition": "new",
                "description": "适合 50 人以内团队的协作软件。"}
        body.update(extra)
        return geo.write("POST", self.gpath("products"), body)["product"]

    def test_products_crud_and_list(self):
        listing = geo.read(self.gpath("products"), {})
        self.assertEqual(listing["count"], 0)
        self.assertEqual(listing["limit"], 200)
        self.assertEqual(listing["host"], "yunshan.example")
        self.assertEqual(listing["currency"], "USD")
        item = self.product()
        self.assertEqual(item["availability_name"], "有货")
        self.assertEqual(item["condition_name"], "全新")
        self.assertEqual(item["issues"], [])
        listing = geo.read(self.gpath("products"), {})
        self.assertEqual(listing["count"], 1)
        updated = geo.write("PUT", f"/api/geo/products/{item['id']}",
                            {"title": "云杉协作专业版", "price": 199.0})["product"]
        self.assertEqual(updated["title"], "云杉协作专业版")
        self.assertEqual(updated["price"], 199.0)
        with self.assertRaises(commander.ChatError):
            self.product(title="重复编号")
        with self.assertRaises(commander.ChatError):
            self.product(sku="SKU-2", url="https://other.example/p/2")
        with self.assertRaises(commander.ChatError):
            self.product(sku="SKU-3", price=-1)
        geo.write("POST", self.gpath("products/delete"), {"ids": [item["id"]]})
        self.assertEqual(geo.read(self.gpath("products"), {})["count"], 0)

    def test_product_issues_and_filters(self):
        self.product(sku="SKU-1")
        self.product(sku="SKU-2", title="另一个商品", url="", image_url="",
                     price=None, description="", gtin="", mpn="")
        listing = geo.read(self.gpath("products"), {"issues": ["1"]})
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["sku"], "SKU-2")
        issues = listing["items"][0]["issues"]
        self.assertIn("缺商品链接", issues)
        self.assertIn("缺图片", issues)
        self.assertIn("缺价格", issues)
        self.assertIn("建议补 GTIN 或型号（MPN）", issues)
        result = geo.read(self.gpath("products"), {"keyword": ["专业版"]})
        self.assertEqual(result["total"], 0)

    def test_product_import_csv(self):
        csv_text = ('id,title,description,price,availability,link,image_link,brand,gtin,product_type\n'
                    'SKU-A,标准版,入门套餐,99.00 USD,in stock,'
                    'https://yunshan.example/a,https://yunshan.example/a.png,云杉,123,协作软件\n'
                    'SKU-B,专业版,进阶套餐,199.00,有货,'
                    'https://yunshan.example/b,https://yunshan.example/b.png,云杉,,协作软件\n'
                    'SKU-C,外部商品,别的站,10.00,有货,https://other.example/c,,,,\n')
        boundary = "ProductBoundary"
        raw = (
            ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"products.csv\"\r\n"
             "Content-Type: text/csv\r\n\r\n" % boundary).encode() + csv_text.encode()
            + ("\r\n--%s--\r\n" % boundary).encode())
        result = geo_site.handle_upload(
            self.gpath("products/import"), "multipart/form-data; boundary=" + boundary, raw)
        self.assertIn("新增 2 个", result["message"])
        self.assertIn("跳过 1 行", result["message"])
        listing = geo.read(self.gpath("products"), {})
        self.assertEqual(listing["count"], 2)
        self.assertEqual(listing["items"][0]["price"], 199.0)
        self.assertEqual(listing["items"][0]["category"], "协作软件")
        again = geo_site.handle_upload(
            self.gpath("products/import"), "multipart/form-data; boundary=" + boundary, raw)
        self.assertIn("更新 2 个", again["message"])

    def test_product_feed_xml_and_tsv(self):
        self.product()
        xml = geo.read(self.gpath("products/feed"), {"format": ["xml"]})
        self.assertIn("<g:id>SKU-1</g:id>", xml["text"])
        self.assertIn("<g:price>99.00 USD</g:price>", xml["text"])
        self.assertIn("<g:availability>in stock</g:availability>", xml["text"])
        self.assertIn("<g:image_link>https://yunshan.example/p/1.png</g:image_link>",
                      xml["text"])
        self.assertEqual(xml["file"], "products.xml")
        tsv = geo.read(self.gpath("products/feed"), {"format": ["tsv"]})
        self.assertIn("id\ttitle\tdescription", tsv["text"])
        self.assertIn("SKU-1\t云杉协作标准版", tsv["text"])

    def test_product_feed_url_and_public_feed(self):
        self.product()
        origin = "http://127.0.0.1:8766"
        data = geo.read(self.gpath("products/feed-url"), {"origin": [origin]})
        self.assertIn(origin + "/geo-products/", data["url"])
        self.assertTrue(data["url"].endswith(".xml"))
        token = data["url"].split("/geo-products/")[1]
        path = "/geo-products/" + token
        self.assertTrue(geo_site.product_feed_public(path))
        status, content, content_type, headers = geo_site.product_feed_response(path)
        self.assertEqual(status, 200)
        self.assertIn("application/xml", content_type)
        self.assertIn(b"<g:id>SKU-1</g:id>", content)
        self.assertEqual(headers["X-Robots-Tag"], "noindex")
        data = geo.read(self.gpath("products/feed-url"), {"origin": [origin]})
        self.assertTrue(data["last_fetch_at"])
        new = geo.read(self.gpath("products/feed-url"),
                       {"origin": [origin], "reset": ["1"]})
        self.assertNotEqual(new["url"], data["url"])
        self.assertEqual(geo_site.product_feed_response(path)[0], 404)

    def test_products_are_tenant_scoped(self):
        self.product()
        other = tenancy.create_tenant("另一个客户")
        tenancy.activate({"tenant_id": other, "user_id": "u", "username": "u", "role": "member"})
        with self.assertRaises(commander.ChatError):
            geo.read(self.gpath("products"), {})
