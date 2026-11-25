"""GEO publishing channels: WordPress and custom webhook."""

import base64
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
import geo_channel
import geo_content
import tenancy


PROJECT = {
    "brand_name": "云杉科技", "aliases": ["云杉"], "website": "https://yunshan.example",
    "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
    "market": "cn", "locales": [],
}
BODY = ("# 企业软件哪家好\n\n云杉科技成立于 2016 年，总部在深圳。\n\n"
        "## 选择要点\n\n- 对比协作能力\n")
ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN",
            "EV_CHANNEL_ALLOW_PRIVATE")


class ChannelBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["EV_PAY_MOCK"] = "0"
        os.environ.pop("EV_CHANNEL_ALLOW_PRIVATE", None)
        for key in ("EV_SEED_DEMO", "EV_TRIAL_ORIGIN"):
            os.environ.pop(key, None)
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
        self.temp.cleanup()

    def channel(self, kind="wordpress", name="品牌官网", **extra):
        config = {"base_url": "https://wp.example", "username": "admin",
                  "app_password": "app-pass", "status": "publish"}
        if kind == "webhook":
            config = {"url": "https://site.example/hook", "token": "hook-token"}
        config.update(extra.pop("config", {}))
        body = {"kind": kind, "name": name, "config": config}
        body.update(extra)
        return geo.write("POST", "/api/geo/channel-configs", body)["channel"]

    def content(self, approved=True, checked=True):
        content = geo.write("POST", self.path("contents"), {
            "question": "企业软件哪家好？", "genre": "qa_answer",
        })["content"]
        geo.write("PUT", f"/api/geo/contents/{content['id']}",
                  {"title": "企业软件怎么选", "body": BODY})
        if checked:
            result = geo.write("POST", f"/api/geo/contents/{content['id']}/check", {})["content"]
            if approved and result["gate"]["decision"] not in {"pass", "approved"}:
                geo.write("POST", f"/api/geo/contents/{content['id']}/approve",
                          {"reason": "验收放行"})
        return content["id"]


class ChannelConfigTests(ChannelBase):
    def test_providers_and_crud_hide_secrets(self):
        kinds = {item["kind"]: item for item in geo_channel.providers()}
        self.assertEqual(set(kinds), {"wordpress", "webhook"})
        self.assertTrue(any(field.get("secret") for field in kinds["wordpress"]["fields"]))

        channel = self.channel()
        self.assertEqual(channel["kind"], "wordpress")
        self.assertEqual(channel["config"]["app_password"], "")
        listing = geo.read("/api/geo/channel-configs", {})
        self.assertEqual(len(listing["items"]), 1)
        self.assertNotIn("app-pass", json.dumps(listing))
        with commander.database() as db:
            row = db.execute("SELECT config FROM geo_channel_configs").fetchone()
        self.assertIn("app-pass", row["config"])

        updated = geo.write("PUT", f"/api/geo/channel-configs/{channel['id']}",
                            {"name": "主站", "config": {
                                "base_url": "https://wp.example", "username": "admin",
                                "app_password": "", "status": "draft",
                            }})["channel"]
        self.assertEqual(updated["name"], "主站")
        self.assertEqual(updated["config"]["status"], "draft")
        with commander.database() as db:
            row = db.execute("SELECT config FROM geo_channel_configs").fetchone()
        self.assertIn("app-pass", row["config"])
        toggled = geo.write("PUT", f"/api/geo/channel-configs/{channel['id']}",
                            {"status": 0})["channel"]
        self.assertEqual(toggled["status"], 0)
        geo.write("DELETE", f"/api/geo/channel-configs/{channel['id']}", {})
        self.assertEqual(geo.read("/api/geo/channel-configs", {})["items"], [])

    def test_invalid_configs_are_rejected(self):
        with self.assertRaises(commander.ChatError):
            geo.write("POST", "/api/geo/channel-configs",
                      {"kind": "unknown", "name": "x", "config": {}})
        with self.assertRaises(commander.ChatError):
            geo.write("POST", "/api/geo/channel-configs",
                      {"kind": "wordpress", "name": "x", "config": {}})
        with self.assertRaises(commander.ChatError):
            self.channel(config={"status": "bad"})

    def test_connection_tests(self):
        channel = self.channel()
        with mock.patch.object(geo_channel, "_request",
                               return_value=(200, '{"id": 1, "name": "admin"}', "u")):
            result = geo.write("POST", f"/api/geo/channel-configs/{channel['id']}/test", {})
        self.assertTrue(result["ok_test"])
        self.assertIn("成功", result["message"])
        self.assertTrue(result["channel"]["last_test"])
        self.assertFalse(result["channel"]["last_error"])

        webhook = self.channel(kind="webhook", name="自建站")
        with mock.patch.object(geo_channel, "_request",
                               return_value=(401, "bad token", "u")):
            result = geo.write("POST", f"/api/geo/channel-configs/{webhook['id']}/test", {})
        self.assertFalse(result["ok_test"])
        self.assertIn("401", result["message"])

    def test_endpoint_private_protection(self):
        with self.assertRaises(commander.ChatError):
            geo_channel._endpoint("http://127.0.0.1:8000/x")
        with mock.patch.dict(os.environ, {"EV_CHANNEL_ALLOW_PRIVATE": "1"}):
            self.assertEqual(geo_channel._endpoint("http://127.0.0.1:8000/x"),
                             "http://127.0.0.1:8000/x")

    def test_empty_post_whitelist(self):
        self.assertTrue(geo.empty_post("/api/geo/channel-configs/1/test"))
        self.assertFalse(geo.empty_post("/api/geo/channel-configs/1"))
        self.assertFalse(geo.empty_post("/api/geo/contents/1/push"))


class ChannelPushTests(ChannelBase):
    def test_wordpress_push_publishes(self):
        channel = self.channel()
        content_id = self.content()
        seen = {}

        def request(method, url, payload=None, headers=None):
            seen.update({"method": method, "url": url, "payload": payload,
                         "headers": headers or {}})
            return 201, '{"id": 5, "link": "https://wp.example/post/5"}', url

        with mock.patch.object(geo_channel, "_request", side_effect=request):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": channel["id"], "cover_url": ""})
        self.assertTrue(result["ok_push"])
        self.assertEqual(seen["payload"]["status"], "publish")
        self.assertEqual(seen["payload"]["title"], "企业软件怎么选")
        self.assertEqual(seen["headers"]["Authorization"],
                         "Basic " + base64.b64encode(b"admin:app-pass").decode())
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["channel_kind"], "channel")
        self.assertEqual(publication["platform"], "wordpress")
        self.assertEqual(publication["url"], "https://wp.example/post/5")
        self.assertEqual(publication["status"], "published")
        item = geo.read(f"/api/geo/contents/{content_id}", {})["content"]
        self.assertEqual(item["status"], "published")

    def test_wordpress_draft_keeps_content_editable(self):
        channel = self.channel(config={"status": "draft"})
        content_id = self.content()
        with mock.patch.object(geo_channel, "_request",
                               return_value=(201, '{"id": 6, "link": "https://wp.example/?p=6"}', "u")):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": channel["id"]})
        self.assertTrue(result["ok_push"])
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "draft_saved")
        item = geo.read(f"/api/geo/contents/{content_id}", {})["content"]
        self.assertNotEqual(item["status"], "published")

    def test_webhook_push_with_and_without_url(self):
        channel = self.channel(kind="webhook", name="自建站")
        content_id = self.content()
        seen = {}

        def request(method, url, payload=None, headers=None):
            seen.update({"payload": payload, "headers": headers or {}})
            return 200, '{"url": "https://site.example/post/1"}', url

        with mock.patch.object(geo_channel, "_request", side_effect=request):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": channel["id"]})
        self.assertTrue(result["ok_push"])
        self.assertEqual(seen["headers"]["Authorization"], "Bearer hook-token")
        self.assertEqual(seen["payload"]["title"], "企业软件怎么选")
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "published")
        self.assertEqual(publication["url"], "https://site.example/post/1")

        content_id = self.content()
        with mock.patch.object(geo_channel, "_request", return_value=(200, "{}", "u")):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": channel["id"]})
        self.assertTrue(result["ok_push"])
        latest = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(latest["status"], "draft_saved")

    def test_push_failure_and_validation(self):
        channel = self.channel()
        content_id = self.content()
        with mock.patch.object(geo_channel, "_request",
                               return_value=(500, '{"message": "boom"}', "u")):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": channel["id"]})
        self.assertFalse(result["ok_push"])
        self.assertIn("500", result["message"])
        listing = geo.read("/api/geo/channel-configs", {})["items"]
        self.assertIn("500", listing[0]["last_error"])
        self.assertEqual(geo.read(self.path("publications"), {})["items"], [])

        unapproved = self.content(checked=False)
        with self.assertRaises(commander.ChatError):
            geo.write("POST", f"/api/geo/contents/{unapproved}/push",
                      {"channel_id": channel["id"]})
        with self.assertRaises(commander.ChatError):
            geo.write("POST", f"/api/geo/contents/{content_id}/push", {"channel_id": 9999})
        disabled = self.channel(name="停用站")
        geo.write("PUT", f"/api/geo/channel-configs/{disabled['id']}", {"status": 0})
        with self.assertRaises(commander.ChatError):
            geo.write("POST", f"/api/geo/contents/{content_id}/push",
                      {"channel_id": disabled["id"]})

    def test_channels_are_tenant_scoped(self):
        channel = self.channel()
        other = tenancy.create_tenant("另一个客户")
        tenancy.activate({"tenant_id": other, "user_id": "u", "username": "u", "role": "member"})
        self.assertEqual(geo.read("/api/geo/channel-configs", {})["items"], [])
        with self.assertRaises(commander.ChatError):
            geo.write("POST", f"/api/geo/contents/1/push", {"channel_id": channel["id"]})
