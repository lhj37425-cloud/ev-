"""Tenant isolation, accounts/sessions and single-tenant database upgrade.

Run from evcommander/:
    python -m unittest discover -s tests -v
"""

from email.message import Message
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import acquisition
import commander
import customers
import geo
import image_generation
import inspirations
import knowledge
import materials
import memory
import operations
import payments
import tenancy
from invitation import Invitation, COOKIE


ORIGIN = "http://127.0.0.1:8766"
PASSWORD = "correct horse battery"


class IsolatedData(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        os.environ["EV_PAY_MOCK"] = "0"
        os.environ.pop("EV_SEED_DEMO", None)

    def tearDown(self):
        commander.DATA_DIR = self.old_dir
        self.temp.cleanup()


class AccountTests(IsolatedData):
    def test_tenant_and_user_lifecycle(self):
        tenant = tenancy.create_tenant("客户A")
        self.assertTrue(tenancy.tenant_exists(tenant))
        user = tenancy.create_user(tenant, "client_a", PASSWORD)
        self.assertTrue(user)
        with self.assertRaises(ValueError):
            tenancy.create_user(tenant, "client_a", PASSWORD)
        with self.assertRaises(ValueError):
            tenancy.create_user(tenant, "short", "1234567")
        with self.assertRaises(ValueError):
            tenancy.create_user("missing", "client_x", PASSWORD)

        result = tenancy.authenticate("client_a", PASSWORD)
        self.assertIsNotNone(result)
        identity, token = result
        self.assertEqual(identity["tenant_id"], tenant)
        self.assertEqual(identity["role"], "member")
        self.assertEqual(tenancy.session_identity(token)["username"], "client_a")
        self.assertIsNone(tenancy.authenticate("client_a", "wrong password"))

        tenancy.sign_out(token)
        self.assertIsNone(tenancy.session_identity(token))

    def test_disabled_user_cannot_login_or_keep_session(self):
        tenant = tenancy.create_tenant("客户B")
        tenancy.create_user(tenant, "client_b", PASSWORD)
        identity, token = tenancy.authenticate("client_b", PASSWORD)
        self.assertIsNotNone(identity)
        tenancy.set_user_status("client_b", "disabled")
        self.assertIsNone(tenancy.authenticate("client_b", PASSWORD))
        self.assertIsNone(tenancy.session_identity(token))
        tenancy.set_user_status("client_b", "active")
        self.assertIsNotNone(tenancy.authenticate("client_b", PASSWORD))

    def test_session_expires(self):
        tenant = tenancy.create_tenant("客户C")
        tenancy.create_user(tenant, "client_c", PASSWORD)
        _, token = tenancy.authenticate("client_c", PASSWORD)
        with commander.database() as db:
            db.execute("UPDATE sessions SET expires_at=? WHERE token_hash IS NOT NULL",
                       (int(time.time()) - 1,))
        self.assertIsNone(tenancy.session_identity(token))

    def test_admin_tenant_switch(self):
        tenant = tenancy.create_tenant("客户D")
        admin = tenancy.local_identity()
        self.assertEqual(tenancy.with_requested_tenant(admin, tenant)["tenant_id"], tenant)
        self.assertEqual(tenancy.with_requested_tenant(admin, "missing")["tenant_id"],
                         tenancy.DEFAULT_TENANT)
        member = {"tenant_id": tenant, "user_id": "u", "username": "u", "role": "member"}
        self.assertEqual(tenancy.with_requested_tenant(member, tenancy.DEFAULT_TENANT)["tenant_id"],
                         tenant)


class FakeRequest:
    def __init__(self, body=b"", cookie=""):
        self.headers = Message()
        self.headers["Origin"] = ORIGIN
        self.headers["Host"] = "127.0.0.1:8766"
        self.headers["Content-Type"] = "application/x-www-form-urlencoded"
        self.headers["Content-Length"] = str(len(body))
        if cookie:
            self.headers["Cookie"] = cookie
        self.rfile = BytesIO(body)
        self.sent = []

    def send_header(self, key, value):
        self.sent.append((key, value))


class InvitationLoginTests(IsolatedData):
    def setUp(self):
        super().setUp()
        self.config = Path(self.temp.name) / "invite.json"
        salt = b"\x01" * 16
        digest = tenancy.hash_password(PASSWORD, salt)
        self.session_key = b"\x02" * 32
        self.config.write_text(json.dumps({
            "username": "boss",
            "salt": salt.hex(),
            "password_hash": digest.hex(),
            "session_key": self.session_key.hex(),
        }), encoding="utf-8")
        self.invite = Invitation(ORIGIN, self.config)

    def login(self, username, password):
        body = f"username={username}&password={password}".encode()
        return self.invite.authenticate(FakeRequest(body))

    def cookie(self, token):
        expiry = int(time.time()) + 300
        value = f"{expiry}.{token}"
        signature = self.invite.signature(self.session_key, value)
        return f"{COOKIE}={value}.{signature}"

    def test_seed_account_can_login_and_read_session(self):
        username, salt, hashed, _ = self.invite.credentials()
        self.assertTrue(tenancy.seed_invite_user(username, salt.hex(), hashed.hex()))
        self.assertFalse(tenancy.seed_invite_user(username, salt.hex(), hashed.hex()))

        status, token = self.login("boss", PASSWORD)
        self.assertEqual(status, 303)
        identity = self.invite.session(FakeRequest(cookie=self.cookie(token)))
        self.assertEqual(identity["username"], "boss")
        self.assertEqual(identity["role"], "admin")
        self.assertEqual(identity["tenant_id"], tenancy.DEFAULT_TENANT)

    def test_wrong_or_forged_session_is_rejected(self):
        username, salt, hashed, _ = self.invite.credentials()
        tenancy.seed_invite_user(username, salt.hex(), hashed.hex())
        self.assertEqual(self.login("boss", "wrong password")[0], 401)
        self.assertFalse(self.invite.session(FakeRequest(cookie=self.cookie("a" * 64))))
        self.assertFalse(self.invite.session(FakeRequest(cookie=f"{COOKIE}=bad")))


class IsolationTests(IsolatedData):
    def setUp(self):
        super().setUp()
        self.tenant_a = tenancy.create_tenant("客户甲")
        self.tenant_b = tenancy.create_tenant("客户乙")

    def test_contacts_orders_geo_knowledge_memory_inspirations(self):
        conversation = commander.conversation_id()
        with tenancy.scope(self.tenant_a):
            contact = customers.write("POST", customers.CRM,
                                      {"name": "甲的客户", "phone": "13800000011"})["data"]
            order = payments.write(payments.RECHARGE, {"channel": "alipay", "amount": 10})
            project = geo.write("POST", geo.PROJECTS, {
                "brand_name": "甲的品牌", "aliases": [], "website": "", "industry": "",
                "region": "", "description": "", "market": "cn", "locales": [],
            })["project"]
            book = knowledge.write("/api/kb/create", {"name": "甲的知识库", "description": ""})
            memory.write(memory.PREFIX + "add", {"content": "甲的偏好"})
            inspiration = inspirations.save("甲的灵感", "甲的内容", "idea", "灵感", {}, "测试")
            materials.write("/api/material/folder/create", {"name": "甲的文件夹"})
            with commander.database() as db:
                operations.configure(db)
                db.execute(
                    "INSERT INTO operations_projects (id,tenant_id,fingerprint,details,updated_at) "
                    "VALUES (?,?,?,?,?)",
                    ("a" * 32, self.tenant_a, "fingerprint-a",
                     json.dumps({"project_id": "a" * 32, "title": "甲的项目"}, ensure_ascii=False),
                     "2026-11-20T00:00:00+00:00"),
                )
            image_generation.save(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "甲的图")
            commander.save_turn(conversation, "你好", "你好呀", "chat")

        with tenancy.scope(self.tenant_b):
            self.assertEqual(customers.read(customers.CRM, {})["data"], [])
            with self.assertRaises(commander.ChatError):
                customers.read(f"{customers.CRM}/{contact['id']}", {})
            self.assertEqual(payments.read(payments.ORDERS, {})["total"], 0)
            with self.assertRaises(commander.ChatError):
                payments.read(payments.ORDER_DETAIL, {"order_no": [order["order_no"]]})
            self.assertEqual(geo.read(geo.PROJECTS, {})["total"], 0)
            with self.assertRaises(commander.ChatError):
                geo.read(f"{geo.PROJECTS}/{project['id']}", {})
            self.assertEqual(knowledge.read("/api/kb/list", {})["list"], [])
            with self.assertRaises(commander.ChatError):
                knowledge.read("/api/kb/detail", {"id": [str(book["id"])]})
            self.assertEqual(memory.read(memory.PREFIX + "list")["items"], [])
            self.assertEqual(inspirations.listing({})["total"], 0)
            with self.assertRaises(commander.ChatError):
                inspirations.detail(inspiration["id"])
            self.assertEqual(materials.read("/api/material/folders", {})["list"], [])
            self.assertEqual(operations.read(operations.PROJECTS, {})["total"], 0)
            with self.assertRaises(commander.ChatError):
                operations.read(operations.PROJECTS + "/" + "a" * 32, {})
            self.assertEqual(image_generation.list_works({})["total"], 0)
            with self.assertRaises(commander.ChatError):
                commander.load_conversation(conversation)
            with self.assertRaises(commander.ChatError):
                commander.save_turn(conversation, "抢写", "不应写入", "chat")

        with tenancy.scope(self.tenant_a):
            self.assertEqual(customers.read(customers.CRM, {})["data"][0]["id"], contact["id"])
            self.assertEqual(payments.read(payments.ORDERS, {})["total"], 1)
            self.assertEqual(geo.read(geo.PROJECTS, {})["total"], 1)
            self.assertEqual(len(knowledge.read("/api/kb/list", {})["list"]), 1)
            self.assertEqual(memory.read(memory.PREFIX + "list")["items"][0]["content"], "甲的偏好")
            self.assertEqual(inspirations.listing({})["total"], 1)
            self.assertEqual(len(materials.read("/api/material/folders", {})["list"]), 1)
            self.assertEqual(operations.read(operations.PROJECTS, {})["total"], 1)
            self.assertEqual(image_generation.list_works({})["total"], 1)
            self.assertEqual(commander.load_conversation(conversation)["messages"][-1]["content"],
                             "你好呀")

    def test_business_profile_is_per_tenant(self):
        with tenancy.scope(self.tenant_a):
            acquisition.write(acquisition.BIZ_PROFILE, {"main_products": "甲的产品"})
        with tenancy.scope(self.tenant_b):
            self.assertEqual(acquisition.read(acquisition.BIZ_PROFILE, {})["main_products"], "")
            acquisition.write(acquisition.BIZ_PROFILE, {"main_products": "乙的产品"})
        with tenancy.scope(self.tenant_a):
            self.assertEqual(acquisition.read(acquisition.BIZ_PROFILE, {})["main_products"], "甲的产品")


OLD_SCHEMA = """
CREATE TABLE pay_account (
    id INTEGER PRIMARY KEY CHECK (id=1), power REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0, total_recharge REAL NOT NULL DEFAULT 0);
INSERT INTO pay_account VALUES (1, 777, 5, 100);
CREATE TABLE acquisition_profile (id INTEGER PRIMARY KEY CHECK (id=1), data TEXT NOT NULL);
INSERT INTO acquisition_profile VALUES (1, '{"main_products":"旧产品","target_customer":"旧客户","exclude_scope":""}');
CREATE TABLE commander_memory (
    id INTEGER PRIMARY KEY, content TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
    salience INTEGER NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO commander_memory VALUES (1, '旧记忆', 'fact', 4, '2026-11-19T00:00:00+00:00');
CREATE TABLE operations_projects (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, details TEXT NOT NULL,
    updated_at TEXT NOT NULL);
INSERT INTO operations_projects VALUES ('p1', 'f1', '{"title":"旧项目"}', '2026-11-19T00:00:00+00:00');
CREATE TABLE trial_usage (day TEXT PRIMARY KEY, count INTEGER NOT NULL);
INSERT INTO trial_usage VALUES ('2026-11-19', 3);
CREATE TABLE image_usage (day TEXT PRIMARY KEY, count INTEGER NOT NULL);
INSERT INTO image_usage VALUES ('2026-11-19', 2);
CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at TEXT NOT NULL,
    source TEXT NOT NULL, messages TEXT NOT NULL);
INSERT INTO conversations VALUES ('c1', '旧会话', '2026-11-19T00:00:00+00:00', 'chat', '[]');
"""


class MigrationTests(IsolatedData):
    def test_single_tenant_database_is_upgraded_to_demo_tenant(self):
        path = Path(self.temp.name) / "commander.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript(OLD_SCHEMA)

        with commander.database() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(pay_account)")}
            self.assertIn("tenant_id", columns)
            self.assertEqual(db.execute(
                "SELECT power FROM pay_account WHERE tenant_id='demo'").fetchone()[0], 777)

        self.assertEqual(payments.account()["balance_power"], 777)
        self.assertEqual(memory.listing()[0]["content"], "旧记忆")
        self.assertEqual(acquisition.read(acquisition.BIZ_PROFILE, {})["main_products"], "旧产品")
        with commander.database() as db:
            self.assertEqual(db.execute(
                "SELECT tenant_id FROM operations_projects WHERE id='p1'").fetchone()[0], "demo")
            self.assertEqual(db.execute(
                "SELECT tenant_id FROM trial_usage WHERE day='2026-11-19'").fetchone()[0], "demo")
            self.assertEqual(db.execute(
                "SELECT tenant_id FROM image_usage WHERE day='2026-11-19'").fetchone()[0], "demo")
            self.assertEqual(db.execute(
                "SELECT tenant_id FROM conversations WHERE id='c1'").fetchone()[0], "demo")
        self.assertEqual(commander.load_conversation("c1")["id"], "c1")

        other = tenancy.create_tenant("新客户")
        with tenancy.scope(other):
            self.assertEqual(payments.account()["balance_power"], 100)
            self.assertEqual(memory.listing(), [])
        with tenancy.scope(tenancy.DEFAULT_TENANT):
            self.assertEqual(payments.account()["balance_power"], 777)

    def test_migration_is_idempotent(self):
        with commander.database():
            pass
        with commander.database():
            pass
        self.assertEqual(len(tenancy.tenants()), 1)


if __name__ == "__main__":
    unittest.main()
