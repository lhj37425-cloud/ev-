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
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import payments
import serve


class RouteServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old_data_dir = commander.DATA_DIR
        cls.old_mock = os.environ.get("EV_PAY_MOCK")
        os.environ["EV_PAY_MOCK"] = "0"
        commander.DATA_DIR = Path(cls.temp.name) / "server"
        commander.DATA_DIR.mkdir()
        cls.server = serve.PreviewServer(("127.0.0.1", 0), serve.FrontendHandler)
        cls.server.invitation = None
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
        self.case = tempfile.TemporaryDirectory()
        commander.DATA_DIR = Path(self.case.name)

    def tearDown(self):
        commander.DATA_DIR = Path(self.temp.name) / "server"
        self.case.cleanup()

    def request(self, method, path, body=None, token=True, content_type="application/json"):
        headers = {"Connection": "close"}
        payload = None
        if body is not None:
            payload = (json.dumps(body, ensure_ascii=False).encode("utf-8")
                       if isinstance(body, (dict, list)) else body)
            headers["Content-Type"] = content_type
        if token:
            headers["Authorization"] = "Bearer " + commander.LOCAL_TOKEN
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            response_type = response.getheader("Content-Type", "")
            data = json.loads(raw.decode("utf-8")) if raw and "json" in response_type else raw
            return response.status, response_type, data
        finally:
            connection.close()

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
        status, _, data = self.request("DELETE", f"/api/geo/projects/{project_id}")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, _, data = self.request("GET", "/api/geo/projects")
        self.assertEqual(data["total"], 0)

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

    def test_write_routes_require_token(self):
        status, _, _ = self.request("POST", "/api/card/exchange", {"code": "X"}, token=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/geo/projects", {}, token=False)
        self.assertEqual(status, 403)

    def test_invalid_body_is_rejected(self):
        status, _, data = self.request("POST", "/api/card/exchange", b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("msg", data)
        status, _, _ = self.request("POST", "/api/card/exchange", {"code": "X"},
                                    content_type="text/plain")
        self.assertEqual(status, 400)

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


if __name__ == "__main__":
    unittest.main()
