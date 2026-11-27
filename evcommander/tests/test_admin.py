"""Tenant administration tests: roles, quotas, audit and admin page."""

import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin
import commander
import tenancy


ADMIN = {"tenant_id": tenancy.DEFAULT_TENANT, "user_id": "local",
         "username": "平台管理员", "role": "admin"}
PASSWORD = secrets.token_urlsafe(16)


class IsolatedData(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = commander.DATA_DIR
        commander.DATA_DIR = Path(self.temp.name)
        os.environ["EV_PAY_MOCK"] = "0"
        os.environ.pop("EV_SEED_DEMO", None)
        tenancy.activate(ADMIN)

    def tearDown(self):
        tenancy.activate(None)
        commander.DATA_DIR = self.old_dir
        self.temp.cleanup()


class AdminTests(IsolatedData):
    def create_tenant(self, name="客户A", max_users=5):
        return admin.write("POST", admin.TENANTS,
                           {"name": name, "max_users": max_users})["data"]

    def test_tenant_crud_and_overview(self):
        tenant = self.create_tenant("客户甲", 2)
        self.assertEqual(tenant["max_users"], 2)
        self.assertEqual(tenant["active_users"], 0)
        tenants = admin.read(admin.TENANTS, {})["data"]
        ids = [item["id"] for item in tenants]
        self.assertIn(tenant["id"], ids)
        self.assertIn(tenancy.DEFAULT_TENANT, ids)
        overview = admin.read(admin.OVERVIEW, {})["data"]
        self.assertEqual(overview["role"], "admin")
        self.assertEqual(overview["totals"]["tenants"], len(ids))
        updated = admin.write("PUT", f"/api/admin/tenants/{tenant['id']}",
                              {"name": "改名", "status": "disabled",
                               "max_users": 3})["data"]
        self.assertEqual(updated["name"], "改名")
        self.assertEqual(updated["status"], "disabled")
        self.assertEqual(updated["max_users"], 3)
        self.assertIn("tenant.create", [row["action"] for row in tenancy.audit_log()])

    def test_user_create_update_and_audit(self):
        tenant = self.create_tenant()
        result = admin.write("POST", admin.USERS, {
            "tenant_id": tenant["id"], "username": "member_a",
            "password": PASSWORD, "role": "member",
        })
        user = result["data"]
        for secret in ("password", "salt", "password_hash"):
            self.assertNotIn(secret, user)
        self.assertEqual(user["role"], "member")
        users = admin.read(admin.USERS, {})["data"]
        self.assertEqual(users[0]["username"], "member_a")
        updated = admin.write("PUT", f"/api/admin/users/{user['id']}",
                              {"role": "owner"})["data"]
        self.assertEqual(updated["role"], "owner")
        updated = admin.write("PUT", f"/api/admin/users/{user['id']}",
                              {"status": "disabled"})["data"]
        self.assertEqual(updated["status"], "disabled")
        audit = admin.read(admin.AUDIT, {"tenant": [tenant["id"]]})["data"]
        actions = [row["action"] for row in audit]
        self.assertIn("user.create", actions)
        self.assertIn("user.role", actions)
        self.assertIn("user.disabled", actions)
        self.assertNotIn(PASSWORD, str(audit))

    def test_max_users_is_enforced(self):
        tenant = self.create_tenant("小租户", 1)
        admin.write("POST", admin.USERS, {
            "tenant_id": tenant["id"], "username": "first",
            "password": PASSWORD, "role": "member",
        })
        with self.assertRaises(ValueError):
            admin.write("POST", admin.USERS, {
                "tenant_id": tenant["id"], "username": "second",
                "password": PASSWORD, "role": "member",
            })

    def test_owner_can_only_manage_own_tenant(self):
        first = self.create_tenant("客户甲")
        second = self.create_tenant("客户乙")
        owner_id = tenancy.create_user(first["id"], "owner_a", PASSWORD, "owner")
        tenancy.activate({"tenant_id": first["id"], "user_id": owner_id,
                          "username": "owner_a", "role": "owner"})
        tenants = admin.read(admin.TENANTS, {})["data"]
        self.assertEqual([item["id"] for item in tenants], [first["id"]])
        users = admin.read(admin.USERS, {})["data"]
        self.assertEqual({item["tenant_id"] for item in users}, {first["id"]})
        created = admin.write("POST", admin.USERS, {
            "username": "member_b", "password": PASSWORD, "role": "member",
        })
        self.assertEqual(created["data"]["tenant_id"], first["id"])
        with self.assertRaises(PermissionError):
            admin.write("POST", admin.TENANTS, {"name": "不给开"})
        with self.assertRaises(PermissionError):
            admin.read(admin.USERS, {"tenant": [second["id"]]})
        with self.assertRaises(PermissionError):
            admin.write("POST", admin.USERS, {
                "tenant_id": second["id"], "username": "x",
                "password": PASSWORD, "role": "member",
            })
        with self.assertRaises(PermissionError):
            admin.write("POST", admin.USERS, {
                "username": "owner_x", "password": PASSWORD, "role": "owner",
            })
        with self.assertRaises(PermissionError):
            admin.write("POST", admin.USERS, {
                "tenant_id": first["id"], "username": "admin_x",
                "password": PASSWORD, "role": "admin",
            })


    def test_owner_audit_excludes_other_tenants_and_member_is_denied(self):
        first = self.create_tenant("审计租户甲")["id"]
        second = self.create_tenant("审计租户乙")["id"]
        with commander.database() as db:
            tenancy.audit_in(db, first, "test.first", "", "")
            tenancy.audit_in(db, second, "test.second", "", "")
        with tenancy.scope(first, role="owner"):
            records = admin.read(admin.AUDIT, {})["data"]
            self.assertEqual({row["tenant_id"] for row in records}, {first})
            self.assertIn("test.first", {row["action"] for row in records})
            with self.assertRaises(PermissionError):
                admin.read(admin.AUDIT, {"tenant": [second]})
        with tenancy.scope(first, role="member"):
            with self.assertRaises(PermissionError):
                admin.read(admin.AUDIT, {})

    def test_member_has_no_admin_access(self):
        tenant = self.create_tenant()
        tenancy.activate({"tenant_id": tenant["id"], "user_id": "member",
                          "username": "member", "role": "member"})
        with self.assertRaises(PermissionError):
            admin.read(admin.ME, {})
        with self.assertRaises(PermissionError):
            admin.read(admin.OVERVIEW, {})

    def test_self_disable_and_role_change_are_blocked(self):
        tenant = self.create_tenant()
        admin_id = tenancy.create_user(tenant["id"], "admin_2", PASSWORD, "admin")
        tenancy.activate({"tenant_id": tenant["id"], "user_id": admin_id,
                          "username": "admin_2", "role": "admin"})
        with self.assertRaises(ValueError):
            admin.write("PUT", f"/api/admin/users/{admin_id}", {"status": "disabled"})
        with self.assertRaises(ValueError):
            admin.write("PUT", f"/api/admin/users/{admin_id}", {"role": "member"})
        # CLI/direct calls must also keep at least one active platform admin.
        with self.assertRaises(ValueError):
            tenancy.set_user_status("admin_2", "disabled")
        with self.assertRaises(ValueError):
            tenancy.set_user_role("admin_2", "member")

    def test_owner_cannot_modify_owner_or_admin(self):
        tenant = self.create_tenant()
        owner_id = tenancy.create_user(tenant["id"], "owner_a", PASSWORD, "owner")
        other_id = tenancy.create_user(tenant["id"], "owner_b", PASSWORD, "owner")
        tenancy.activate({"tenant_id": tenant["id"], "user_id": owner_id,
                          "username": "owner_a", "role": "owner"})
        with self.assertRaises(PermissionError):
            admin.write("PUT", f"/api/admin/users/{other_id}", {"status": "disabled"})

    def test_totp_setup_enable_and_reset(self):
        tenant = self.create_tenant()
        user_id = tenancy.create_user(tenant["id"], "admin_totp", PASSWORD, "admin")
        tenancy.activate({"tenant_id": tenant["id"], "user_id": user_id,
                          "username": "admin_totp", "role": "admin"})
        setup = admin.write("POST", admin.ME_TOTP_SETUP, {})["data"]
        code = tenancy.totp_code(setup["secret"])
        admin.write("POST", admin.ME_TOTP_ENABLE, {"code": code})
        self.assertTrue(admin.read(admin.ME_SECURITY, {})["data"]["totp_enabled"])
        admin.write("PUT", f"/api/admin/users/{user_id}", {"totp_reset": True})
        self.assertFalse(admin.read(admin.ME_SECURITY, {})["data"]["totp_enabled"])

    def test_login_history_is_visible_to_admin(self):
        tenant = self.create_tenant()
        tenancy.create_user(tenant["id"], "login_user", PASSWORD, "member")
        tenancy.authenticate("login_user", "wrong password", ip="127.0.0.1")
        tenancy.activate(ADMIN)
        rows = admin.read(admin.LOGINS, {"tenant": [tenant["id"]]})["data"]
        self.assertTrue(any(row["username"] == "login_user" for row in rows))
        self.assertFalse(any(row["success"] for row in rows))

    def test_page_and_path_validation(self):
        html = admin.page().decode("utf-8")
        self.assertIn("管理后台", html)
        self.assertNotIn("__LOCAL_FLAG__", html)
        self.assertNotIn("__LOCAL_BOOT__", html)
        local = admin.page("heph-local-preview").decode("utf-8")
        self.assertIn("heph-local-preview", local)
        self.assertEqual(admin.normalize_path(""), "")
        self.assertEqual(admin.normalize_path("/admin-test/"), "/admin-test")
        for value in ("/adminx", "/ops", "/admin/../x", "/admin//x"):
            with self.assertRaises(ValueError):
                admin.normalize_path(value)


if __name__ == "__main__":
    unittest.main()
