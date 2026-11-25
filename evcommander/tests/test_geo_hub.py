"""GEO brand knowledge hub: settings, pages, public site, leads and domains."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import customers
import geo
import geo_content
import geo_hub
import tenancy


PROJECT = {
    "brand_name": "云杉科技", "aliases": ["云杉"], "website": "https://example.com",
    "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
    "market": "cn", "locales": [],
}
PASS_BODY = ("# 企业软件哪家好\n\n云杉科技成立于 2016 年，总部在深圳。\n\n"
             "## 选择要点\n\n- 对比协作能力\n- 对比部署方式\n")
ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN", "EV_HUB_CNAME",
            "EV_HUB_DAILY_LIMIT", "EV_HUB_DNS_DOH")


class HubBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["EV_PAY_MOCK"] = "0"
        for key in ("EV_SEED_DEMO", "EV_TRIAL_ORIGIN", "EV_HUB_CNAME",
                    "EV_HUB_DAILY_LIMIT", "EV_HUB_DNS_DOH"):
            os.environ.pop(key, None)
        geo_hub._LEAD_HITS.clear()
        tenancy.activate(tenancy.local_identity())
        self.project_id = geo.write("POST", geo.PROJECTS, PROJECT)["project"]["id"]
        self.path = lambda suffix="": (f"/api/geo/projects/{self.project_id}/{suffix}".rstrip("/")
                                       if suffix else f"/api/geo/projects/{self.project_id}")
        geo.write("POST", self.path("facts"), {
            "fact_key": "founded", "label": "成立时间", "value": "2016 年成立于深圳",
        })

    def tearDown(self):
        tenancy.activate(None)
        commander.DATA_DIR = self.old_dir
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        geo_hub._LEAD_HITS.clear()
        self.temp.cleanup()

    def settings(self, **values):
        body = {"title": "云杉帮助中心", "intro": "企业协作软件的使用说明",
                "leads_enabled": True, "status": 1}
        body.update(values)
        return geo.write("PUT", self.path("hub"), body)["site"]

    def content(self, title="企业软件怎么选", body=PASS_BODY, genre="qa_answer",
                checked=True):
        created = geo.write("POST", self.path("contents"), {
            "question": "企业软件哪家好？", "genre": genre,
        })["content"]
        geo.write("PUT", f"/api/geo/contents/{created['id']}",
                  {"title": title, "body": body})
        if checked:
            geo.write("POST", f"/api/geo/contents/{created['id']}/check", {})
        return created["id"]

    def add(self, content_id, section="answers", checked=True):
        if checked:
            geo.write("POST", f"/api/geo/contents/{content_id}/check", {})
        return geo.write("POST", self.path("hub/pages"), {
            "content_id": content_id, "section": section,
        })["page"]

    def public(self, method, path, headers=None, body=b"", query=None):
        headers = {"Host": "127.0.0.1:8766", "User-Agent": "Mozilla/5.0", **(headers or {})}
        return geo_hub.public_response(method, path, query or {}, headers, body, "10.0.0.1")


class HubSettingsTests(HubBase):
    def test_defaults_and_settings(self):
        site = geo.read(self.path("hub"), {})["site"]
        self.assertEqual(site["title"], "云杉科技")
        self.assertEqual(site["intro"], "做企业协作软件")
        self.assertFalse(site["leads_enabled"])
        self.assertEqual(site["status"], 1)
        self.assertTrue(site["can_edit"])
        self.assertEqual(site["preview_url"], f"/geo-hub/{self.project_id}/")

        site = self.settings(title="云杉帮助中心", intro="使用说明", leads_enabled=True)
        self.assertEqual(site["title"], "云杉帮助中心")
        self.assertTrue(site["leads_enabled"])
        self.assertEqual(site["domain_status"], "none")

        with self.assertRaises(commander.ChatError):
            self.settings(title="短")
        with self.assertRaises(commander.ChatError):
            self.settings(status="坏值")

    def test_origin_builds_absolute_preview(self):
        site = geo.read(self.path("hub"), {"origin": ["https://help.example.com"]})["site"]
        self.assertEqual(site["preview_url"], f"https://help.example.com/geo-hub/{self.project_id}/")
        site = geo.read(self.path("hub"), {"origin": ["javascript:alert(1)"]})["site"]
        self.assertEqual(site["preview_url"], f"/geo-hub/{self.project_id}/")


class HubPageTests(HubBase):
    def test_candidates_add_and_duplicate(self):
        content_id = self.content(checked=False)
        candidates = geo.read(self.path("hub/candidates"), {})["items"]
        self.assertEqual(candidates[0]["content_id"], content_id)
        self.assertFalse(candidates[0]["ok"])
        self.assertIn("质检", candidates[0]["reason"])

        page = self.add(content_id)
        self.assertEqual(page["status"], "live")
        self.assertEqual(page["section"], "answers")
        self.assertFalse(page["outdated"])
        site = geo.read(self.path("hub"), {})["site"]
        self.assertEqual(site["pages_live"], 1)

        self.assertTrue(geo.read(self.path("hub/candidates"), {})["items"] == [])
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("hub/pages"), {
                "content_id": content_id, "section": "answers"})

    def test_section_offline_online(self):
        content_id = self.content()
        page = self.add(content_id)
        page = geo.write("PUT", f"/api/geo/hub/pages/{page['id']}",
                         {"live": False})["page"]
        self.assertEqual(page["status"], "offline")
        self.assertEqual(self.public("GET", f"/geo-hub/{self.project_id}/p/{page['id']}")[0], 404)

        page = geo.write("PUT", f"/api/geo/hub/pages/{page['id']}",
                         {"live": True})["page"]
        self.assertEqual(page["status"], "live")
        self.assertEqual(self.public("GET", f"/geo-hub/{self.project_id}/p/{page['id']}")[0], 200)

        page = geo.write("PUT", f"/api/geo/hub/pages/{page['id']}",
                         {"section": "guides"})["page"]
        self.assertEqual(page["section"], "guides")
        self.assertEqual(page["section_name"], "使用指南")

    def test_sync_gate_codes_and_snapshot(self):
        content_id = self.content()
        page = self.add(content_id)
        geo.write("PUT", f"/api/geo/contents/{content_id}",
                  {"title": "企业软件怎么选", "body": PASS_BODY + "\n追加一句。\n"})
        listed = geo.read(self.path("hub/pages"), {})["items"][0]
        self.assertTrue(listed["outdated"])
        with self.assertRaises(geo_hub.GateError) as caught:
            geo.write("POST", f"/api/geo/hub/pages/{page['id']}/sync", {})
        self.assertEqual(caught.exception.code, "gate_stale")

        geo.write("POST", f"/api/geo/contents/{content_id}/check", {})
        page = geo.write("POST", f"/api/geo/hub/pages/{page['id']}/sync", {})["page"]
        self.assertFalse(page["outdated"])

        stale = self.content(body="云杉科技提供免费试用。云杉科技成立于 2016 年。")
        with self.assertRaises(geo_hub.GateError) as caught:
            self.add(stale, checked=False)
        self.assertEqual(caught.exception.code, "gate_review")

        blank = self.content(body="", checked=False)
        with self.assertRaises(commander.ChatError):
            geo.write("POST", self.path("hub/pages"), {"content_id": blank})

    def test_daily_limit(self):
        os.environ["EV_HUB_DAILY_LIMIT"] = "1"
        first = self.content()
        second = self.content(title="第二篇")
        self.add(first)
        with self.assertRaises(commander.ChatError):
            self.add(second)

    def test_publication_records(self):
        content_id = self.content()
        page = self.add(content_id)
        with commander.database() as db:
            row = db.execute("SELECT * FROM geo_publications WHERE content_id=?",
                             (content_id,)).fetchone()
        self.assertEqual(row["channel_kind"], "hub")
        self.assertEqual(row["status"], "published")
        self.assertIn(f"/geo-hub/{self.project_id}/p/{page['id']}", row["url"])

        geo.write("PUT", f"/api/geo/hub/pages/{page['id']}", {"live": False})
        with commander.database() as db:
            row = db.execute("SELECT status FROM geo_publications WHERE content_id=?",
                             (content_id,)).fetchone()
        self.assertEqual(row["status"], "offline")


class HubPublicTests(HubBase):
    def test_public_pages_seo_and_escaping(self):
        self.settings(leads_enabled=True)
        body = PASS_BODY + "\n<script>alert(1)</script>\n\n[官网](https://example.com)\n"
        content_id = self.content(body=body)
        geo.write("POST", f"/api/geo/contents/{content_id}/approve", {"reason": "验收放行"})
        page = self.add(content_id, checked=False)
        home = self.public("GET", f"/geo-hub/{self.project_id}/")
        self.assertEqual(home[0], 200)
        self.assertIn("noindex", home[3]["X-Robots-Tag"])
        self.assertIn("云杉帮助中心", home[1].decode("utf-8"))

        detail = self.public("GET", f"/geo-hub/{self.project_id}/p/{page['id']}")
        html = detail[1].decode("utf-8")
        self.assertEqual(detail[0], 200)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('href="https://example.com"', html)
        self.assertIn("2016 年成立于深圳", html)

        robots = self.public("GET", f"/geo-hub/{self.project_id}/robots.txt")
        self.assertIn("Disallow: /", robots[1].decode("utf-8"))
        llms = self.public("GET", f"/geo-hub/{self.project_id}/llms.txt")
        text = llms[1].decode("utf-8")
        self.assertIn("# 云杉帮助中心", text)
        self.assertIn("企业软件怎么选", text)
        self.assertIn("成立时间", text)

    def test_closed_hub_and_missing_pages(self):
        content_id = self.content()
        page = self.add(content_id)
        self.settings(status=0)
        self.assertEqual(self.public("GET", f"/geo-hub/{self.project_id}/")[0], 404)
        self.assertEqual(self.public("GET", f"/geo-hub/{self.project_id}/p/{page['id']}")[0], 404)
        self.settings(status=1)
        self.assertEqual(self.public("GET", f"/geo-hub/{self.project_id}/p/9999")[0], 404)
        self.assertEqual(self.public("GET", "/geo-hub/9999/")[0], 404)

    def test_views_and_stats(self):
        content_id = self.content()
        page = self.add(content_id)
        self.public("GET", f"/geo-hub/{self.project_id}/",
                    {"User-Agent": "Mozilla/5.0 (compatible; GPTBot/1.0)"})
        self.public("GET", f"/geo-hub/{self.project_id}/p/{page['id']}",
                    {"User-Agent": "Mozilla/5.0", "Referer": "https://chatgpt.com/c/1"})
        self.public("GET", f"/geo-hub/{self.project_id}/")
        stats = geo.read(self.path("hub/stats"), {"days": ["30"]})
        self.assertEqual(stats["bot_total"], 1)
        self.assertEqual(stats["ai_visits"], 1)
        self.assertEqual(stats["other_visits"], 1)
        self.assertEqual(stats["bots"][0]["name"], "GPTBot")
        self.assertEqual(stats["sources"][0]["name"], "ChatGPT")
        self.assertIn("近 30 天", stats["note"])

    def test_lead_form(self):
        self.settings(leads_enabled=True)
        content_id = self.content()
        page = self.add(content_id)
        status, body, ctype, headers = self.public(
            "POST", f"/geo-hub/{self.project_id}/lead",
            body=b"name=zhang&phone=13800000011&message=hello&page_id=" + str(page["id"]).encode())
        self.assertEqual(status, 303)
        self.assertIn("thanks=1", headers["Location"])
        with tenancy.scope("demo"):
            with commander.database() as db:
                customers.initialize(db)
                row = db.execute("SELECT * FROM crm_customers").fetchone()
                details = json.loads(row["details"])
                self.assertEqual(row["source"], "hub_lead")
                self.assertEqual(details["phone"], "13800000011")
                self.assertIn("品牌知识页", details["notes"])
        contact = customers.contact(row)
        self.assertEqual(contact["source_label"], "品牌知识页留资")

        duplicate = self.public(
            "POST", f"/geo-hub/{self.project_id}/lead",
            body=b"name=again&phone=13800000011")
        self.assertEqual(duplicate[0], 303)
        with tenancy.scope("demo"):
            with commander.database() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM crm_customers").fetchone()[0], 1)
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM geo_hub_leads").fetchone()[0], 1)
        stats = geo.read(self.path("hub/stats"), {"days": ["30"]})
        self.assertEqual(stats["leads"], 1)

        missing = self.public("POST", f"/geo-hub/{self.project_id}/lead", body=b"name=x")
        self.assertEqual(missing[0], 400)
        honeypot = self.public(
            "POST", f"/geo-hub/{self.project_id}/lead",
            body=b"name=x&phone=13800000012&website=spam")
        self.assertEqual(honeypot[0], 303)
        with tenancy.scope("demo"):
            with commander.database() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM crm_customers").fetchone()[0], 1)

    def test_lead_rate_limit(self):
        self.settings(leads_enabled=True)
        for index in range(geo_hub.LEAD_LIMIT):
            result = self.public(
                "POST", f"/geo-hub/{self.project_id}/lead",
                body=f"name=u{index}&phone=1390000000{index}".encode())
            self.assertEqual(result[0], 303)
        blocked = self.public("POST", f"/geo-hub/{self.project_id}/lead",
                              body=b"name=x&phone=13911112222")
        self.assertEqual(blocked[0], 429)


class HubDomainTests(HubBase):
    def test_bind_invalid_conflict_and_clear(self):
        os.environ["EV_HUB_CNAME"] = "hub.platform.test"
        site = geo.write("PUT", self.path("hub/domain"), {"domain": "Help.Example.com"})["site"]
        self.assertEqual(site["domain"], "help.example.com")
        self.assertEqual(site["domain_status"], "pending_dns")
        self.assertEqual(site["domain_status_name"], "等待解析")
        types = [record["type"] for record in site["dns"]]
        self.assertEqual(types, ["CNAME", "TXT"])
        self.assertEqual(site["dns"][0]["value"], "hub.platform.test")
        self.assertTrue(site["icp_notice"])

        with self.assertRaises(commander.ChatError):
            geo.write("PUT", self.path("hub/domain"), {"domain": "not a domain"})

        other = tenancy.create_tenant("另一个客户")
        tenancy.activate({"tenant_id": other, "user_id": "u", "username": "u", "role": "member"})
        other_project = geo.write("POST", geo.PROJECTS, PROJECT)["project"]["id"]
        with self.assertRaises(commander.ChatError):
            geo.write("PUT", f"/api/geo/projects/{other_project}/hub/domain",
                      {"domain": "help.example.com"})
        tenancy.activate(tenancy.local_identity())
        self.assertEqual(geo.write("PUT", self.path("hub/domain"), {"domain": ""})["site"]["domain"], "")

    def test_domain_check_statuses(self):
        os.environ["EV_HUB_CNAME"] = "hub.platform.test"
        site = geo.write("PUT", self.path("hub/domain"), {"domain": "help.example.com"})["site"]
        token = site["dns"][1]["value"]

        with mock.patch.object(geo_hub, "_addresses", return_value=set()), \
                mock.patch.object(geo_hub, "_dns_values", return_value=[]):
            site = geo.write("POST", self.path("hub/domain/check"), {})["site"]
        self.assertEqual(site["domain_status"], "pending_dns")

        addresses = lambda host: {"1.2.3.4"}  # noqa: E731
        with mock.patch.object(geo_hub, "_addresses", side_effect=addresses), \
                mock.patch.object(geo_hub, "_dns_values", return_value=["wrong"]):
            site = geo.write("POST", self.path("hub/domain/check"), {})["site"]
        self.assertEqual(site["domain_status"], "failed")

        with mock.patch.object(geo_hub, "_addresses", side_effect=addresses), \
                mock.patch.object(geo_hub, "_dns_values", return_value=[token]), \
                mock.patch.object(geo_hub, "_probe_verify", return_value=False):
            site = geo.write("POST", self.path("hub/domain/check"), {})["site"]
        self.assertEqual(site["domain_status"], "pending_cert")

        with mock.patch.object(geo_hub, "_addresses", side_effect=addresses), \
                mock.patch.object(geo_hub, "_dns_values", return_value=[token]), \
                mock.patch.object(geo_hub, "_probe_verify", return_value=True):
            site = geo.write("POST", self.path("hub/domain/check"), {})["site"]
        self.assertEqual(site["domain_status"], "active")
        self.assertEqual(site["domain_url"], "https://help.example.com/")
        self.assertEqual(site["dns"], [])

    def test_domain_public_serving_and_verify(self):
        os.environ["EV_HUB_CNAME"] = "hub.platform.test"
        self.settings()
        content_id = self.content()
        page = self.add(content_id)
        site = geo.write("PUT", self.path("hub/domain"), {"domain": "help.example.com"})["site"]
        token = site["dns"][1]["value"]
        # Not active yet: the domain does not serve pages, but verification works.
        self.assertEqual(self.public("GET", "/", {"Host": "help.example.com"})[0], 404)
        verify = self.public("GET", geo_hub.DOMAIN_VERIFY, {"Host": "help.example.com"})
        self.assertEqual(verify[0], 200)
        self.assertEqual(verify[1].decode(), token)

        with commander.database() as db:
            db.execute("UPDATE geo_hubs SET domain_status='active',domain_note=''")
        self.assertEqual(self.public("GET", "/", {"Host": "help.example.com"})[0], 200)
        detail = self.public("GET", f"/p/{page['id']}", {"Host": "help.example.com"})
        self.assertEqual(detail[0], 200)
        llms = self.public("GET", "/llms.txt", {"Host": "help.example.com"})
        self.assertIn("https://help.example.com/p/", llms[1].decode())
        sitemap = self.public("GET", "/sitemap.xml", {"Host": "help.example.com"})
        self.assertIn("https://help.example.com/p/", sitemap[1].decode())
        robots = self.public("GET", "/robots.txt", {"Host": "help.example.com"})
        self.assertIn("Allow: /", robots[1].decode())
        self.assertTrue(geo_hub.handles("/", {"Host": "help.example.com"}))
        self.assertFalse(geo_hub.handles("/", {"Host": "127.0.0.1:8766"}))


class HubIsolationTests(HubBase):
    def test_hub_is_tenant_scoped_but_public_pages_are_by_id(self):
        self.settings()
        content_id = self.content()
        page = self.add(content_id)
        hub_id = geo.read(self.path("hub"), {})["site"]["id"]

        other = tenancy.create_tenant("另一个客户")
        tenancy.activate({"tenant_id": other, "user_id": "u", "username": "u", "role": "member"})
        with self.assertRaises(commander.ChatError):
            geo.read(self.path("hub"), {})
        with self.assertRaises(commander.ChatError):
            geo.read(self.path("hub/pages"), {})
        own = geo.write("POST", geo.PROJECTS, PROJECT)["project"]["id"]
        site = geo.read(f"/api/geo/projects/{own}/hub", {})["site"]
        self.assertNotEqual(site["id"], hub_id)
        self.assertEqual(site["pages_live"], 0)

        tenancy.activate(None)
        # Public page rendering is intentionally cross-tenant by page id.
        self.assertEqual(self.public("GET", f"/geo-hub/{self.project_id}/p/{page['id']}")[0], 200)

    def test_empty_post_whitelist(self):
        self.assertTrue(geo.empty_post(f"/api/geo/projects/{self.project_id}/hub/domain/check"))
        self.assertTrue(geo.empty_post("/api/geo/hub/pages/1/sync"))
        self.assertFalse(geo.empty_post(f"/api/geo/projects/{self.project_id}/hub/pages"))
        self.assertFalse(geo.empty_post(f"/api/geo/projects/{self.project_id}/hub/domain"))
