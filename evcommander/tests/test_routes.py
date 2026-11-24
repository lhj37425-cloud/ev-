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
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import customers
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
        self.assertEqual(data["credits"], 0)

        status, _, data = self.request("POST", f"{base}/layers/entity/content")
        self.assertEqual(status, 400)
        self.assertIn("暂未开放", data["detail"])

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
        self.assertIn("暂未开放", data["detail"])

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
