"""HTTP route regression tests for serve.py.

Every module's routed paths must reach the right handler through the real
server and return the response shape the archived frontend expects. Tests run
against a PreviewServer on an ephemeral port with an isolated data directory.

Run from evcommander/:
    python -m unittest discover -s tests -v
"""

import http.client
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import customers
import geo
import geo_channel
import geo_site
import geo_sources
import image_generation
import materials
import payments
import serve
import tenancy


TEST_PASSWORD = secrets.token_urlsafe(16)


class RouteServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.old_data_dir = commander.DATA_DIR
        cls.old_mock = os.environ.get("EV_PAY_MOCK")
        os.environ["EV_PAY_MOCK"] = "0"
        commander.DATA_DIR = Path(cls.temp.name) / "server"
        commander.DATA_DIR.mkdir()
        cls.server = serve.PreviewServer(("127.0.0.1", 0), serve.FrontendHandler)
        cls.server.invitation = None
        cls.server.admin_path = "/admin-test"
        cls.server.image_pool = ThreadPoolExecutor(max_workers=1)
        cls.server.bootstrap = serve.BOOTSTRAP
        serve.prepare_archived_preview(cls.server)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.image_pool.shutdown(wait=False)
        commander.DATA_DIR = cls.old_data_dir
        if cls.old_mock is None:
            os.environ.pop("EV_PAY_MOCK", None)
        else:
            os.environ["EV_PAY_MOCK"] = cls.old_mock
        cls.temp.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        commander.DATA_DIR = Path(self.case.name)

    def tearDown(self):
        commander.DATA_DIR = Path(self.temp.name) / "server"
        self.case.cleanup()

    def request(self, method, path, body=None, token=True, content_type="application/json",
                headers=None):
        request_headers = {"Connection": "close"}
        payload = None
        if body is not None:
            payload = (json.dumps(body, ensure_ascii=False).encode("utf-8")
                       if isinstance(body, (dict, list)) else body)
            request_headers["Content-Type"] = content_type
        if token:
            request_headers["Authorization"] = "Bearer " + commander.LOCAL_TOKEN
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            response_type = response.getheader("Content-Type", "")
            data = json.loads(raw.decode("utf-8")) if raw and "json" in response_type else raw
            return response.status, response_type, data
        finally:
            connection.close()

    def raw_request(self, method, path, headers):
        # Send headers only; the server rejects on Content-Length before reading.
        lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{self.port}",
                 "Connection: close"]
        lines.extend(f"{key}: {value}" for key, value in headers.items())
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        data = b""
        with socket.create_connection(("127.0.0.1", self.port), timeout=15) as connection:
            connection.sendall(request)
            try:
                while chunk := connection.recv(4096):
                    data += chunk
            except ConnectionResetError:
                pass
        return int(data.split(b" ", 2)[1]), data

    def test_read_routes_require_token(self):
        for path in ("/api/pay/power-products", "/api/geo/meta", "/api/kb/list",
                     "/agent-api/api/memory/list", "/api/acquisition/regions",
                     "/agent-api/api/ext/inspiration/list"):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path, token=False)
                self.assertEqual(status, 403)

    def test_read_routes_by_module(self):
        cases = {
            "/api/material/list": lambda data: data["code"] == "0000",
            "/api/pay/power-products": lambda data: data["code"] == "0000",
            "/api/bill/list": lambda data: data["code"] == "0000",
            "/api/geo/meta": lambda data: data["ok"] is True,
            "/api/acquisition/regions": lambda data: data["list"] == [],
            "/api/kb/list": lambda data: data["code"] == "0000",
            "/agent-api/api/memory/list": lambda data: data["items"] == [],
            "/agent-api/api/planning/wizard/questions": lambda data: data["questions"],
            "/agent-api/api/ext/inspiration/list": lambda data: data["list"] == [],
            "/scrm-api/api/scrm/contacts": lambda data: data["data"] == [],
            "/agent-api/api/acquisition/leads": lambda data: data["leads"] == [],
        }
        for path, check in cases.items():
            with self.subTest(path=path):
                status, _, data = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIsInstance(data, dict)
                self.assertTrue(check(data), data)

    def test_geo_project_crud_routes(self):
        payload = {"brand_name": "路由测试品牌", "aliases": ["路由"], "website": "",
                   "industry": "测试", "region": "", "description": "",
                   "market": "cn", "locales": ["zh"]}
        status, _, data = self.request("POST", "/api/geo/projects", payload)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        project_id = data["project"]["id"]
        status, _, data = self.request("GET", "/agent-api/api/geo/projects")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        status, _, data = self.request("PUT", f"/api/geo/projects/{project_id}",
                                       {**payload, "brand_name": "改名后"})
        self.assertEqual(status, 200)
        self.assertEqual(data["project"]["brand_name"], "改名后")
        # The archived page posts resume/extract/suggest without a body.
        status, _, data = self.request("POST", f"/api/geo/projects/{project_id}/resume")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, _, data = self.request("DELETE", f"/api/geo/projects/{project_id}")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, _, data = self.request("GET", "/api/geo/projects")
        self.assertEqual(data["total"], 0)

    def test_geo_brand_routes(self):
        payload = {"brand_name": "品牌路由测试", "aliases": [], "website": "",
                   "industry": "测试", "region": "", "description": "",
                   "market": "cn", "locales": []}
        status, _, data = self.request("POST", "/api/geo/projects", payload)
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]

        status, _, data = self.request("POST", f"/api/geo/projects/{project_id}/facts", {
            "fact_key": "founded", "label": "成立时间", "value": "2016 年",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["item"]["status"], "confirmed")
        status, _, data = self.request("GET", f"/agent-api/api/geo/projects/{project_id}/facts")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        self.assertIn("founded", [item["key"] for item in data["keys"]])

        status, _, data = self.request("POST", f"/api/geo/projects/{project_id}/competitors", {
            "items": [{"name": "甲科技", "aliases": ["甲"]}], "source": "manual",
        })
        self.assertEqual(status, 200)
        competitor_id = data["items"][0]["id"]
        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/competitors")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/competitors/sov")
        self.assertEqual(status, 200)
        self.assertEqual(data["total_answers"], 0)
        status, _, data = self.request("DELETE",
                                       f"/api/geo/projects/{project_id}/competitors/{competitor_id}")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_geo_matrix_routes(self):
        payload = {"brand_name": "词库路由测试", "aliases": [], "website": "",
                   "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
                   "market": "cn", "locales": []}
        status, _, data = self.request("POST", "/api/geo/projects", payload)
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]
        base = f"/api/geo/projects/{project_id}/kw-matrix"

        status, _, data = self.request("GET", base)
        self.assertEqual(status, 200)
        self.assertFalse(data["has_matrix"])
        self.assertEqual(len(data["layers"]), 5)
        self.assertEqual(len(data["groups"]), 5)

        status, _, data = self.request("PUT", base, {"roots": {
            "industry": [{"text": "企业软件", "tier": "S"}],
            "slang": [{"text": "哪家好", "tier": "B"}],
        }})
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 2)
        self.assertTrue(data["has_matrix"])

        status, _, data = self.request("POST", f"{base}/terms", {
            "layer": "question", "tier": "A", "texts": ["云杉科技怎么样"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["added"], 1)
        status, _, data = self.request("GET", f"/agent-api/api/geo/projects/{project_id}/kw-matrix/terms")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 3)

        draft = json.dumps({
            "roots": {"industry": [{"text": "协作工具", "tier": "S"}]},
            "layers": {"entity": {"pain": "找不到服务商", "service": "本地部署"}},
        }, ensure_ascii=False)
        with mock.patch.object(commander, "generate", return_value=[draft]):
            # The archived page posts the draft action without a body.
            status, _, data = self.request("POST", f"{base}/draft")
        self.assertEqual(status, 200)
        self.assertTrue(data["has_matrix"])
        self.assertGreater(data["credits"], 0)

        status, _, data = self.request("POST", f"{base}/layers/entity/content")
        self.assertEqual(status, 200)
        self.assertTrue(data["content"]["id"])

    def test_geo_report_routes(self):
        payload = {"brand_name": "报告路由测试", "aliases": [], "website": "",
                   "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
                   "market": "cn", "locales": []}
        status, _, data = self.request("POST", "/api/geo/projects", payload)
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]

        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/reports?period=week")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["report"]["runs"], 0)
        self.assertEqual(len(data["report"]["trend"]), 7)

        status, _, data = self.request("POST",
                                       f"/api/geo/projects/{project_id}/report-shares",
                                       {"period": "week", "offset": 0, "days": 7})
        self.assertEqual(status, 200)
        share = data["share"]
        self.assertTrue(share["token"])
        status, _, data = self.request("GET",
                                       f"/api/geo/projects/{project_id}/report-shares")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)

        # Public links work without the preview token, with either API prefix.
        status, _, data = self.request(
            "GET", "/api/public/geo/report/" + share["token"], token=False)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["report"]["runs"], 0)
        status, _, data = self.request(
            "GET", "/agent-api/api/public/geo/report/" + share["token"], token=False)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        status, content_type, _ = self.request("GET", "/geo-share/" + share["token"], token=False)
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)

        status, _, data = self.request("DELETE", f"/api/geo/report-shares/{share['id']}")
        self.assertEqual(status, 200)
        status, _, data = self.request(
            "GET", "/api/public/geo/report/" + share["token"], token=False)
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])

        status, _, data = self.request(
            "GET", f"/api/geo/projects/{project_id}/diagnoses/estimate")
        self.assertEqual(status, 200)
        self.assertTrue(data["estimate"]["blockers"])
        status, _, data = self.request("POST", f"/api/geo/projects/{project_id}/diagnoses",
                                       {"run_id": 1})
        self.assertEqual(status, 400)
        self.assertIn("不存在", data["detail"])

    def test_geo_diagnosis_routes(self):
        payload = {"brand_name": "诊断路由测试", "aliases": [], "website": "",
                   "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
                   "market": "cn", "locales": []}
        status, _, data = self.request("POST", "/api/geo/projects", payload)
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]

        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/diagnoses")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 0)
        status, _, data = self.request(
            "GET", f"/api/geo/projects/{project_id}/diagnoses/estimate?run_id=0")
        self.assertEqual(status, 200)
        self.assertTrue(data["estimate"]["blockers"])
        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/alerts")
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], [])
        status, _, data = self.request("PUT", "/api/geo/alerts/999", {"status": "handled"})
        self.assertEqual(status, 400)
        self.assertIn("不存在", data["detail"])
        status, _, data = self.request("GET", "/api/geo/diagnoses/999")
        self.assertEqual(status, 400)

    def test_geo_content_routes(self):
        payload = {"brand_name": "内容路由测试", "aliases": [], "website": "",
                   "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
                   "market": "cn", "locales": []}
        status, _, data = self.request("POST", "/api/geo/projects", payload)
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]

        status, _, data = self.request("GET", "/api/geo/content-meta?market=cn")
        self.assertEqual(status, 200)
        self.assertEqual(data["genres"][0]["key"], "qa_answer")
        self.assertTrue(data["targets"])

        status, _, data = self.request("POST", f"/api/geo/projects/{project_id}/contents", {
            "question": "企业软件哪家好？", "genre": "qa_answer", "platforms": ["zhihu"],
        })
        self.assertEqual(status, 200)
        content_id = data["content"]["id"]
        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/contents")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        status, _, data = self.request("GET", f"/api/geo/contents/{content_id}")
        self.assertEqual(status, 200)
        self.assertEqual(data["content"]["question"], "企业软件哪家好？")

        status, _, data = self.request("PUT", f"/api/geo/contents/{content_id}", {
            "title": "标题", "body": "云杉科技成立于 2016 年。",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["content"]["title"], "标题")
        # The archived page posts generate/score/check without a body.
        status, _, data = self.request("POST", f"/api/geo/contents/{content_id}/check")
        self.assertEqual(status, 200)
        self.assertIn("gate", data["content"])
        status, _, data = self.request("POST", f"/api/geo/contents/{content_id}/tickets",
                                       {"platform": "zhihu"})
        self.assertEqual(status, 400)
        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/opportunities")
        self.assertEqual(status, 200)
        self.assertFalse(data["has_run"])
        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/publications")
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], [])
        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/tickets")
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], [])

    def test_geo_hub_routes(self):
        payload = {"brand_name": "知识页路由测试", "aliases": [], "website": "",
                   "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
                   "market": "cn", "locales": []}
        body = ("# 企业软件哪家好\n\n云杉科技成立于 2016 年，总部在深圳。\n\n"
                "## 选择要点\n\n- 对比协作能力\n")
        tenancy.activate(tenancy.local_identity())
        try:
            project = geo.write("POST", geo.PROJECTS, payload)["project"]
            project_id = project["id"]
            geo.write("POST", f"/api/geo/projects/{project_id}/facts", {
                "fact_key": "founded", "label": "成立时间", "value": "2016 年成立于深圳",
            })
            content = geo.write("POST", f"/api/geo/projects/{project_id}/contents", {
                "question": "企业软件哪家好？", "genre": "qa_answer",
            })["content"]
            geo.write("PUT", f"/api/geo/contents/{content['id']}",
                      {"title": "企业软件怎么选", "body": body})
            geo.write("POST", f"/api/geo/contents/{content['id']}/check", {})
        finally:
            tenancy.activate(None)

        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/hub")
        self.assertEqual(status, 200)
        self.assertEqual(data["site"]["title"], "知识页路由测试")
        status, _, data = self.request("PUT", f"/api/geo/projects/{project_id}/hub", {
            "title": "云杉帮助中心", "intro": "使用说明", "leads_enabled": True, "status": 1,
        })
        self.assertEqual(status, 200)
        self.assertTrue(data["site"]["leads_enabled"])

        status, _, data = self.request("POST", f"/api/geo/projects/{project_id}/hub/pages", {
            "content_id": content["id"], "section": "answers",
        })
        self.assertEqual(status, 200)
        page_id = data["page"]["id"]
        status, _, data = self.request(
            "GET", f"/agent-api/api/geo/projects/{project_id}/hub/pages")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)

        status, _, data = self.request("PUT", f"/api/geo/projects/{project_id}/hub/domain",
                                       {"domain": "route.example.com"})
        self.assertEqual(status, 200)
        self.assertEqual(data["site"]["domain_status"], "pending_dns")
        with mock.patch.dict(os.environ, {"EV_HUB_CNAME": ""}):
            status, _, data = self.request(
                "POST", f"/api/geo/projects/{project_id}/hub/domain/check")
        self.assertEqual(status, 200)
        self.assertEqual(data["site"]["domain_status"], "failed")

        status, _, _ = self.request("GET", f"/api/geo/projects/{project_id}/hub", token=False)
        self.assertEqual(status, 403)
        status, kind, raw = self.request("GET", f"/geo-hub/{project_id}/", token=False)
        self.assertEqual(status, 200)
        self.assertIn("text/html", kind)
        self.assertIn("云杉帮助中心", raw.decode("utf-8"))
        status, kind, raw = self.request("GET", f"/geo-hub/{project_id}/p/{page_id}", token=False)
        self.assertEqual(status, 200)
        self.assertIn("企业软件怎么选", raw.decode("utf-8"))
        status, kind, raw = self.request("GET", f"/geo-hub/{project_id}/llms.txt", token=False)
        self.assertEqual(status, 200)
        self.assertIn("text/plain", kind)

        status, _, _ = self.request(
            "POST", f"/geo-hub/{project_id}/lead",
            b"name=route&phone=13800000077", token=False,
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 303)

        tenancy.activate(tenancy.local_identity())
        try:
            geo.write("PUT", f"/api/geo/contents/{content['id']}",
                      {"title": "企业软件怎么选", "body": body + "\n追加一句。\n"})
        finally:
            tenancy.activate(None)
        status, _, data = self.request("POST", f"/api/geo/hub/pages/{page_id}/sync")
        self.assertEqual(status, 400)
        self.assertEqual(data["detail"]["code"], "gate_stale")

    def test_geo_sources_routes(self):
        status, _, data = self.request("POST", "/api/geo/projects", {
            "brand_name": "引用路由测试", "aliases": [], "website": "https://example.com",
            "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
            "market": "cn", "locales": [],
        })
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]

        status, _, data = self.request(
            "GET", f"/agent-api/api/geo/projects/{project_id}/gaps")
        self.assertEqual(status, 200)
        self.assertIsNone(data["run"])
        self.assertEqual(data["items"], [])

        status, _, data = self.request(
            "GET", f"/api/geo/projects/{project_id}/sources?days=30")
        self.assertEqual(status, 200)
        self.assertEqual(data["answers"], 0)
        self.assertEqual(data["types"], [])
        self.assertEqual(data["items"], [])
        self.assertEqual(data["total"], 0)

        page = ("<html><head><title>企业软件怎么选？</title><meta name=\"author\" "
                "content=\"张三\"></head><body><h1>怎么选</h1><h2>常见问题</h2>"
                "<h2>选择标准</h2><ul><li>要点</li></ul><p>2024 年 30% 的企业在用。"
                + ("正文内容说明。" * 120) + "</p></body></html>")
        with mock.patch.object(geo_sources, "_public_fetch",
                               return_value=(200, "https://zhihu.com/question/1", page)):
            status, _, data = self.request(
                "POST", f"/api/geo/projects/{project_id}/cited-pages/analyze",
                {"urls": ["https://zhihu.com/question/1"], "days": 30})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["cached"])
        self.assertEqual(data["credits"], 0)
        self.assertTrue(data["pages"][0]["ok"])
        self.assertTrue(data["pages"][0]["features"])

        status, _, _ = self.request(
            "GET", f"/api/geo/projects/{project_id}/sources", token=False)
        self.assertEqual(status, 403)

    def test_geo_site_audit_routes(self):
        status, _, data = self.request("POST", "/api/geo/projects", {
            "brand_name": "官网路由测试", "aliases": [], "website": "https://site-route.example",
            "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
            "market": "cn", "locales": [],
        })
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]

        html = ("<html lang=\"zh-CN\"><head><title>官网路由测试 - 企业软件</title>"
                "<meta name=\"description\" content=\"官网路由测试做企业协作软件。\">"
                "<meta name=\"viewport\" content=\"width=device-width\"><link rel=\"canonical\" "
                "href=\"https://site-route.example/\"><script type=\"application/ld+json\">{}</script>"
                "</head><body><h1>官网路由测试</h1><h2>A</h2><h2>B</h2><ul><li>x</li></ul><p>"
                + ("官网路由测试的正文内容。" * 40) + "</p></body></html>")
        robots = "User-agent: *\nAllow: /\nSitemap: https://site-route.example/sitemap.xml\n"
        llms = "# 官网路由测试\n\n> 企业协作软件。\n"
        sitemap = "<urlset><url><loc>https://site-route.example/a</loc></url></urlset>"

        def fetch(url, base_host, timeout=10):
            if url.endswith("/robots.txt"):
                return geo_site._Fetched(200, url, robots)
            if url.endswith("/llms.txt"):
                return geo_site._Fetched(200, url, llms)
            if url.endswith("/sitemap.xml"):
                return geo_site._Fetched(200, url, sitemap)
            return geo_site._Fetched(200, url, html)

        with mock.patch.dict(os.environ, {"EV_SITE_AUDIT_ALLOW_PRIVATE": "1"}), \
                mock.patch.object(geo_site, "_fetch", side_effect=fetch):
            status, _, data = self.request(
                "POST", f"/api/geo/projects/{project_id}/site-audits", {})
        self.assertEqual(status, 200)
        audit = data["audit"]
        self.assertEqual(audit["status"], "done")
        self.assertGreaterEqual(audit["score"], 80)
        status, _, data = self.request(
            "GET", f"/agent-api/api/geo/projects/{project_id}/site-audits")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        status, _, data = self.request("GET", f"/api/geo/site-audits/{audit['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data["audit"]["id"], audit["id"])
        status, _, data = self.request(
            "POST", f"/api/geo/projects/{project_id}/generators/llms", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["kind"], "llms")
        status, _, _ = self.request(
            "GET", f"/api/geo/projects/{project_id}/site-audits", token=False)
        self.assertEqual(status, 403)

    def test_geo_crawl_log_routes(self):
        status, _, data = self.request("POST", "/api/geo/projects", {
            "brand_name": "日志路由测试", "aliases": [], "website": "https://log-route.example",
            "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
            "market": "cn", "locales": [],
        })
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]
        today = datetime.now(timezone.utc).date()
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        stamp = "%02d/%s/%d:08:00:00 +0800" % (today.day, months[today.month - 1], today.year)
        line = ('203.0.113.10 - - [%s] "GET /p HTTP/1.1" 200 1 "-" "GPTBot/1.2"' % stamp)
        boundary = "RouteBoundary"
        raw = (
            "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"access.log\"\r\n"
            "Content-Type: text/plain\r\n\r\n%s\r\n--%s--\r\n" % (boundary, line, boundary)
        ).encode()
        status, _, data = self.request(
            "POST", f"/api/geo/projects/{project_id}/crawl-logs", raw,
            content_type="multipart/form-data; boundary=" + boundary)
        self.assertEqual(status, 200)
        self.assertIn("1 次", data["message"])
        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/crawl-logs")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        upload_id = data["items"][0]["id"]
        status, _, data = self.request(
            "GET", f"/api/geo/projects/{project_id}/crawl-stats?days=30")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        status, _, data = self.request("DELETE", f"/api/geo/crawl-logs/{upload_id}")
        self.assertEqual(status, 200)
        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/crawl-logs")
        self.assertEqual(data["total"], 0)
        status, _, _ = self.request(
            "GET", f"/api/geo/projects/{project_id}/crawl-stats", token=False)
        self.assertEqual(status, 403)

    def test_geo_beacon_routes(self):
        status, _, data = self.request("POST", "/api/geo/projects", {
            "brand_name": "访问统计路由测试", "aliases": [], "website": "https://beacon-route.example",
            "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
            "market": "cn", "locales": [],
        })
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]
        origin = "http://127.0.0.1:%d" % self.port
        status, _, data = self.request(
            "GET", f"/api/geo/projects/{project_id}/beacon?origin={origin}")
        self.assertEqual(status, 200)
        site = data["site"]
        self.assertIn(origin + "/geo-beacon.js?k=", site["snippet"])
        self.assertFalse(site["installed"])
        token = re.search(r"k=([A-Za-z0-9_-]+)", site["snippet"]).group(1)
        status, _, data = self.request("GET", "/geo-beacon.js", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"geo-beacon/hit", data)
        status, _, _ = self.request(
            "POST", "/geo-beacon/hit",
            json.dumps({"k": token, "s": "chatgpt", "p": "/pricing"}).encode(),
            token=False, content_type="text/plain")
        self.assertEqual(status, 204)
        status, _, data = self.request(
            "GET", f"/api/geo/projects/{project_id}/beacon/stats?days=30")
        self.assertEqual(status, 200)
        self.assertEqual(data["ai_visits"], 1)
        self.assertEqual(data["sources"][0]["key"], "chatgpt")
        status, _, data = self.request(
            "PUT", f"/api/geo/projects/{project_id}/beacon?origin={origin}",
            {"leads_enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(data["site"]["leads_enabled"])
        status, _, _ = self.request(
            "GET", f"/api/geo/projects/{project_id}/beacon", token=False)
        self.assertEqual(status, 403)

    def test_geo_import_routes(self):
        status, _, data = self.request("POST", "/api/geo/projects", {
            "brand_name": "报表路由测试", "aliases": [], "website": "https://import-route.example",
            "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
            "market": "cn", "locales": [],
        })
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]
        boundary = "ImportRoute"
        csv_text = ('Top pages,Clicks,Impressions,CTR,Position\n'
                    'https://import-route.example/a,5,50,10%,2\n')
        raw = (
            "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"pages.csv\"\r\n"
            "Content-Type: text/csv\r\n\r\n%s\r\n--%s--\r\n" % (boundary, csv_text, boundary)
        ).encode()
        status, _, data = self.request(
            "POST", f"/api/geo/projects/{project_id}/imports", raw,
            content_type="multipart/form-data; boundary=" + boundary)
        self.assertEqual(status, 200)
        self.assertIn("1 份报表", data["message"])
        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/imports")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["source"], "gsc")
        self.assertEqual(len(data["sources"]), 3)
        import_id = data["items"][0]["id"]
        status, _, data = self.request("GET", f"/api/geo/imports/{import_id}")
        self.assertEqual(status, 200)
        self.assertEqual(data["import"]["kind"], "pages")
        self.assertEqual(data["import"]["items"][0]["clicks"], 5)
        status, _, _ = self.request("DELETE", f"/api/geo/imports/{import_id}")
        self.assertEqual(status, 200)
        status, _, data = self.request("GET", f"/api/geo/projects/{project_id}/imports")
        self.assertEqual(data["total"], 0)
        status, _, _ = self.request(
            "GET", f"/api/geo/projects/{project_id}/imports", token=False)
        self.assertEqual(status, 403)

    def test_geo_product_routes(self):
        status, _, data = self.request("POST", "/api/geo/projects", {
            "brand_name": "商品路由测试", "aliases": [], "website": "https://product-route.example",
            "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
            "market": "global", "locales": [],
        })
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]
        status, _, data = self.request("POST", f"/api/geo/projects/{project_id}/products", {
            "sku": "SKU-R", "title": "路由商品", "price": 99,
            "url": "https://product-route.example/p/1",
            "image_url": "https://product-route.example/p/1.png",
        })
        self.assertEqual(status, 200)
        product_id = data["product"]["id"]
        status, _, data = self.request(
            "GET", f"/api/geo/projects/{project_id}/products")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["availability_name"], "有货")
        status, _, data = self.request(
            "PUT", f"/api/geo/products/{product_id}", {"price": 120})
        self.assertEqual(status, 200)
        self.assertEqual(data["product"]["price"], 120)
        status, _, data = self.request(
            "GET", f"/api/geo/projects/{project_id}/products/feed?format=xml")
        self.assertEqual(status, 200)
        self.assertIn("<g:id>SKU-R</g:id>", data["text"])
        status, _, data = self.request(
            "GET", f"/api/geo/projects/{project_id}/products/feed-url"
                   f"?origin=http://127.0.0.1:{self.port}")
        self.assertEqual(status, 200)
        feed_path = urlsplit(data["url"]).path
        status, _, content = self.request("GET", feed_path, token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"<g:id>SKU-R</g:id>", content)
        boundary = "ProductRoute"
        csv_text = "id,title,price\nSKU-U,导入商品,9.99\n"
        raw = (
            ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"products.csv\"\r\n"
             "Content-Type: text/csv\r\n\r\n" % boundary).encode() + csv_text.encode()
            + ("\r\n--%s--\r\n" % boundary).encode())
        status, _, data = self.request(
            "POST", f"/api/geo/projects/{project_id}/products/import", raw,
            content_type="multipart/form-data; boundary=" + boundary)
        self.assertEqual(status, 200)
        self.assertIn("新增 1 个", data["message"])
        status, _, data = self.request(
            "POST", f"/api/geo/projects/{project_id}/products/delete",
            {"ids": [product_id]})
        self.assertEqual(data["deleted"], 1)
        status, _, _ = self.request(
            "GET", f"/api/geo/projects/{project_id}/products", token=False)
        self.assertEqual(status, 403)

    def test_geo_index_routes(self):
        status, _, data = self.request("POST", "/api/geo/projects", {
            "brand_name": "索引路由测试", "aliases": [], "website": "https://index-route.example",
            "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
            "market": "global", "locales": [],
        })
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]
        status, _, data = self.request(
            "GET", f"/api/geo/projects/{project_id}/index-submit")
        self.assertEqual(status, 200)
        self.assertEqual(data["state"]["host"], "index-route.example")
        self.assertEqual(data["candidates"], [])
        key = data["state"]["indexnow"]["key"]
        self.assertEqual(data["state"]["indexnow"]["key_file"], key + ".txt")
        status, _, data = self.request(
            "PUT", f"/api/geo/projects/{project_id}/index-submit/baidu",
            {"site": "https://index-route.example", "token": "route-token"})
        self.assertEqual(status, 200)
        self.assertTrue(data["state"]["baidu"]["configured"])

        def fetch(url, base_host, timeout=10):
            return geo_site._Fetched(200, url, key)

        with mock.patch.object(geo_site, "_fetch", side_effect=fetch):
            status, _, data = self.request(
                "POST", f"/api/geo/projects/{project_id}/index-submit/verify")
        self.assertEqual(status, 200)
        self.assertTrue(data["state"]["indexnow"]["verified"])

        with mock.patch.object(geo_site, "_post_request", return_value=(200, "")):
            status, _, data = self.request(
                "POST", f"/api/geo/projects/{project_id}/index-submit",
                {"target": "indexnow",
                 "urls": ["https://index-route.example/a"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["state"]["indexnow"]["today"], 1)
        status, _, _ = self.request(
            "GET", f"/api/geo/projects/{project_id}/index-submit", token=False)
        self.assertEqual(status, 403)

    def test_power_prices_route(self):
        status, _, data = self.request("POST", "/api/personal/power-prices", {
            "items": [{"type": "model", "key": "m",
                       "usage": {"prompt_tokens": 1000}}],
        })
        self.assertEqual(status, 200)
        payload = data["data"]
        self.assertEqual(payload["list"][0]["billing_type"], "per_token")
        self.assertEqual(payload["list"][0]["credits"], 0.05)
        self.assertEqual(payload["balance"], 100.0)
        self.assertTrue(payload["enforce"])
        status, _, data = self.request(
            "POST", "/api/personal/power-prices", {"items": []}, token=False)
        self.assertEqual(status, 403)

    def test_studio_stream_charges_and_refunds(self):
        body = {"topic": "周末露营", "goal": "note", "platforms": ["xiaohongshu"]}

        def fake(*args, **kwargs):
            yield "# 标题\n\n正文内容"

        with mock.patch.object(commander, "generate", new=fake):
            status, _, raw = self.request(
                "POST", "/agent-api/api/creative-dept/studio/stream", body)
        self.assertEqual(status, 200)
        self.assertIn(b"data", raw)
        self.assertLess(payments.balance(), 100.0)
        with commander.database() as db:
            notes = [row[0] for row in db.execute(
                "SELECT note FROM pay_bills WHERE kind='consume'").fetchall()]
        self.assertTrue(any(note.startswith("文案工坊：小红书（") for note in notes))

        before = payments.balance()
        with mock.patch.object(commander, "generate",
                               side_effect=commander.ChatError("模型暂不可用")):
            status, _, raw = self.request(
                "POST", "/agent-api/api/creative-dept/studio/stream", body)
        self.assertEqual(status, 200)
        self.assertIn("模型暂不可用".encode("utf-8"), raw)
        self.assertEqual(payments.balance(), before)

    def test_geo_shopify_webflow_routes(self):
        status, _, data = self.request("POST", "/api/geo/projects", {
            "brand_name": "渠道二路由测试", "aliases": [], "website": "https://shop-route.example",
            "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
            "market": "global", "locales": [],
        })
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]
        body = "# 企业软件哪家好\n\n云杉科技成立于 2016 年，总部在深圳。\n"
        status, _, data = self.request("POST", f"/api/geo/projects/{project_id}/contents", {
            "question": "企业软件哪家好？", "genre": "qa_answer",
        })
        content = data["content"]
        self.request("PUT", f"/api/geo/contents/{content['id']}",
                     {"title": "Shopify 渠道文章", "body": body})
        status, _, data = self.request(
            "POST", f"/api/geo/contents/{content['id']}/check", {})
        if data["content"]["gate"]["decision"] not in {"pass", "approved"}:
            self.request("POST", f"/api/geo/contents/{content['id']}/approve",
                         {"reason": "路由验收放行"})
        status, _, data = self.request("POST", "/api/geo/channel-configs", {
            "kind": "shopify", "name": "Shopify 店",
            "config": {"shop": "https://shop.example", "token": "route-shop-token",
                       "status": "publish", "blog": ""},
        })
        self.assertEqual(status, 200)
        channel = data["channel"]
        self.assertEqual(channel["config"]["token"], "")
        status, _, data = self.request("GET", "/api/geo/channel-configs?kind=shopify")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)

        def request(method, url, payload=None, headers=None, body=None, content_type=None):
            if url.endswith("/shop.json"):
                return 200, '{"shop": {"id": 1}}', url
            if url.endswith("/blogs.json"):
                return 200, '{"blogs": [{"id": 9, "handle": "news"}]}', url
            return 201, '{"article": {"id": 88, "handle": "post"}}', url

        with mock.patch.object(geo_channel, "_request", side_effect=request),                 mock.patch.dict(os.environ, {"EV_CHANNEL_SYNC_PUSH": "1"}):
            status, _, data = self.request(
                "POST", f"/api/geo/channel-configs/{channel['id']}/test")
            self.assertEqual(status, 200)
            self.assertTrue(data["ok_test"])
            status, _, data = self.request(
                "POST", f"/api/geo/contents/{content['id']}/push",
                {"channel_id": channel["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok_push"])
        status, _, data = self.request(
            "GET", f"/api/geo/projects/{project_id}/publications")
        self.assertEqual(data["items"][0]["platform"], "shopify")
        self.assertEqual(data["items"][0]["url"],
                         "https://shop.example/blogs/news/post")

        status, _, data = self.request("POST", "/api/geo/channel-configs", {
            "kind": "webflow", "name": "Webflow 站",
            "config": {"token": "route-wf-token", "collection": "col-1",
                       "title_field": "name", "body_field": "post-body",
                       "slug_field": "slug", "status": "draft"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["channel"]["config"]["token"], "")

    def test_geo_channel_routes(self):
        status, _, data = self.request("POST", "/api/geo/projects", {
            "brand_name": "渠道路由测试", "aliases": [], "website": "https://example.com",
            "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
            "market": "cn", "locales": [],
        })
        self.assertEqual(status, 200)
        project_id = data["project"]["id"]
        body = ("# 企业软件哪家好\n\n云杉科技成立于 2016 年，总部在深圳。\n\n"
                "## 选择要点\n\n- 对比协作能力\n")
        tenancy.activate(tenancy.local_identity())
        try:
            geo.write("POST", f"/api/geo/projects/{project_id}/facts", {
                "fact_key": "founded", "label": "成立时间", "value": "2016 年成立于深圳",
            })
            content = geo.write("POST", f"/api/geo/projects/{project_id}/contents", {
                "question": "企业软件哪家好？", "genre": "qa_answer",
            })["content"]
            geo.write("PUT", f"/api/geo/contents/{content['id']}",
                      {"title": "企业软件怎么选", "body": body})
            checked = geo.write("POST", f"/api/geo/contents/{content['id']}/check", {})["content"]
            if checked["gate"]["decision"] not in {"pass", "approved"}:
                geo.write("POST", f"/api/geo/contents/{content['id']}/approve",
                          {"reason": "验收放行"})
        finally:
            tenancy.activate(None)

        status, _, data = self.request("POST", "/api/geo/channel-configs", {
            "kind": "wordpress", "name": "品牌官网",
            "config": {"base_url": "https://wp.example", "username": "admin",
                       "app_password": "route-secret", "status": "publish"},
        })
        self.assertEqual(status, 200)
        channel = data["channel"]
        self.assertEqual(channel["config"]["app_password"], "")
        channel_id = channel["id"]
        status, _, data = self.request("GET", "/api/geo/channel-configs")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        self.assertNotIn("route-secret", json.dumps(data))

        def fake(method, url, payload=None, headers=None, body=None, content_type=None):
            if method == "GET":
                return 200, '{"id": 1, "name": "admin"}', url
            return 201, '{"id": 4, "link": "https://wp.example/post/4"}', url

        with mock.patch.object(geo_channel, "_request", side_effect=fake), \
                mock.patch.dict(os.environ, {"EV_CHANNEL_SYNC_PUSH": "1"}):
            status, _, data = self.request(
                "POST", f"/api/geo/channel-configs/{channel_id}/test")
            self.assertEqual(status, 200)
            self.assertTrue(data["ok_test"])
            status, _, data = self.request(
                "POST", f"/api/geo/contents/{content['id']}/push",
                {"channel_id": channel_id, "cover_url": ""})
            self.assertEqual(status, 200)
            self.assertTrue(data["ok_push"])
        status, _, data = self.request(
            "GET", f"/agent-api/api/geo/projects/{project_id}/publications")
        self.assertEqual(status, 200)
        self.assertEqual(data["items"][0]["platform"], "wordpress")
        self.assertEqual(data["items"][0]["url"], "https://wp.example/post/4")
        status, _, _ = self.request("GET", "/api/geo/channel-configs", token=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("DELETE", f"/api/geo/channel-configs/{channel_id}")
        self.assertEqual(status, 200)

    def test_leads_save_and_list_routes(self):
        status, _, data = self.request(
            "POST", "/agent-api/api/acquisition/leads/save",
            {"items": [{"name": "路由测试客户", "phone": "13800000002"}], "source": "map"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["ingested"], 1)
        status, _, data = self.request("GET", "/agent-api/api/acquisition/leads")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["leads"][0]["name"], "路由测试客户")

    def test_contact_delete_route(self):
        status, _, data = self.request("POST", "/scrm-api/api/scrm/contacts",
                                       {"name": "待删除客户", "phone": "13800000003"})
        self.assertEqual(status, 200)
        contact_id = data["data"]["id"]
        status, _, data = self.request("DELETE", f"/scrm-api/api/scrm/contacts/{contact_id}")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, _, data = self.request("GET", "/scrm-api/api/scrm/contacts")
        self.assertEqual(data["total"], 0)

    def test_card_exchange_route(self):
        with commander.database() as db:
            db.execute("BEGIN IMMEDIATE")
            payments.initialize(db)
            db.execute("INSERT INTO pay_cards (code,power_amount,used) VALUES ('ROUTE-5',5,0)")
        status, _, data = self.request("POST", "/api/card/exchange",
                                       {"card_no": "ROUTE-5", "card_password": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(data["code"], "0000")
        self.assertTrue(data["data"]["ok"])

    def test_chat_charges_power_by_tokens_and_rejects_insufficient_balance(self):
        def fake(messages, usage=None, **kwargs):
            if usage is not None:
                usage.update({"prompt_tokens": 1000, "completion_tokens": 100})
            yield "你好"

        body = {"messages": [{"role": "user", "content": "hi"}]}
        with mock.patch.object(commander, "generate", fake):
            status, _, raw = self.request("POST", "/api/chat/send", body)
        self.assertEqual(status, 200)
        self.assertIn("你好", raw.decode("utf-8"))
        self.assertAlmostEqual(
            payments.balance(), 100 - payments.token_charge(1000, 100))
        with commander.database() as db:
            payments.initialize(db)
            db.execute("UPDATE pay_account SET power=0 WHERE tenant_id=?",
                       (commander.tenant(),))
        with mock.patch.object(commander, "generate", fake):
            status, _, raw = self.request("POST", "/api/chat/send", body)
        self.assertEqual(status, 200)
        self.assertIn("算力余额不足", raw.decode("utf-8"))
        self.assertEqual(payments.balance(), 0)

    def test_chat_refunds_when_model_fails(self):
        def boom(messages, **kwargs):
            raise commander.ChatError("模型不可用")
            yield  # pragma: no cover

        with mock.patch.object(commander, "generate", boom):
            status, _, raw = self.request("POST", "/api/chat/send", {
                "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(status, 200)
        self.assertIn("模型不可用", raw.decode("utf-8"))
        self.assertEqual(payments.balance(), 100.0)
        self.assertEqual(payments.read("/api/bill/list", {})["total"], 0)

    def test_image_charges_power_and_refunds_failure(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        body = {"prompt": "测试生图", "params": {"size": "1:1"},
                "model_code": image_generation.MODEL, "count": 1}
        with mock.patch.object(image_generation, "generate", lambda request: (png, {})):
            status, _, raw = self.request("POST", "/api/ext/designer/image/stream", body)
        self.assertEqual(status, 200)
        self.assertIn(b"image_url", raw)
        self.assertAlmostEqual(payments.balance(), 100 - payments.image_price())
        with commander.database() as db:
            notes = [row[0] for row in db.execute(
                "SELECT note FROM pay_bills WHERE kind='consume'").fetchall()]
        self.assertTrue(any(note.startswith("生图消费（已完成）：") for note in notes))
        with mock.patch.object(
            image_generation, "generate",
            mock.Mock(side_effect=commander.ChatError("生图失败")),
        ):
            status, _, raw = self.request("POST", "/api/ext/designer/image/stream", body)
        self.assertEqual(status, 200)
        self.assertIn("生图失败", raw.decode("utf-8"))
        self.assertAlmostEqual(payments.balance(), 100 - payments.image_price())

    def test_write_routes_require_token(self):
        status, _, _ = self.request("POST", "/api/card/exchange", {"code": "X"}, token=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/geo/projects", {}, token=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("PUT", "/api/geo/projects/1", {"brand_name": "x"}, token=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("DELETE", "/api/geo/projects/1", token=False)
        self.assertEqual(status, 403)

    def test_delete_with_non_json_body_is_rejected(self):
        status, _, data = self.request("DELETE", "/api/geo/projects/1", b"x",
                                       content_type="text/plain")
        self.assertEqual(status, 400)
        self.assertIn("msg", data)

    def test_unrouted_write_api_uses_preview_fallback(self):
        status, _, data = self.request("POST", "/agent-api/api/desktop/agents", {})
        self.assertEqual(status, 503)
        self.assertIn("detail", data)

    def test_oversized_json_body_is_rejected(self):
        status, _ = self.raw_request("POST", "/api/card/exchange", {
            "Authorization": "Bearer " + commander.LOCAL_TOKEN,
            "Content-Type": "application/json",
            "Content-Length": "160001",
        })
        self.assertEqual(status, 400)

    def test_oversized_upload_is_rejected(self):
        status, _ = self.raw_request("POST", "/api/common/upload", {
            "Authorization": "Bearer " + commander.LOCAL_TOKEN,
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(materials.MAX_FILE + 64 * 1024 + 1),
        })
        self.assertEqual(status, 413)

    def test_invalid_content_length_is_rejected(self):
        for value in ("abc", "-1"):
            with self.subTest(value=value):
                status, _ = self.raw_request("POST", "/api/card/exchange", {
                    "Authorization": "Bearer " + commander.LOCAL_TOKEN,
                    "Content-Type": "application/json",
                    "Content-Length": value,
                })
                self.assertEqual(status, 400)

    def test_head_requests_have_no_body(self):
        status, content_type, data = self.request("HEAD", "/mb/home")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertEqual(data, b"")
        status, _, data = self.request("HEAD", "/api/pay/power-products")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"")
        status, _, data = self.request("HEAD", "/api/pay/power-products", token=False)
        self.assertEqual(status, 403)
        self.assertEqual(data, b"")

    def test_invalid_body_is_rejected(self):
        status, _, data = self.request("POST", "/api/card/exchange", b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("msg", data)
        status, _, _ = self.request("POST", "/api/card/exchange", {"code": "X"},
                                    content_type="text/plain")
        self.assertEqual(status, 400)

    def test_empty_post_body_is_rejected(self):
        # Only DELETE may omit its body; an empty POST must not reach writers
        # such as memory/clear or acquisition/business-profile.
        for path in ("/agent-api/api/memory/clear",
                     "/api/acquisition/business-profile",
                     "/api/pay/recharge"):
            with self.subTest(path=path):
                status, _, data = self.request("POST", path)
                self.assertEqual(status, 400)
                self.assertIn("msg", data)

    def test_empty_profile_does_not_overwrite(self):
        saved = {"main_products": "原产品", "target_customer": "原客户", "exclude_scope": "原范围"}
        status, _, data = self.request("POST", "/api/acquisition/business-profile", saved)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        for body in ({}, {"main_products": "", "target_customer": "", "exclude_scope": ""},
                     {"target_customer": "新客户"}):
            with self.subTest(body=body):
                status, _, data = self.request("POST", "/api/acquisition/business-profile", body)
                self.assertEqual(status, 400)
                self.assertIn("msg", data)
                status, _, data = self.request("GET", "/api/acquisition/business-profile")
                self.assertEqual(status, 200)
                self.assertEqual(data["main_products"], "原产品")
                self.assertEqual(data["target_customer"], "原客户")
                self.assertEqual(data["exclude_scope"], "原范围")

    def test_unknown_api_uses_preview_fixture(self):
        status, _, data = self.request("GET", "/agent-api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_customer_import_template_route(self):
        status, content_type, data = self.request(
            "GET", "/scrm-api/api/scrm/contacts/import/template", token=True,
        )
        self.assertEqual(status, 200)
        self.assertIn("spreadsheetml", content_type)
        self.assertIsInstance(data, bytes)
        self.assertTrue(data)

    def test_frontend_fallback_serves_index(self):
        status, content_type, data = self.request("GET", "/mb/home")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"<div id=\"app\">", data)

    def test_unknown_media_path_is_not_found(self):
        status, _, _ = self.request("GET", "/material-media/" + "a" * 32)
        self.assertEqual(status, 404)

    def test_whatsapp_routes_and_webhook(self):
        account = {
            "account_name": "路由 WhatsApp",
            "phone_number_id": "555000111",
            "verify_token": "route-verify",
            "access_token": "",
            "app_secret": "",
        }
        status, _, data = self.request("POST", "/scrm-api/api/scrm/accounts", account)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["data"]["configured"])
        account_id = data["data"]["id"]

        status, _, data = self.request("GET", "/scrm-api/api/scrm/accounts")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["data"]), 1)

        status, _, data = self.request("GET", "/scrm-api/api/scrm/onboarding")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["channel"], "whatsapp")

        status, _, data = self.request("POST", "/scrm-api/api/scrm/roles",
                                       {"scene": "sales", "persona_name": "路由人设",
                                        "kb_ids": []})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, _, data = self.request("GET", "/scrm-api/api/scrm/roles")
        self.assertEqual(status, 200)
        self.assertEqual(data["data"][0]["persona_name"], "路由人设")

        status, _, data = self.request("GET", "/scrm-api/api/scrm/inbox/threads")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 0)

        status, _, data = self.request(
            "POST", f"/scrm-api/api/scrm/accounts/{account_id}/probe")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])

        status, _, data = self.request(
            "POST", "/scrm-api/api/scrm/inbox/threads/999/send", {"content": "你好"})
        self.assertEqual(status, 400)
        self.assertIn("msg", data)

        status, _, _ = self.request("GET", "/scrm-api/api/scrm/accounts", token=False)
        self.assertEqual(status, 403)

        status, content_type, body = self.request(
            "GET", "/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=route-verify"
                   "&hub.challenge=abc123", token=False)
        self.assertEqual(status, 200)
        self.assertIn("text/plain", content_type)
        self.assertEqual(body, b"abc123")
        status, _, _ = self.request(
            "GET", "/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=wrong"
                   "&hub.challenge=abc123", token=False)
        self.assertEqual(status, 403)

    def test_admin_page_and_api_routes(self):
        status, _, data = self.request("GET", "/api/admin/me")
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["role"], "admin")

        status, content_type, body = self.request("GET", "/admin-test")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("管理后台".encode("utf-8"), body)

        status, _, data = self.request("POST", "/api/admin/tenants",
                                       {"name": "路由租户", "max_users": 3})
        self.assertEqual(status, 200)
        tenant = data["data"]
        status, _, data = self.request("POST", "/api/admin/users", {
            "tenant_id": tenant["id"], "username": "route_admin_user",
            "password": TEST_PASSWORD, "role": "member",
        })
        self.assertEqual(status, 200)
        self.assertNotIn("password", data["data"])
        self.assertNotIn("salt", data["data"])

        status, _, data = self.request("GET", f"/api/admin/users?tenant={tenant['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["data"]), 1)
        status, _, data = self.request("GET", "/api/admin/audit")
        self.assertEqual(status, 200)
        self.assertTrue(data["data"])
        status, _, _ = self.request("GET", "/api/admin/overview", token=False)
        self.assertEqual(status, 403)

    def test_admin_switches_tenant_with_header(self):
        tenant = tenancy.create_tenant("路由租户")
        with tenancy.scope(tenant):
            customers.write("POST", customers.CRM,
                            {"name": "租户专属客户", "phone": "13800000009"})
        status, _, data = self.request("GET", "/scrm-api/api/scrm/contacts")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 0)
        status, _, data = self.request("GET", "/scrm-api/api/scrm/contacts",
                                       headers={"X-Tenant": tenant})
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["data"][0]["name"], "租户专属客户")


if __name__ == "__main__":
    unittest.main()
