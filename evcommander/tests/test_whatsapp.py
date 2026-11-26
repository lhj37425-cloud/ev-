"""WhatsApp Cloud API service tests: accounts, webhook, inbox and sending.

No network is used: Graph API calls are replaced with a local fake.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commander
import knowledge
import materials
import tenancy
import whatsapp


PHONE_ID = "123456789012345"
PEER = "8613800000000"
SECRET = secrets.token_hex(16)
VERIFY = secrets.token_hex(16)
TOKEN = secrets.token_hex(16)


class IsolatedData(unittest.TestCase):
    ENV_KEYS = ("EV_WHATSAPP_SYNC_SEND", "EV_WHATSAPP_MEDIA_FETCH",
                "EV_WHATSAPP_RATE_LIMIT", "EV_WHATSAPP_MEDIA_MAX")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        os.environ["EV_PAY_MOCK"] = "0"
        os.environ.pop("EV_SEED_DEMO", None)
        self.old_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        os.environ["EV_WHATSAPP_SYNC_SEND"] = "1"
        os.environ["EV_WHATSAPP_MEDIA_FETCH"] = "0"
        os.environ["EV_WHATSAPP_RATE_LIMIT"] = "0"
        tenancy.activate(tenancy.local_identity())

    def tearDown(self):
        tenancy.activate(None)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        commander.DATA_DIR = self.old_dir
        self.temp.cleanup()

    def media_payload(self, media_id="9876543210"):
        return {
            "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": PHONE_ID},
                "contacts": [{"wa_id": PEER, "profile": {"name": "客户甲"}}],
                "messages": [{
                    "id": "wamid.media.1", "from": PEER, "timestamp": "1760000000",
                    "type": "image",
                    "image": {"id": media_id, "mime_type": "image/png", "caption": "图片说明"},
                }],
            }}]}],
        }

    def account(self, **overrides):
        body = {
            "account_name": "测试 WhatsApp",
            "phone": "+8613800000000",
            "phone_number_id": PHONE_ID,
            "waba_id": "987654321",
            "access_token": TOKEN,
            "app_secret": SECRET,
            "verify_token": VERIFY,
        }
        body.update(overrides)
        return whatsapp.create_account(body)["data"]

    def payload(self, text="你好，价格多少？", wa_id="wamid.in.1", peer=PEER,
                phone_id=PHONE_ID, name="客户甲"):
        return {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "WABA",
                "changes": [{
                    "field": "messages",
                    "value": {
                        "metadata": {"phone_number_id": phone_id},
                        "contacts": [{"wa_id": peer, "profile": {"name": name}}],
                        "messages": [{
                            "id": wa_id, "from": peer, "timestamp": "1760000000",
                            "type": "text", "text": {"body": text},
                        }],
                    },
                }],
            }],
        }

    def deliver(self, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        signature = "sha256=" + hmac.new(
            SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        return whatsapp.receive_webhook(raw, {"X-Hub-Signature-256": signature})


class AccountTests(IsolatedData):
    def test_account_crud_hides_secrets(self):
        view = self.account()
        self.assertTrue(view["configured"])
        self.assertTrue(view["linked"])
        self.assertEqual(view["channel"], "whatsapp")
        for key in ("access_token", "app_secret", "verify_token"):
            self.assertNotIn(key, view)
        listing = whatsapp.read(whatsapp.ACCOUNTS, {})["data"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["account_name"], "测试 WhatsApp")
        self.assertIn("wa.account.create",
                      {row["action"] for row in tenancy.audit_log()})

        updated = whatsapp.write("PUT", f"/api/scrm/accounts/{view['id']}",
                                 {"account_name": "改名后", "enabled": 0})["data"]
        self.assertEqual(updated["account_name"], "改名后")
        self.assertFalse(updated["enabled"])

        with self.assertRaises(commander.ChatError):
            self.account()
        self.assertTrue(whatsapp.write("DELETE", f"/api/scrm/accounts/{view['id']}", {})["ok"])
        self.assertEqual(whatsapp.read(whatsapp.ACCOUNTS, {})["data"], [])

    def test_unconfigured_account_is_explicit(self):
        view = self.account(access_token="", app_secret="")
        self.assertFalse(view["configured"])
        self.assertTrue(view["verification_required"])
        self.assertIn("尚未配置", view["verification_message"])
        probe = whatsapp.write("POST", f"/api/scrm/accounts/{view['id']}/probe", {})
        self.assertFalse(probe["ok"])
        self.assertIn("未配置", probe["note"])

    def test_probe_success_and_failure(self):
        view = self.account()
        calls = []

        def fake(account, method, path, payload=None):
            calls.append((method, path))
            return {"display_phone_number": "+86 138 0000 0000", "verified_name": "测试号"}

        original = whatsapp._cloud_request
        whatsapp._cloud_request = fake
        try:
            result = whatsapp.write("POST", f"/api/scrm/accounts/{view['id']}/probe", {})
            self.assertTrue(result["ok"])
            self.assertIn("138 0000", result["note"])
        finally:
            whatsapp._cloud_request = original
        self.assertEqual(calls[0][0], "GET")

        def failing(account, method, path, payload=None):
            raise whatsapp.CloudError("无效的访问令牌")

        whatsapp._cloud_request = failing
        try:
            failed = whatsapp.probe_account(view["id"])
        finally:
            whatsapp._cloud_request = original
        self.assertFalse(failed["ok"])
        self.assertIn("无效的访问令牌", failed["note"])

    def test_reset_token_returns_once(self):
        view = self.account()
        result = whatsapp.write("POST", f"/api/scrm/accounts/{view['id']}/reset-token", {})
        token = result["data"]["token"]
        self.assertTrue(token)
        status, body, _ = whatsapp.verify_webhook({
            "hub.mode": ["subscribe"], "hub.verify_token": [token],
            "hub.challenge": ["challenge-2"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(body, b"challenge-2")


class OnboardingTests(IsolatedData):
    def test_state_enable_disable_and_roles(self):
        account = self.account()
        role = whatsapp.write("POST", whatsapp.ROLES, {
            "scene": "sales", "persona_name": "销售顾问", "persona_prompt": "简短回答",
            "kb_ids": [], "reply_max_len": 120,
        })["data"]
        listing = whatsapp.read(whatsapp.ROLES, {})
        self.assertEqual(listing["data"][0]["persona_name"], "销售顾问")

        state = whatsapp.read(whatsapp.ONBOARDING, {})
        self.assertEqual(len(state["items"]), 1)
        self.assertEqual(state["roles"][0]["name"], "销售顾问")
        self.assertEqual(state["role_id"], role["id"])

        result = whatsapp.write("POST", whatsapp.ONBOARDING_ENABLE, {
            "items": [{"channel": "whatsapp", "account_pk": account["id"]}],
            "sales_config_id": role["id"],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped"], [])
        view = whatsapp.read(whatsapp.ACCOUNTS, {})["data"][0]
        self.assertTrue(view["enabled"])
        self.assertEqual(view["sales_config_id"], role["id"])

        stopped = whatsapp.write("POST", whatsapp.ONBOARDING_DISABLE,
                                 {}, query={"account_pk": [str(account["id"])]})
        self.assertTrue(stopped["ok"])
        self.assertFalse(whatsapp.read(whatsapp.ACCOUNTS, {})["data"][0]["enabled"])
        self.assertTrue(whatsapp.write("DELETE", f"/api/scrm/roles/{role['id']}", {})["ok"])
        self.assertEqual(whatsapp.read(whatsapp.ROLES, {})["data"], [])


class WebhookTests(IsolatedData):
    def test_verification_and_signature(self):
        self.account()
        status, body, _ = whatsapp.verify_webhook({
            "hub.mode": ["subscribe"], "hub.verify_token": [VERIFY],
            "hub.challenge": ["challenge-1"],
        })
        self.assertEqual((status, body), (200, b"challenge-1"))
        self.assertEqual(whatsapp.verify_webhook({
            "hub.mode": ["subscribe"], "hub.verify_token": ["wrong"],
            "hub.challenge": ["x"],
        })[0], 403)
        raw = json.dumps(self.payload()).encode("utf-8")
        status, data = whatsapp.receive_webhook(raw, {"X-Hub-Signature-256": "sha256=bad"})
        self.assertEqual(status, 403)
        self.assertFalse(data["ok"])

    def test_inbound_message_thread_and_duplicate(self):
        self.account()
        status, data = self.deliver(self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"], 1)
        threads = whatsapp.read(whatsapp.THREADS, {})
        self.assertEqual(threads["total"], 1)
        thread = threads["data"][0]
        self.assertEqual(thread["peer_name"], "客户甲")
        self.assertEqual(thread["unread"], 1)
        messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": [str(thread["id"])]})
        self.assertTrue(messages["ok"])
        self.assertEqual(messages["data"][0]["content"], "你好，价格多少？")
        self.assertEqual(whatsapp.read(whatsapp.THREADS, {})["data"][0]["unread"], 0)

        status, data = self.deliver(self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"], 0)
        self.assertEqual(data["duplicates"], 1)
        thread = whatsapp.read(whatsapp.THREADS, {})["data"][0]
        self.assertEqual(thread["msg_count"], 1)
        self.assertEqual(thread["unread"], 0)

    def test_disabled_account_does_not_ingest_new_messages(self):
        account = self.account()
        whatsapp.update_account(account["id"], {"enabled": 0})
        status, data = self.deliver(self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"], 0)
        self.assertEqual(whatsapp.read(whatsapp.THREADS, {})["total"], 0)
        view = whatsapp.read(whatsapp.ACCOUNTS, {})["data"][0]
        self.assertFalse(view["enabled"])
        self.assertEqual(view["status_text"], "已停止值守")

    def test_missing_app_secret_is_not_receivable(self):
        view = self.account(app_secret="")
        self.assertTrue(view["configured"])
        self.assertTrue(view["verification_required"])
        self.assertIn("App Secret", view["verification_message"])
        state = whatsapp.read(whatsapp.ONBOARDING, {})
        self.assertIn("WhatsApp", state["unverified"])
        enabled = whatsapp.write("POST", whatsapp.ONBOARDING_ENABLE,
                                 {"items": [{"account_pk": view["id"]}]})
        self.assertEqual(enabled["skipped"][0]["channel_name"], "WhatsApp")

    def test_status_update_marks_delivery(self):
        account = self.account()
        thread = self.deliver(self.payload())
        self.assertEqual(thread[0], 200)
        messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": ["1"]})["data"]
        self.assertEqual(len(messages), 1)
        calls = []
        original = whatsapp._cloud_request

        def fake(account_row, method, path, payload=None):
            calls.append(payload)
            return {"messages": [{"id": "wamid.out.1"}]}

        whatsapp._cloud_request = fake
        try:
            result = whatsapp.write("POST", "/api/scrm/inbox/threads/1/send",
                                    {"content": "您好，已收到。"})
        finally:
            whatsapp._cloud_request = original
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["takeover"], 1)
        self.assertEqual(calls[0]["to"], PEER)
        status_payload = {
            "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": PHONE_ID},
                "statuses": [{"id": "wamid.out.1", "status": "delivered"}],
            }}]}],
        }
        status, data = self.deliver(status_payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["statuses"], 1)
        messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": ["1"]})["data"]
        self.assertEqual(messages[-1]["deliver_status"], "delivered")
        self.assertEqual(messages[-1]["role"], "self")


class SendTests(IsolatedData):
    def test_send_without_credentials_is_explicit(self):
        account = self.account()
        self.deliver(self.payload())
        whatsapp.update_account(account["id"], {"access_token": ""})
        with self.assertRaises(commander.ChatError) as captured:
            whatsapp.write("POST", "/api/scrm/inbox/threads/1/send", {"content": "你好"})
        self.assertIn("尚未配置", str(captured.exception))

    def test_send_records_message(self):
        self.account()
        self.deliver(self.payload())
        original = whatsapp._cloud_request
        calls = []

        def fake(account, method, path, payload=None):
            calls.append((method, path, payload))
            return {"messages": [{"id": "wamid.out.2"}]}

        whatsapp._cloud_request = fake
        try:
            result = whatsapp.write("POST", "/api/scrm/inbox/threads/1/send",
                                    {"content": "您好，价格请咨询客服。"})
        finally:
            whatsapp._cloud_request = original
        self.assertEqual(result["note"], "已排入发送队列")
        messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": ["1"]})["data"]
        self.assertEqual(messages[-1]["role"], "self")
        self.assertEqual(messages[-1]["content"], "您好，价格请咨询客服。")
        self.assertEqual(messages[-1]["deliver_status"], "sent")
        self.assertEqual(calls[0][1], f"/{PHONE_ID}/messages")
        self.assertIn("wa.message.queue",
                      {row["action"] for row in tenancy.audit_log()})

    def test_template_payload(self):
        account = self.account()
        captured = {}

        def fake(account_row, method, path, payload=None):
            captured.update(payload)
            return {"messages": [{"id": "wamid.template"}]}

        original = whatsapp._cloud_request
        whatsapp._cloud_request = fake
        try:
            wa_id = whatsapp.send_template(account, PEER, "hello_world", "en_US")
        finally:
            whatsapp._cloud_request = original
        self.assertEqual(wa_id, "wamid.template")
        self.assertEqual(captured["type"], "template")
        self.assertEqual(captured["template"]["name"], "hello_world")


class TranslateTests(IsolatedData):
    def test_translate_thread_and_draft(self):
        self.account()
        self.deliver(self.payload())
        original = commander.generate

        def translate(messages, max_output_tokens=1024, usage=None):
            payload = json.loads(messages[-1]["content"])
            yield json.dumps([{"id": item["id"], "zh": "你好，价格多少？", "lang": "zh"}
                              for item in payload], ensure_ascii=False)

        commander.generate = translate
        try:
            result = whatsapp.write("POST", "/api/scrm/inbox/threads/1/translate", {})
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"][0]["content_zh"], "你好，价格多少？")
            messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": ["1"]})["data"]
            self.assertEqual(messages[0]["lang"], "zh")

            def draft(messages, max_output_tokens=1024, usage=None):
                yield json.dumps({"text": "Hello, how can I help?", "lang": "en"})

            commander.generate = draft
            draft_result = whatsapp.write(
                "POST", "/api/scrm/inbox/threads/1/translate-draft",
                {"text": "你好，有什么可以帮您？", "target_lang": "en"})
            self.assertTrue(draft_result["ok"])
            self.assertEqual(draft_result["data"]["lang"], "en")
        finally:
            commander.generate = original


class AutoReplyTests(IsolatedData):
    def test_auto_reply_records_ai_message_without_takeover(self):
        account = self.account()
        role = whatsapp.write("POST", whatsapp.ROLES, {
            "scene": "sales", "persona_name": "销售顾问",
            "persona_prompt": "简短回答客户", "kb_ids": [],
        })["data"]
        # Ingest first with auto-reply off, then call the worker directly so the
        # test does not depend on a background thread racing the assertions.
        self.deliver(self.payload())
        whatsapp.update_account(account["id"], {"auto_reply": 1,
                                               "sales_config_id": role["id"]})
        original_generate = commander.generate
        original_cloud = whatsapp._cloud_request

        def fake_generate(messages, max_output_tokens=1024, usage=None):
            yield "您好，价格可以帮您确认。"

        def fake_cloud(account_row, method, path, payload=None):
            return {"messages": [{"id": "wamid.ai.1"}]}

        commander.generate = fake_generate
        whatsapp._cloud_request = fake_cloud
        try:
            whatsapp._auto_reply(account["id"], 1, tenancy.DEFAULT_TENANT)
        finally:
            commander.generate = original_generate
            whatsapp._cloud_request = original_cloud
        thread = whatsapp.read(whatsapp.THREADS, {})["data"][0]
        self.assertFalse(thread["takeover"])
        messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": ["1"]})["data"]
        self.assertEqual(messages[-1]["source"], "ai")
        self.assertEqual(messages[-1]["role"], "self")

    def test_auto_reply_deduplicates_concurrent_jobs(self):
        original = whatsapp._auto_reply
        gate = threading.Event()
        started = threading.Event()

        def fake(*args):
            started.set()
            gate.wait(2)

        whatsapp._auto_reply = fake
        try:
            self.assertTrue(whatsapp.schedule_auto_reply(1, 1, tenancy.DEFAULT_TENANT))
            self.assertTrue(started.wait(2))
            self.assertFalse(whatsapp.schedule_auto_reply(1, 1, tenancy.DEFAULT_TENANT))
            gate.set()
            time.sleep(0.2)
            self.assertTrue(whatsapp.schedule_auto_reply(1, 1, tenancy.DEFAULT_TENANT))
        finally:
            gate.set()
            whatsapp._auto_reply = original


class MediaTests(IsolatedData):
    def test_media_id_and_download_to_material_library(self):
        account = self.account()
        self.deliver(self.media_payload())
        messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": ["1"]})["data"]
        self.assertEqual(messages[0]["msg_type"], "image")
        self.assertEqual(messages[0]["media_url"], "")
        with commander.database() as db:
            row = db.execute(
                "SELECT id,media_id FROM wa_messages WHERE tenant_id=? AND wa_id=?",
                (tenancy.DEFAULT_TENANT, "wamid.media.1")).fetchone()
        self.assertEqual(row["media_id"], "9876543210")

        original = whatsapp.download_media
        whatsapp.download_media = lambda account_row, media_id: (
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")
        try:
            whatsapp._fetch_media(account["id"], row["id"], tenancy.DEFAULT_TENANT)
        finally:
            whatsapp.download_media = original
        messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": ["1"]})["data"]
        self.assertTrue(messages[0]["media_url"].startswith("/material-media/"))
        key = messages[0]["media_url"].rsplit("/", 1)[-1]
        self.assertIsNotNone(materials.connection_file(key))

    def test_download_media_rejects_http_url(self):
        account = self.account()
        with commander.database() as db:
            row = whatsapp._account_row(db, account["id"])
        original = whatsapp._cloud_request
        whatsapp._cloud_request = lambda *args, **kwargs: {
            "url": "http://127.0.0.1/media", "mime_type": "image/png", "file_size": 1}
        try:
            with self.assertRaises(whatsapp.CloudError):
                whatsapp.download_media(row, "123456")
        finally:
            whatsapp._cloud_request = original


class OutboxTests(IsolatedData):
    def test_send_is_queued_then_delivered(self):
        self.account()
        self.deliver(self.payload())
        os.environ["EV_WHATSAPP_SYNC_SEND"] = "0"
        original_worker = whatsapp._ensure_outbox_worker
        original_cloud = whatsapp._cloud_request
        whatsapp._ensure_outbox_worker = lambda: None
        whatsapp._cloud_request = lambda *args, **kwargs: {
            "messages": [{"id": "wamid.out.queue"}]}
        try:
            result = whatsapp.write("POST", "/api/scrm/inbox/threads/1/send",
                                    {"content": "排队发送"})
            self.assertEqual(result["note"], "已排入发送队列")
            messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": ["1"]})["data"]
            self.assertEqual(messages[-1]["deliver_status"], "pending")
            self.assertGreaterEqual(whatsapp.flush_outbox(), 1)
            messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": ["1"]})["data"]
            self.assertEqual(messages[-1]["deliver_status"], "sent")
        finally:
            whatsapp._ensure_outbox_worker = original_worker
            whatsapp._cloud_request = original_cloud
            os.environ["EV_WHATSAPP_SYNC_SEND"] = "1"

    def test_rate_limit_requeues(self):
        self.account()
        self.deliver(self.payload())
        os.environ["EV_WHATSAPP_SYNC_SEND"] = "0"
        original_worker = whatsapp._ensure_outbox_worker
        original_cloud = whatsapp._cloud_request
        whatsapp._ensure_outbox_worker = lambda: None
        whatsapp._cloud_request = lambda *args, **kwargs: {
            "messages": [{"id": "wamid.out.rate"}]}
        try:
            whatsapp.write("POST", "/api/scrm/inbox/threads/1/send", {"content": "第一条"})
            whatsapp.flush_outbox(respect_limit=True)
            os.environ["EV_WHATSAPP_RATE_LIMIT"] = "1"
            whatsapp.write("POST", "/api/scrm/inbox/threads/1/send", {"content": "第二条"})
            whatsapp.flush_outbox(respect_limit=True)
            messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": ["1"]})["data"]
            self.assertEqual(messages[-1]["deliver_status"], "pending")
            with commander.database() as db:
                db.execute("UPDATE wa_outbox SET next_at='' WHERE status='pending'")
            os.environ["EV_WHATSAPP_RATE_LIMIT"] = "0"
            whatsapp.flush_outbox()
            messages = whatsapp.read(whatsapp.MESSAGES, {"thread_id": ["1"]})["data"]
            self.assertEqual(messages[-1]["deliver_status"], "sent")
        finally:
            whatsapp._ensure_outbox_worker = original_worker
            whatsapp._cloud_request = original_cloud
            os.environ["EV_WHATSAPP_SYNC_SEND"] = "1"


class PermissionTests(IsolatedData):
    def test_member_cannot_manage_accounts_roles_or_onboarding(self):
        with tenancy.scope(tenancy.DEFAULT_TENANT, role="member"):
            self.assertEqual(whatsapp.read(whatsapp.ACCOUNTS, {})["data"], [])
            with self.assertRaises(PermissionError):
                whatsapp.create_account({"account_name": "无权限号"})
            with self.assertRaises(PermissionError):
                whatsapp.save_role({"scene": "sales", "persona_name": "无权限人设"})
            with self.assertRaises(PermissionError):
                whatsapp.write("POST", whatsapp.ONBOARDING_ENABLE,
                               {"items": [{"account_pk": 1}]})

    def test_owner_can_manage_own_tenant(self):
        tenant = tenancy.create_tenant("Owner权限租户")
        owner = tenancy.create_user(tenant, "owner_perm", secrets.token_urlsafe(16), "owner")
        with tenancy.scope(tenant, user_id=owner, username="owner_perm", role="owner"):
            view = whatsapp.create_account({
                "account_name": "Owner号", "phone_number_id": "777000111",
                "access_token": secrets.token_hex(16),
                "app_secret": secrets.token_hex(16),
                "verify_token": secrets.token_hex(16),
            })["data"]
            self.assertEqual(view["account_name"], "Owner号")
            self.assertEqual(len(whatsapp.list_accounts()), 1)


class IsolationTests(IsolatedData):
    def test_accounts_and_threads_do_not_cross_tenants(self):
        own = self.account()
        other = tenancy.create_tenant("客户乙")
        with tenancy.scope(tenancy.DEFAULT_TENANT):
            self.assertEqual(len(whatsapp.list_accounts()), 1)
        with tenancy.scope(other, role="owner"):
            self.assertEqual(whatsapp.list_accounts(), [])
            with self.assertRaises(commander.ChatError):
                self.account()
            self.assertEqual(whatsapp.read(whatsapp.THREADS, {})["total"], 0)

        self.deliver(self.payload())
        with tenancy.scope(other):
            self.assertEqual(whatsapp.read(whatsapp.THREADS, {})["total"], 0)
        with tenancy.scope(tenancy.DEFAULT_TENANT):
            self.assertEqual(whatsapp.read(whatsapp.THREADS, {})["total"], 1)

    def test_roles_knowledge_is_tenant_scoped(self):
        other = tenancy.create_tenant("客户丙")
        with tenancy.scope(other):
            book = knowledge.write("/api/kb/create", {"name": "乙的知识库", "description": ""})
        with self.assertRaises(commander.ChatError):
            whatsapp.save_role({"scene": "sales", "persona_name": "销售",
                                "kb_ids": [book["id"]]})
        with tenancy.scope(other, role="owner"):
            role = whatsapp.save_role({"scene": "sales", "persona_name": "销售",
                                       "kb_ids": [book["id"]]})
            self.assertTrue(role["ok"])
            self.assertEqual(whatsapp.read(whatsapp.ROLES, {})["data"][0]["kb_count"], 1)


if __name__ == "__main__":
    unittest.main()
