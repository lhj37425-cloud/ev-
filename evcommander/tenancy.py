"""Tenants, accounts and login sessions for the shared EV Commander instance.

Business data lives in one SQLite database and every business table carries a
``tenant_id`` column. The active tenant is kept in a context variable:

* the HTTP layer sets it for each request (local preview uses the demo tenant);
* background jobs enter it with :func:`scope`;
* command-line tools run as the demo tenant unless they manage tenants/users.

The hosted login reuses the existing invitation page and cookie. Accounts are
opened by an administrator with ``tenant_admin.py``; there is no self-service
registration.
"""

from contextlib import contextmanager
import base64
import contextvars
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
import struct
import threading
import time
from urllib.parse import quote
from uuid import uuid4


DEFAULT_TENANT = "demo"
DEFAULT_TENANT_NAME = "演示租户"
SESSION_SECONDS = 8 * 3600
ROLES = {"admin", "owner", "member"}
USERNAME_LIMIT = 64

_identity = contextvars.ContextVar("ev_identity", default=None)
_migrated = set()
_migration_lock = threading.Lock()


# Tables that only need ``ALTER TABLE ... ADD COLUMN tenant_id`` on upgrade.
BUSINESS_TABLES = (
    "conversations",
    "crm_customers",
    "crm_followups",
    "crm_imports",
    "inspirations",
    "geo_projects",
    "geo_questions",
    "geo_runs",
    "geo_answers",
    "pay_orders",
    "pay_bills",
    "pay_assets",
    "dispatch_plans",
    "operations_reports",
    "operations_boards",
    "knowledge_books",
    "knowledge_text",
    "material_folders",
    "material_files",
    "designer_images",
)

# Tables whose old constraints (single row, global unique key, day primary key)
# do not fit one row per tenant. They are rebuilt during migration.
REBUILT_TABLES = {
    "pay_account": (
        """CREATE TABLE pay_account (
            tenant_id TEXT PRIMARY KEY, power REAL NOT NULL,
            commission REAL NOT NULL DEFAULT 0, total_recharge REAL NOT NULL DEFAULT 0)""",
        """INSERT INTO pay_account (tenant_id,power,commission,total_recharge)
           SELECT 'demo',power,commission,total_recharge FROM pay_account_legacy""",
    ),
    "acquisition_profile": (
        """CREATE TABLE acquisition_profile (tenant_id TEXT PRIMARY KEY, data TEXT NOT NULL)""",
        """INSERT INTO acquisition_profile (tenant_id,data)
           SELECT 'demo',data FROM acquisition_profile_legacy WHERE id=1""",
    ),
    "commander_memory": (
        """CREATE TABLE commander_memory (
            id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, content TEXT NOT NULL,
            kind TEXT NOT NULL, salience INTEGER NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(tenant_id, content))""",
        """INSERT INTO commander_memory (id,tenant_id,content,kind,salience,updated_at)
           SELECT id,'demo',content,kind,salience,updated_at FROM commander_memory_legacy""",
    ),
    "trial_usage": (
        """CREATE TABLE trial_usage (
            tenant_id TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,
            PRIMARY KEY(tenant_id, day))""",
        """INSERT INTO trial_usage (tenant_id,day,count)
           SELECT 'demo',day,count FROM trial_usage_legacy""",
    ),
    "image_usage": (
        """CREATE TABLE image_usage (
            tenant_id TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,
            PRIMARY KEY(tenant_id, day))""",
        """INSERT INTO image_usage (tenant_id,day,count)
           SELECT 'demo',day,count FROM image_usage_legacy""",
    ),
    "operations_projects": (
        """CREATE TABLE operations_projects (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            details TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(tenant_id, fingerprint))""",
        """INSERT INTO operations_projects (id,tenant_id,fingerprint,details,updated_at)
           SELECT id,'demo',fingerprint,details,updated_at FROM operations_projects_legacy""",
    ),
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_identity():
    return {"tenant_id": DEFAULT_TENANT, "user_id": "local",
            "username": "本地演示", "role": "admin"}


def anonymous_identity():
    return {"tenant_id": DEFAULT_TENANT, "user_id": "anonymous",
            "username": "未登录", "role": "member"}


def current_identity():
    identity = _identity.get()
    return identity or anonymous_identity()


def tenant_id():
    return current_identity()["tenant_id"]


def is_admin():
    return current_identity().get("role") == "admin"


def is_owner():
    return current_identity().get("role") == "owner"


def activate(identity):
    """Set the identity for the current request/thread; ``None`` resets it."""
    _identity.set(identity or None)


@contextmanager
def scope(tenant, user_id="system", username="系统任务", role="member"):
    token = _identity.set({"tenant_id": tenant, "user_id": user_id,
                           "username": username, "role": role})
    try:
        yield
    finally:
        _identity.reset(token)


def with_requested_tenant(identity, requested):
    """Administrators may switch tenants with the X-Tenant header."""
    requested = (requested or "").strip()
    if requested and identity.get("role") == "admin" and tenant_exists(requested):
        return {**identity, "tenant_id": requested}
    return identity


def database():
    # Imported lazily: commander imports tenancy for schema migration.
    import commander
    return commander.database()


# --------------------------------------------------------------------------
# Schema

def _core_tables(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS tenants (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
        max_users INTEGER NOT NULL DEFAULT 5, created_at TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, username TEXT NOT NULL UNIQUE,
        salt TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
        status TEXT NOT NULL DEFAULT 'active', totp_secret TEXT NOT NULL DEFAULT '',
        totp_pending TEXT NOT NULL DEFAULT '',
        totp_enabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
    _add_column(connection, "users", "totp_secret", "TEXT NOT NULL DEFAULT ''")
    _add_column(connection, "users", "totp_pending", "TEXT NOT NULL DEFAULT ''")
    _add_column(connection, "users", "totp_enabled", "INTEGER NOT NULL DEFAULT 0")
    connection.execute("""CREATE TABLE IF NOT EXISTS login_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL, success INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '',
        ip TEXT NOT NULL DEFAULT '', at TEXT NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS login_log_user ON login_log (username, at)")
    connection.execute("CREATE INDEX IF NOT EXISTS login_log_tenant ON login_log (tenant_id, at)")
    connection.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        expires_at INTEGER NOT NULL, created_at TEXT NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions (user_id)")
    connection.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        actor_user_id TEXT NOT NULL DEFAULT '', actor_username TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS audit_by_tenant ON audit_log (tenant_id, id)")
    # An existing single-tenant database has a tenants table without max_users.
    had_limit = "max_users" in _columns(connection, "tenants")
    _add_column(connection, "tenants", "max_users", "INTEGER NOT NULL DEFAULT 5")
    if not had_limit:
        connection.execute("UPDATE tenants SET max_users=50 WHERE id=?", (DEFAULT_TENANT,))
    connection.execute(
        "INSERT OR IGNORE INTO tenants (id,name,status,max_users,created_at) "
        "VALUES (?,?, 'active', 50, ?)",
        (DEFAULT_TENANT, DEFAULT_TENANT_NAME, now()),
    )


def _columns(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _table_exists(connection, table):
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _add_column(connection, table, column, definition):
    if not _table_exists(connection, table) or column in _columns(connection, table):
        return
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _add_tenant_column(connection, table):
    if not _table_exists(connection, table):
        return
    if "tenant_id" in _columns(connection, table):
        return
    connection.execute(
        f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT '{DEFAULT_TENANT}'"
    )


def _rebuild(connection, table, create_sql, copy_sql):
    if not _table_exists(connection, table):
        return
    if "tenant_id" in _columns(connection, table):
        return
    connection.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
    connection.execute(create_sql)
    connection.execute(copy_sql)
    connection.execute(f"DROP TABLE {table}_legacy")


def migrate(connection, path):
    """Create tenancy tables and upgrade an existing single-tenant database."""
    key = str(path)
    if key in _migrated:
        return
    with _migration_lock:
        if key in _migrated:
            return
        _core_tables(connection)
        for table, (create_sql, copy_sql) in REBUILT_TABLES.items():
            _rebuild(connection, table, create_sql, copy_sql)
        for table in BUSINESS_TABLES:
            _add_tenant_column(connection, table)
        # Commit the schema/data moves before callers open their own
        # ``BEGIN IMMEDIATE`` transactions on this connection.
        connection.commit()
        _migrated.add(key)


# --------------------------------------------------------------------------
# Accounts

def hash_password(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)


def _token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _audit(connection, tenant, action, target="", detail=""):
    actor = current_identity()
    connection.execute(
        "INSERT INTO audit_log (tenant_id,actor_user_id,actor_username,action,target,detail,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (tenant, actor.get("user_id", ""), actor.get("username", ""),
         action[:64], str(target)[:200], str(detail)[:1000], now()),
    )


def audit(tenant, action, target="", detail=""):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _audit(connection, tenant, action, target, detail)


def audit_in(connection, tenant, action, target="", detail=""):
    """Record a business audit event inside the caller's transaction."""
    _audit(connection, tenant, action, target, detail)


def audit_log(tenant=None, limit=200):
    try:
        limit = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    with database() as connection:
        if tenant:
            rows = connection.execute(
                "SELECT * FROM audit_log WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant, limit)).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


def _max_users(value, default=5):
    try:
        number = int(value if value is not None else default)
    except (TypeError, ValueError) as error:
        raise ValueError("账号上限需为 1 到 1000 的数字。") from error
    if not 1 <= number <= 1000:
        raise ValueError("账号上限需为 1 到 1000 的数字。")
    return number


def generate_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_key(secret):
    padded = secret.strip().upper()
    padded += "=" * ((8 - len(padded) % 8) % 8)
    return base64.b32decode(padded)


def totp_code(secret, at=None):
    counter = int((time.time() if at is None else at) // 30)
    digest = hmac.new(_totp_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


def verify_totp(secret, code, window=1):
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code.strip()):
        return False
    current = int(time.time())
    code = code.strip()
    return any(hmac.compare_digest(totp_code(secret, current + delta * 30), code)
               for delta in range(-window, window + 1))


def totp_uri(username, secret):
    issuer = "EV指挥官"
    label = quote(f"{issuer}:{username}", safe="")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"
            "&algorithm=SHA1&digits=6&period=30")


def setup_totp(username):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = user_by_username(connection, (username or "").strip())
        if row is None:
            raise ValueError("账号不存在。")
        secret = generate_totp_secret()
        connection.execute("UPDATE users SET totp_pending=? WHERE id=?", (secret, row["id"]))
        _audit(connection, row["tenant_id"], "user.totp_setup", row["username"])
        return {"secret": secret, "uri": totp_uri(row["username"], secret)}


def enable_totp(username, code):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = user_by_username(connection, (username or "").strip())
        if row is None:
            raise ValueError("账号不存在。")
        secret = row["totp_pending"] or row["totp_secret"]
        if not verify_totp(secret, code):
            raise ValueError("动态验证码不正确。")
        connection.execute(
            "UPDATE users SET totp_secret=?,totp_pending='',totp_enabled=1 WHERE id=?",
            (secret, row["id"]))
        _audit(connection, row["tenant_id"], "user.totp_enable", row["username"])


def disable_totp(username, code):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = user_by_username(connection, (username or "").strip())
        if row is None:
            raise ValueError("账号不存在。")
        if not verify_totp(row["totp_secret"], code):
            raise ValueError("动态验证码不正确。")
        connection.execute(
            "UPDATE users SET totp_secret='',totp_pending='',totp_enabled=0 WHERE id=?",
            (row["id"],))
        _audit(connection, row["tenant_id"], "user.totp_disable", row["username"])


def reset_totp(username):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = user_by_username(connection, (username or "").strip())
        if row is None:
            raise ValueError("账号不存在。")
        connection.execute(
            "UPDATE users SET totp_secret='',totp_pending='',totp_enabled=0 WHERE id=?",
            (row["id"],))
        _audit(connection, row["tenant_id"], "user.totp_reset", row["username"])


def user_security(username):
    with database() as connection:
        row = user_by_username(connection, (username or "").strip())
        if row is None:
            raise ValueError("账号不存在。")
        return {"username": row["username"], "totp_enabled": bool(row["totp_enabled"])}


def _login_locked(connection, username):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec="seconds")
    last_success = connection.execute(
        "SELECT max(at) FROM login_log WHERE username=? AND success=1", (username,)
    ).fetchone()[0] or ""
    failures = connection.execute(
        "SELECT COUNT(*) FROM login_log WHERE username=? AND success=0 AND at>=? AND at>?",
        (username, cutoff, last_success)).fetchone()[0]
    return failures >= 5


def record_login(connection, tenant, username, success, ip="", reason=""):
    connection.execute(
        "INSERT INTO login_log (tenant_id,username,success,reason,ip,at) VALUES (?,?,?,?,?,?)",
        (tenant or "", (username or "")[:64], 1 if success else 0,
         (reason or "")[:64], (ip or "")[:64], now()))


def login_history(tenant=None, limit=200):
    try:
        limit = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    with database() as connection:
        if tenant:
            rows = connection.execute(
                "SELECT * FROM login_log WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant, limit)).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM login_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


def unlock_user(username):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = user_by_username(connection, (username or "").strip())
        if row is None:
            raise ValueError("账号不存在。")
        connection.execute("DELETE FROM login_log WHERE username=? AND success=0",
                           (row["username"],))
        _audit(connection, row["tenant_id"], "user.unlock", row["username"])


def create_tenant(name, max_users=5):
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise ValueError("请填写 1 到 64 字的租户名称。")
    max_users = _max_users(max_users)
    key = uuid4().hex
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO tenants (id,name,status,max_users,created_at) VALUES (?,?,?,?,?)",
            (key, name, "active", max_users, now()))
        _audit(connection, key, "tenant.create", key, f"name={name}; max_users={max_users}")
    return key


def update_tenant(key, name=None, status=None, max_users=None):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tenants WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("租户不存在。")
        new_name = row["name"] if name is None else (name or "").strip()
        if not new_name or len(new_name) > 64:
            raise ValueError("请填写 1 到 64 字的租户名称。")
        new_status = row["status"] if status is None else status
        if new_status not in {"active", "disabled"}:
            raise ValueError("租户状态无效。")
        new_max = row["max_users"] if max_users is None else _max_users(max_users)
        connection.execute(
            "UPDATE tenants SET name=?,status=?,max_users=? WHERE id=?",
            (new_name, new_status, new_max, key))
        if new_status == "disabled":
            connection.execute("DELETE FROM sessions WHERE tenant_id=?", (key,))
        _audit(connection, key, "tenant.update", key,
               f"name={new_name}; status={new_status}; max_users={new_max}")
        return dict(connection.execute("SELECT * FROM tenants WHERE id=?", (key,)).fetchone())


def tenant_exists(key):
    if not key:
        return False
    with database() as connection:
        return bool(connection.execute(
            "SELECT 1 FROM tenants WHERE id=? AND status='active'", (key,)
        ).fetchone())


def tenants():
    with database() as connection:
        rows = connection.execute(
            "SELECT * FROM tenants ORDER BY created_at, id").fetchall()
        return [dict(row) for row in rows]


def user_row(connection, key):
    return connection.execute("SELECT * FROM users WHERE id=?", (key,)).fetchone()


def user_by_username(connection, username):
    return connection.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def create_user(tenant, username, password, role="member"):
    username = (username or "").strip()
    if not username or len(username) > USERNAME_LIMIT or any(ch.isspace() for ch in username):
        raise ValueError("账号需为 1 到 64 个不含空白的字符。")
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ValueError("口令需为 8 到 128 个字符。")
    if role not in ROLES:
        raise ValueError("账号角色无效。")
    salt = secrets.token_bytes(16)
    digest = hash_password(password, salt)
    key = uuid4().hex
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        tenant_row = connection.execute("SELECT * FROM tenants WHERE id=?", (tenant,)).fetchone()
        if tenant_row is None or tenant_row["status"] != "active":
            raise ValueError("租户不存在或已停用。")
        active = connection.execute(
            "SELECT COUNT(*) FROM users WHERE tenant_id=? AND status='active'", (tenant,)
        ).fetchone()[0]
        if active >= int(tenant_row["max_users"]):
            raise ValueError(f"这个租户的账号数已达上限（{tenant_row['max_users']}）。")
        try:
            connection.execute(
                "INSERT INTO users (id,tenant_id,username,salt,password_hash,role,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (key, tenant, username, salt.hex(), digest.hex(), role, "active", now()),
            )
        except Exception as error:
            if "UNIQUE" in str(error):
                raise ValueError("这个账号已存在。") from error
            raise
        _audit(connection, tenant, "user.create", username, f"role={role}")
    return key


def users(tenant=None):
    with database() as connection:
        if tenant:
            rows = connection.execute(
                "SELECT id,tenant_id,username,role,status,totp_enabled,created_at FROM users "
                "WHERE tenant_id=? ORDER BY created_at, id", (tenant,)).fetchall()
        else:
            rows = connection.execute(
                "SELECT id,tenant_id,username,role,status,totp_enabled,created_at FROM users "
                "ORDER BY tenant_id, created_at, id").fetchall()
        return [dict(row) for row in rows]


def set_password(username, password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ValueError("口令需为 8 到 128 个字符。")
    salt = secrets.token_bytes(16)
    digest = hash_password(password, salt)
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = user_by_username(connection, (username or "").strip())
        if row is None:
            raise ValueError("账号不存在。")
        connection.execute(
            "UPDATE users SET salt=?, password_hash=? WHERE id=?",
            (salt.hex(), digest.hex(), row["id"]),
        )
        connection.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        _audit(connection, row["tenant_id"], "user.password", row["username"])


def set_user_status(username, status):
    if status not in {"active", "disabled"}:
        raise ValueError("账号状态无效。")
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = user_by_username(connection, (username or "").strip())
        if row is None:
            raise ValueError("账号不存在。")
        if status == "disabled" and row["role"] == "admin":
            others = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND status='active' AND id!=?",
                (row["id"],)).fetchone()[0]
            if not others:
                raise ValueError("至少需要保留一个启用的平台管理员。")
        connection.execute("UPDATE users SET status=? WHERE id=?", (status, row["id"]))
        if status == "disabled":
            connection.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        _audit(connection, row["tenant_id"], f"user.{status}", row["username"])


def set_user_role(username, role):
    if role not in ROLES:
        raise ValueError("账号角色无效。")
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = user_by_username(connection, (username or "").strip())
        if row is None:
            raise ValueError("账号不存在。")
        if row["role"] == "admin" and role != "admin":
            others = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND status='active' AND id!=?",
                (row["id"],)).fetchone()[0]
            if not others:
                raise ValueError("至少需要保留一个启用的平台管理员。")
        connection.execute("UPDATE users SET role=? WHERE id=?", (role, row["id"]))
        _audit(connection, row["tenant_id"], "user.role", row["username"], f"role={role}")


def seed_invite_user(username, salt_hex, password_hash_hex):
    """Move the legacy single invitation account into the users table once."""
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return False
        try:
            connection.execute(
                "INSERT INTO users (id,tenant_id,username,salt,password_hash,role,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uuid4().hex, DEFAULT_TENANT, username, salt_hex, password_hash_hex,
                 "admin", "active", now()),
            )
        except Exception as error:
            if "UNIQUE" in str(error):
                return False
            raise
    return True


def authenticate(username, password, code=None, ip=""):
    """Return ``(identity, token)`` for valid credentials, otherwise ``None``."""
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    username = username[:64]
    with database() as connection:
        row = connection.execute(
            "SELECT u.*, t.status AS tenant_status FROM users u "
            "JOIN tenants t ON t.id=u.tenant_id WHERE u.username=?", (username,)
        ).fetchone()
        tenant = row["tenant_id"] if row is not None else ""
        if _login_locked(connection, username):
            record_login(connection, tenant, username, False, ip, "locked")
            return None
        if row is None or row["status"] != "active" or row["tenant_status"] != "active":
            record_login(connection, tenant, username, False, ip, "account")
            return None
        try:
            salt = bytes.fromhex(row["salt"])
            expected = bytes.fromhex(row["password_hash"])
        except ValueError:
            record_login(connection, tenant, username, False, ip, "account")
            return None
        digest = hash_password(password, salt)
        if not hmac.compare_digest(digest, expected):
            record_login(connection, tenant, username, False, ip, "password")
            return None
        if row["totp_enabled"]:
            if not verify_totp(row["totp_secret"], code):
                record_login(connection, tenant, username, False, ip, "totp")
                return None
        record_login(connection, tenant, username, True, ip)
    token = secrets.token_hex(32)
    stamp = int(time.time())
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM sessions WHERE expires_at < ?", (stamp,))
        connection.execute(
            "INSERT INTO sessions (token_hash,user_id,tenant_id,expires_at,created_at) "
            "VALUES (?,?,?,?,?)",
            (_token_hash(token), row["id"], row["tenant_id"], stamp + SESSION_SECONDS, now()),
        )
    return {"tenant_id": row["tenant_id"], "user_id": row["id"],
            "username": row["username"], "role": row["role"]}, token


def session_identity(token):
    if not isinstance(token, str) or not token:
        return None
    with database() as connection:
        row = connection.execute(
            "SELECT s.expires_at, u.id AS user_id, u.username, u.role, u.status AS user_status, "
            "u.tenant_id, t.status AS tenant_status FROM sessions s "
            "JOIN users u ON u.id=s.user_id JOIN tenants t ON t.id=u.tenant_id "
            "WHERE s.token_hash=?", (_token_hash(token),)
        ).fetchone()
    if (row is None or row["expires_at"] <= int(time.time())
            or row["user_status"] != "active" or row["tenant_status"] != "active"):
        return None
    return {"tenant_id": row["tenant_id"], "user_id": row["user_id"],
            "username": row["username"], "role": row["role"]}


def sign_out(token):
    if not isinstance(token, str) or not token:
        return
    with database() as connection:
        connection.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
