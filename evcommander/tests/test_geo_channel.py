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

from mock_http import json_server

import commander
import geo
import geo_channel
import geo_content
import materials
import tenancy


PROJECT = {
    "brand_name": "云杉科技", "aliases": ["云杉"], "website": "https://yunshan.example",
    "industry": "企业软件", "region": "深圳", "description": "做企业协作软件",
    "market": "cn", "locales": [],
}
BODY = ("# 企业软件哪家好\n\n云杉科技成立于 2016 年，总部在深圳。\n\n"
        "## 选择要点\n\n- 对比协作能力\n")
ENV_KEYS = ("EV_PAY_MOCK", "EV_SEED_DEMO", "EV_TRIAL_ORIGIN",
            "EV_CHANNEL_ALLOW_PRIVATE", "EV_CHANNEL_SYNC_PUSH", "EV_CHANNEL_PUSH_WAIT")


class ChannelBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        self.old_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["EV_PAY_MOCK"] = "0"
        os.environ["EV_CHANNEL_SYNC_PUSH"] = "1"
        os.environ["EV_CHANNEL_PUSH_WAIT"] = "0"
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
        if kind == "shopify":
            config = {"shop": "https://shop.example", "token": "shpat-test",
                      "status": "publish", "blog": ""}
        if kind == "webflow":
            config = {"token": "wf-test", "collection": "col-1",
                      "title_field": "name", "body_field": "post-body",
                      "slug_field": "slug", "status": "draft"}
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
        self.assertEqual(set(kinds), {"wordpress", "webhook", "shopify", "webflow"})
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
                               return_value=(403, '{"message": "forbidden"}', "u")):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": channel["id"]})
        self.assertFalse(result["ok_push"])
        self.assertIn("403", result["message"])
        listing = geo.read("/api/geo/channel-configs", {})["items"]
        self.assertIn("403", listing[0]["last_error"])
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "failed")
        self.assertIn("403", publication["error"])

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


class ChannelQueueTests(ChannelBase):
    def test_push_is_queued_then_worker_publishes(self):
        channel = self.channel()
        content_id = self.content()
        os.environ.pop("EV_CHANNEL_SYNC_PUSH", None)
        with mock.patch.object(geo_channel, "_ensure_push_worker"):
            with mock.patch.object(geo_channel, "_request",
                                   return_value=(201, '{"id": 7, "link": "https://wp.example/?p=7"}', "u")):
                result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                                   {"channel_id": channel["id"]})
                self.assertTrue(result["ok_push"])
                self.assertTrue(result["queued"])
                self.assertIn("队列", result["message"])
                publication = geo.read(self.path("publications"), {})["items"][0]
                self.assertEqual(publication["status"], "queued")
                self.assertEqual(publication["remote_id"], "")
                self.assertEqual(geo_channel.flush_push_jobs(), 1)
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "published")
        self.assertEqual(publication["url"], "https://wp.example/?p=7")
        self.assertEqual(publication["remote_id"], "7")
        item = geo.read(f"/api/geo/contents/{content_id}", {})["content"]
        self.assertEqual(item["status"], "published")
        with commander.database() as db:
            job = db.execute("SELECT * FROM geo_push_jobs").fetchone()
        self.assertEqual(job["status"], "sent")
        self.assertEqual(job["attempts"], 1)

    def test_duplicate_push_reuses_queued_job(self):
        channel = self.channel()
        content_id = self.content()
        os.environ.pop("EV_CHANNEL_SYNC_PUSH", None)
        with mock.patch.object(geo_channel, "_ensure_push_worker"):
            first = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": channel["id"]})
            second = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                               {"channel_id": channel["id"]})
        self.assertEqual(first["publication_id"], second["publication_id"])
        self.assertIn("队列", second["message"])
        self.assertEqual(len(geo.read(self.path("publications"), {})["items"]), 1)
        with mock.patch.object(geo_channel, "_request",
                               return_value=(201, '{"id": 8, "link": "https://wp.example/?p=8"}', "u")):
            geo_channel.flush_push_jobs()
        self.assertEqual(len(geo.read(self.path("publications"), {})["items"]), 1)

    def test_retryable_failures_are_retried_three_times(self):
        channel = self.channel()
        content_id = self.content()
        with mock.patch.object(geo_channel, "_ensure_push_worker"):
            with mock.patch.object(geo_channel, "_request",
                                   return_value=(500, '{"message": "boom"}', "u")):
                result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                                   {"channel_id": channel["id"]})
                self.assertTrue(result["queued"])
                self.assertIn("重试", result["message"])
                with commander.database() as db:
                    job = db.execute("SELECT * FROM geo_push_jobs").fetchone()
                self.assertEqual(job["attempts"], 1)
                self.assertTrue(job["next_at"])
                for _ in range(2):
                    with commander.database() as db:
                        db.execute("UPDATE geo_push_jobs SET next_at=''")
                    geo_channel.flush_push_jobs()
                with commander.database() as db:
                    job = db.execute("SELECT * FROM geo_push_jobs").fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["attempts"], 3)
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "failed")
        self.assertIn("500", publication["error"])

    def test_retry_succeeds_after_transient_failure(self):
        channel = self.channel()
        content_id = self.content()
        answers = [(500, '{"message": "boom"}', "u"),
                   (201, '{"id": 9, "link": "https://wp.example/?p=9"}', "u")]
        with mock.patch.object(geo_channel, "_ensure_push_worker"):
            with mock.patch.object(geo_channel, "_request", side_effect=answers):
                result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                                   {"channel_id": channel["id"]})
                self.assertTrue(result["queued"])
                with commander.database() as db:
                    db.execute("UPDATE geo_push_jobs SET next_at=''")
                geo_channel.flush_push_jobs()
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "published")
        self.assertEqual(publication["url"], "https://wp.example/?p=9")

    def test_disabled_channel_fails_queued_job(self):
        channel = self.channel()
        content_id = self.content()
        os.environ.pop("EV_CHANNEL_SYNC_PUSH", None)
        with mock.patch.object(geo_channel, "_ensure_push_worker"):
            geo.write("POST", f"/api/geo/contents/{content_id}/push",
                      {"channel_id": channel["id"]})
        geo.write("PUT", f"/api/geo/channel-configs/{channel['id']}", {"status": 0})
        geo_channel.flush_push_jobs()
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "failed")
        self.assertIn("停用", publication["error"])

    def test_recover_requeues_sending_jobs(self):
        with commander.database() as db:
            geo_channel.initialize(db)
            db.execute(
                "INSERT INTO geo_push_jobs (tenant_id,publication_id,content_id,channel_id,"
                "cover_url,status,attempts,next_at,last_error,created_at,updated_at) "
                "VALUES (?,1,1,1,'','sending',0,'','','x','x')", (commander.tenant(),))
        with mock.patch.object(geo_channel, "_ensure_push_worker") as worker:
            geo_channel.recover_push_jobs()
        worker.assert_called_once()
        with commander.database() as db:
            job = db.execute("SELECT * FROM geo_push_jobs").fetchone()
        self.assertEqual(job["status"], "pending")


class ChannelCoverTests(ChannelBase):
    def upload_image(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        body = (b"--X\r\nContent-Disposition: form-data; name=\"file\"; filename=\"cover.png\"\r\n"
                b"Content-Type: image/png\r\n\r\n" + raw + b"\r\n--X--\r\n")
        return materials.upload("multipart/form-data; boundary=X", body)["url"]

    def test_wordpress_cover_is_uploaded_and_featured(self):
        channel = self.channel()
        content_id = self.content()
        cover = "http://127.0.0.1:8766" + self.upload_image()
        seen = []

        def request(method, url, payload=None, headers=None, body=None, content_type=None):
            seen.append({"method": method, "url": url, "payload": payload,
                         "headers": headers or {}, "body": body,
                         "content_type": content_type})
            if "/wp-json/wp/v2/media" in url:
                return 201, '{"id": 11}', url
            return 201, '{"id": 5, "link": "https://wp.example/post/5"}', url

        with mock.patch.object(geo_channel, "_request", side_effect=request):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                               {"channel_id": channel["id"], "cover_url": cover})
        self.assertTrue(result["ok_push"])
        media = next(item for item in seen if "/wp-json/wp/v2/media" in item["url"])
        self.assertIn("multipart/form-data", media["content_type"])
        self.assertIn(b'name="file"', media["body"])
        self.assertIn(b"cover.png", media["body"])
        self.assertEqual(media["headers"]["Authorization"],
                         "Basic " + base64.b64encode(b"admin:app-pass").decode())
        post = next(item for item in seen if item["url"].endswith("/wp-json/wp/v2/posts"))
        self.assertEqual(post["payload"]["featured_media"], 11)

    def test_invalid_cover_is_rejected_before_queueing(self):
        channel = self.channel()
        content_id = self.content()
        with self.assertRaises(commander.ChatError):
            geo.write("POST", f"/api/geo/contents/{content_id}/push",
                      {"channel_id": channel["id"],
                       "cover_url": "https://evil.example/cover.png"})
        self.assertEqual(geo.read(self.path("publications"), {})["items"], [])
        with self.assertRaises(commander.ChatError):
            geo.write("POST", f"/api/geo/contents/{content_id}/push",
                      {"channel_id": channel["id"],
                       "cover_url": "http://127.0.0.1:8766/material-media/nope"})

    def test_missing_cover_file_fails_push(self):
        channel = self.channel()
        content_id = self.content()
        missing = "http://127.0.0.1:8766/material-media/" + "a" * 32
        with mock.patch.object(geo_channel, "_request") as request:
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                               {"channel_id": channel["id"], "cover_url": missing})
        self.assertFalse(result["ok_push"])
        self.assertIn("封面图", result["message"])
        request.assert_not_called()
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "failed")

    def test_webhook_cover_is_passed_through(self):
        channel = self.channel(kind="webhook", name="自建站")
        content_id = self.content()
        cover = "http://127.0.0.1:8766/material-media/" + "b" * 32
        seen = {}

        def request(method, url, payload=None, headers=None, body=None, content_type=None):
            seen.update({"payload": payload})
            return 200, '{"url": "https://site.example/post/2"}', url

        with mock.patch.object(geo_channel, "_request", side_effect=request):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                               {"channel_id": channel["id"], "cover_url": cover})
        self.assertTrue(result["ok_push"])
        self.assertEqual(seen["payload"]["cover_url"], cover)


class ChannelTaxonomyTests(ChannelBase):
    def test_wordpress_categories_and_tags(self):
        channel = self.channel(config={"categories": "技术，案例", "tags": "SaaS"})
        content_id = self.content()
        calls = []

        def request(method, url, payload=None, headers=None, body=None, content_type=None):
            calls.append((method, url, payload))
            if method == "GET" and "/categories" in url:
                if "search=" + geo_channel.quote("技术") in url:
                    return 200, '[{"id": 3, "name": "技术"}]', url
                if "search=" + geo_channel.quote("案例") in url:
                    return 200, "[]", url
                return 200, "[]", url
            if method == "POST" and "/categories" in url:
                return 201, '{"id": 9, "name": "案例"}', url
            if method == "GET" and "/tags" in url:
                return 200, "[]", url
            if method == "POST" and "/tags" in url:
                return 201, '{"id": 5, "name": "SaaS"}', url
            return 201, '{"id": 7, "link": "https://wp.example/?p=7"}', url

        with mock.patch.object(geo_channel, "_request", side_effect=request):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                               {"channel_id": channel["id"]})
        self.assertTrue(result["ok_push"])
        post = next(item for item in calls if item[1].endswith("/wp-json/wp/v2/posts"))
        self.assertEqual(post[2]["categories"], [3, 9])
        self.assertEqual(post[2]["tags"], [5])
        created = [item for item in calls if item[0] == "POST" and "/categories" in item[1]]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][2], {"name": "案例"})

    def test_taxonomy_failure_fails_push(self):
        channel = self.channel(config={"categories": "技术"})
        content_id = self.content()
        with mock.patch.object(geo_channel, "_request",
                               return_value=(403, '{"message": "forbidden"}', "u")):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                               {"channel_id": channel["id"]})
        self.assertFalse(result["ok_push"])
        self.assertIn("403", result["message"])
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "failed")

    def test_terms_are_parsed_and_deduplicated(self):
        self.assertEqual(geo_channel._term_names(" 技术, 案例，技术 ,,"),
                         ["技术", "案例"])
        self.assertEqual(geo_channel._term_names(""), [])


class ChannelOfflineTests(ChannelBase):
    def publish(self):
        channel = self.channel()
        content_id = self.content()
        with mock.patch.object(geo_channel, "_request",
                               return_value=(201, '{"id": 5, "link": "https://wp.example/post/5"}', "u")):
            geo.write("POST", f"/api/geo/contents/{content_id}/push",
                      {"channel_id": channel["id"]})
        return channel, content_id, geo.read(self.path("publications"), {})["items"][0]

    def test_wordpress_publication_offline(self):
        channel, content_id, publication = self.publish()
        seen = {}

        def request(method, url, payload=None, headers=None, body=None, content_type=None):
            seen.update({"method": method, "url": url})
            return 200, '{"id": 5, "status": "trash"}', url

        with mock.patch.object(geo_channel, "_request", side_effect=request):
            result = geo.write("DELETE", f"/api/geo/publications/{publication['id']}", {})
        self.assertTrue(result["ok"])
        self.assertTrue(result["remote"])
        self.assertEqual(seen["method"], "DELETE")
        self.assertTrue(seen["url"].endswith("/wp-json/wp/v2/posts/5"))
        item = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(item["status"], "offline")
        self.assertEqual(item["status_name"], "已下线")
        content = geo.read(f"/api/geo/contents/{content_id}", {})["content"]
        self.assertEqual(content["status"], "ready")
        self.assertTrue(content["can_publish"])

    def test_offline_cancels_queued_push(self):
        channel = self.channel()
        content_id = self.content()
        os.environ.pop("EV_CHANNEL_SYNC_PUSH", None)
        with mock.patch.object(geo_channel, "_ensure_push_worker"):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                               {"channel_id": channel["id"]})
        with mock.patch.object(geo_channel, "_request") as request:
            result = geo.write("DELETE",
                               f"/api/geo/publications/{result['publication_id']}", {})
        self.assertTrue(result["ok"])
        request.assert_not_called()
        with commander.database() as db:
            job = db.execute("SELECT * FROM geo_push_jobs").fetchone()
        self.assertEqual(job["status"], "cancelled")
        item = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(item["status"], "offline")
        content = geo.read(f"/api/geo/contents/{content_id}", {})["content"]
        self.assertEqual(content["status"], "ready")

    def test_offline_rejects_manual_records(self):
        with commander.database() as db:
            geo_content.initialize(db)
            stamp = "2026-11-26T00:00:00+00:00"
            publication_id = db.execute(
                "INSERT INTO geo_publications (tenant_id,project_id,content_id,ticket_id,"
                "channel_kind,platform,platform_name,title,status,url,created_at,updated_at) "
                "VALUES (?,?,?,0,'community','zhihu','知乎','手工','published',"
                "'https://zhuanlan.zhihu.com/p/1',?,?)",
                (commander.tenant(), self.project_id, 1, stamp, stamp)).lastrowid
        with self.assertRaises(commander.ChatError):
            geo.write("DELETE", f"/api/geo/publications/{publication_id}", {})

    def test_offline_is_tenant_scoped(self):
        channel, content_id, publication = self.publish()
        other = tenancy.create_tenant("另一个客户")
        tenancy.activate({"tenant_id": other, "user_id": "u", "username": "u", "role": "member"})
        with self.assertRaises(commander.ChatError):
            geo.write("DELETE", f"/api/geo/publications/{publication['id']}", {})


class ShopifyWebflowTests(ChannelBase):
    def test_providers_include_shopify_and_webflow(self):
        kinds = {item["kind"]: item for item in geo_channel.providers()}
        self.assertEqual(set(kinds), {"wordpress", "webhook", "shopify", "webflow"})
        self.assertTrue(any(field.get("secret") for field in kinds["shopify"]["fields"]))
        self.assertTrue(any(field.get("secret") for field in kinds["webflow"]["fields"]))


    def test_publish_all_channels_over_loopback_http(self):
        responses = {
            "/wp-json/wp/v2/posts": (201, {"id": 41, "link": "https://wp.example/post/41"}),
            "/hook": (200, {"id": 42, "url": "https://site.example/post/42"}),
            "/admin/api/2024-10/blogs/77/articles.json": (201, {"article": {"id": 43}}),
            "/v2/collections/col-1/items": (201, {"id": "44"}),
            "/v2/collections/col-1/items/44/publish": (200, {"publishedItemIds": ["44"]}),
            "/denied": (403, {"error": "denied"}),
        }
        seen = []

        def respond(method, path, headers, payload):
            seen.append((method, path, headers, payload))
            return responses.get(path, (404, {}))

        with json_server(respond) as base, mock.patch.dict(os.environ, {
            "EV_CHANNEL_ALLOW_PRIVATE": "1", "EV_WEBFLOW_BASE": base,
            "EV_SHOPIFY_API_VERSION": "2024-10",
        }):
            configs = [
                ("wordpress", {"base_url": base}, "41"),
                ("webhook", {"url": base + "/hook"}, "42"),
                ("shopify", {"shop": base, "blog": "77"}, "43"),
                ("webflow", {"status": "publish"}, "44"),
            ]
            for kind, config, remote_id in configs:
                with self.subTest(kind=kind):
                    channel = self.channel(kind, config=config)
                    content_id = self.content()
                    result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                                       {"channel_id": channel["id"]})
                    self.assertTrue(result["ok_push"], result)
                    records = geo.read(self.path("publications"), {})["items"]
                    publication = next(record for record in records if record["remote_id"] == remote_id)
                    self.assertEqual(publication["status"], "published")
            denied = self.channel("webhook", name="拒绝发布", config={"url": base + "/denied"})
            result = geo.write("POST", f"/api/geo/contents/{self.content()}/push",
                               {"channel_id": denied["id"]})
            self.assertFalse(result["ok_push"], result)
        self.assertEqual({path for method, path, _, _ in seen if method == "POST"}, set(responses))
        hook = next(entry for entry in seen if entry[1] == "/hook")
        self.assertEqual(hook[2]["Authorization"], "Bearer hook-token")
        self.assertEqual(hook[3]["title"], "企业软件怎么选")

    def test_shopify_test_and_push(self):
        channel = self.channel(kind="shopify")
        self.assertEqual(channel["config"]["token"], "")
        with mock.patch.object(geo_channel, "_request",
                               return_value=(200, '{"shop": {"id": 1, "name": "云杉店"}}', "u")):
            result = geo.write("POST", f"/api/geo/channel-configs/{channel['id']}/test", {})
        self.assertTrue(result["ok_test"])
        content_id = self.content()
        seen = []

        def request(method, url, payload=None, headers=None, body=None, content_type=None):
            seen.append({"method": method, "url": url, "payload": payload,
                         "headers": headers or {}})
            if url.endswith("/blogs.json"):
                return 200, '{"blogs": [{"id": 77, "handle": "news"}]}', url
            return 201, '{"article": {"id": 555, "handle": "hello"}}', url

        with mock.patch.object(geo_channel, "_request", side_effect=request):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": channel["id"]})
        self.assertTrue(result["ok_push"])
        article = next(item for item in seen if item["url"].endswith("/articles.json"))
        self.assertEqual(article["headers"]["X-Shopify-Access-Token"], "shpat-test")
        self.assertEqual(article["payload"]["article"]["title"], "企业软件怎么选")
        self.assertTrue(article["payload"]["article"]["published"])
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["platform"], "shopify")
        self.assertEqual(publication["status"], "published")
        self.assertEqual(publication["url"], "https://shop.example/blogs/news/hello")
        self.assertEqual(publication["remote_id"], "555")

    def test_shopify_draft(self):
        channel = self.channel(kind="shopify", config={"status": "draft"})
        content_id = self.content()

        def request(method, url, payload=None, headers=None, body=None, content_type=None):
            if url.endswith("/blogs.json"):
                return 200, '{"blogs": [{"id": 77, "handle": "news"}]}', url
            return 201, '{"article": {"id": 7, "handle": "d"}}', url

        with mock.patch.object(geo_channel, "_request", side_effect=request):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": channel["id"]})
        self.assertTrue(result["ok_push"])
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "draft_saved")
        item = geo.read(f"/api/geo/contents/{content_id}", {})["content"]
        self.assertNotEqual(item["status"], "published")

    def test_webflow_draft_and_publish(self):
        channel = self.channel(kind="webflow")
        content_id = self.content()
        seen = []

        def draft_request(method, url, payload=None, headers=None, body=None, content_type=None):
            seen.append({"url": url, "payload": payload, "headers": headers or {}})
            return 201, '{"id": "item-1"}', url

        with mock.patch.object(geo_channel, "_request", side_effect=draft_request):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": channel["id"]})
        self.assertTrue(result["ok_push"])
        item = next(entry for entry in seen if entry["url"].endswith("/items"))
        self.assertEqual(item["headers"]["Authorization"], "Bearer wf-test")
        self.assertEqual(item["payload"]["fieldData"]["name"], "企业软件怎么选")
        self.assertIn("post-body", item["payload"]["fieldData"])
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "draft_saved")

        published_channel = self.channel(kind="webflow", name="Webflow 发布",
                                         config={"status": "publish"})
        calls = []

        def publish_request(method, url, payload=None, headers=None, body=None, content_type=None):
            calls.append(url)
            if url.endswith("/publish"):
                return 200, '{"publishedItemIds": ["item-2"]}', url
            return 201, '{"id": "item-2"}', url

        with mock.patch.object(geo_channel, "_request", side_effect=publish_request):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": published_channel["id"]})
        self.assertTrue(result["ok_push"])
        self.assertTrue(any(url.endswith("/items/item-2/publish") for url in calls))
        latest = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(latest["status"], "published")
        self.assertEqual(latest["remote_id"], "item-2")

    def test_shopify_webflow_failures(self):
        channel = self.channel(kind="shopify")
        with mock.patch.object(geo_channel, "_request",
                               return_value=(401, "{}", "u")):
            result = geo.write("POST", f"/api/geo/channel-configs/{channel['id']}/test", {})
        self.assertFalse(result["ok_test"])
        self.assertIn("401", result["message"])
        content_id = self.content()
        with mock.patch.object(geo_channel, "_request",
                               return_value=(422, '{"errors": "bad"}', "u")):
            result = geo.write("POST", f"/api/geo/contents/{content_id}/push",
                              {"channel_id": channel["id"]})
        self.assertFalse(result["ok_push"])
        publication = geo.read(self.path("publications"), {})["items"][0]
        self.assertEqual(publication["status"], "failed")
        with self.assertRaises(commander.ChatError):
            geo.write("POST", "/api/geo/channel-configs",
                      {"kind": "shopify", "name": "缺字段", "config": {"shop": "https://x.example"}})
