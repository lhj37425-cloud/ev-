"""Recharge products, orders, bills, cards and the local power balance.

The archived frontend is unchanged. This module supplies the direct response
shapes it expects (arrays, {list,total}, {status}). EV_PAY_MOCK defaults to 0.
Set EV_PAY_MOCK=1 explicitly for local or test payment auto-completion.
"""

from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import math
import os
import secrets

import commander
import tenancy


PRODUCTS = "/api/pay/power-products"
CHANNELS = "/api/pay/channels"
RECHARGE = "/api/pay/recharge"
SKILL_PLAN = "/api/pay/skill-plan"
QUERY = "/api/pay/query"
ORDERS = "/api/order/list"
ORDER_DETAIL = "/api/order/detail"
ORDER_CANCEL = "/api/order/cancel"
BILLS = "/api/bill/list"
CARD_EXCHANGE = "/api/card/exchange"
ASSETS = "/api/asset/list"
ASSET_CREATE = "/api/asset/create"
ASSET_DELETE = "/api/asset/delete"
USER_ASSET = "/api/user/asset"
PERSONAL_CENTER = "/api/personal/center"
POWER_PRICES = "/api/personal/power-prices"
READ = {PRODUCTS, CHANNELS, QUERY, ORDERS, ORDER_DETAIL, BILLS, ASSETS, USER_ASSET, PERSONAL_CENTER}
WRITE = {RECHARGE, SKILL_PLAN, ORDER_CANCEL, CARD_EXCHANGE, ASSET_CREATE, ASSET_DELETE,
         POWER_PRICES}
DISPLAY_TZ = timezone(timedelta(hours=8))
STATUS_TEXT = {0: "待支付", 1: "已支付", 2: "已退款", 3: "已关闭"}
POWER_RATE = 0.1  # 1 算力约合 ¥0.1，用于消费账单的金额折算


def _aliases(path):
    tail = path[len("/api"):] if path.startswith("/api") else path
    return {path, "/agent-api" + path, "/agent-api" + tail}


READ_PATHS = set().union(*(_aliases(path) for path in READ))
WRITE_PATHS = set().union(*(_aliases(path) for path in WRITE))


def canonical(path):
    if path.startswith("/agent-api"):
        path = path[len("/agent-api"):]
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def display_time(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


def _price(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(0.0, round(value, 4))


def input_token_price():
    return _price("EV_PRICE_INPUT_PER_1K", 0.05)


def output_token_price():
    return _price("EV_PRICE_OUTPUT_PER_1K", 0.2)


def image_price():
    return _price("EV_PRICE_IMAGE", 2.0)


def geo_answer_price():
    return _price("EV_PRICE_GEO_ANSWER", 0.2)


def charging_enabled():
    """Trial deployments use the daily trial quota instead of a wallet."""
    if os.environ.get("EV_POWER_CHARGE", "1") == "0":
        return False
    if os.environ.get("EV_TRIAL_ORIGIN"):
        return False
    return True


def _power_value(value):
    value = round(float(value), 4)
    return int(value) if value == int(value) else value


def _money(power):
    value = round(float(power) * POWER_RATE, 2)
    if value == 0 and power > 0:
        return round(float(power) * POWER_RATE, 4)
    return value


def _wide(char):
    code = ord(char)
    return (
        0x2E80 <= code <= 0x9FFF      # CJK radicals, kana, unified ideographs
        or 0xAC00 <= code <= 0xD7AF   # Hangul
        or 0xF900 <= code <= 0xFAFF   # CJK compatibility ideographs
        or 0xFF00 <= code <= 0xFFEF   # fullwidth forms
    )


def estimate_text_tokens(text):
    """Conservative local token estimate used when the provider has no usage."""
    if not isinstance(text, str):
        text = str(text or "")
    wide = sum(1 for char in text if _wide(char))
    return int(math.ceil(wide + (len(text) - wide) / 4.0))


def estimate_message_tokens(messages):
    total = 0
    for item in messages or []:
        if isinstance(item, dict):
            total += estimate_text_tokens(item.get("content") or "")
            total += 4  # per-message role/framing overhead
    return total


def token_charge(prompt_tokens, completion_tokens):
    prompt_tokens = max(0.0, float(prompt_tokens or 0))
    completion_tokens = max(0.0, float(completion_tokens or 0))
    power = (
        prompt_tokens / 1000.0 * input_token_price()
        + completion_tokens / 1000.0 * output_token_price()
    )
    return round(power, 4)


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS pay_products (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, power_amount INTEGER NOT NULL,
        sell_price REAL NOT NULL, original_price REAL NOT NULL, tag TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS pay_channels (
        key TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS pay_orders (
        order_no TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, product_id INTEGER, product_name TEXT NOT NULL,
        order_type INTEGER NOT NULL DEFAULT 1, amount REAL NOT NULL, power_amount INTEGER NOT NULL DEFAULT 0,
        channel TEXT NOT NULL, channel_name TEXT NOT NULL, pay_status INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, paid_at TEXT, closed_at TEXT, target TEXT NOT NULL DEFAULT ''
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS pay_bills (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, order_no TEXT, kind TEXT NOT NULL,
        amount REAL NOT NULL DEFAULT 0, power_amount INTEGER NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS pay_cards (
        code TEXT PRIMARY KEY, power_amount INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0,
        used_at TEXT
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS pay_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'other', amount REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS pay_account (
        tenant_id TEXT PRIMARY KEY, power REAL NOT NULL, commission REAL NOT NULL DEFAULT 0,
        total_recharge REAL NOT NULL DEFAULT 0
    )""")
    connection.execute(
        "INSERT OR IGNORE INTO pay_account (tenant_id,power,commission,total_recharge) VALUES (?,100,0,0)",
        (commander.tenant(),),
    )
    products = [
        (1, "100 算力", 100, 10.0, 12.0, "入门", 1, 1),
        (2, "500 算力", 500, 45.0, 60.0, "热门", 1, 2),
        (3, "1000 算力", 1000, 88.0, 120.0, "超值", 1, 3),
        (4, "5000 算力", 5000, 400.0, 600.0, "企业", 1, 4),
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO pay_products (id,name,power_amount,sell_price,original_price,tag,active,sort_order) "
        "VALUES (?,?,?,?,?,?,?,?)", products,
    )
    connection.executemany(
        "INSERT OR IGNORE INTO pay_channels (key,name,enabled,sort_order) VALUES (?,?,?,?)",
        [("alipay", "支付宝", 1, 1), ("wechat", "微信支付", 1, 2)],
    )
    if os.environ.get("EV_PAY_MOCK", "0") == "1" or os.environ.get("EV_SEED_DEMO") == "1":
        connection.execute("INSERT OR IGNORE INTO pay_cards (code,power_amount,used) VALUES ('EVOL-100',100,0)")


def _connection():
    return commander.database()


def account():
    with _connection() as db:
        initialize(db)
        row = db.execute("SELECT * FROM pay_account WHERE tenant_id=?", (commander.tenant(),)).fetchone()
    return {
        "balance_power": float(row["power"]), "power": float(row["power"]),
        "balance_commission": float(row["commission"]), "freeze_commission": 0,
        "total_recharge": float(row["total_recharge"]),
    }


def balance_in(db):
    initialize(db)
    row = db.execute("SELECT power FROM pay_account WHERE tenant_id=?",
                     (commander.tenant(),)).fetchone()
    return float(row["power"]) if row else 0.0


def balance():
    with _connection() as db:
        return balance_in(db)


def charge_in(db, power, note, ref=""):
    """Deduct power inside an existing transaction; returns the bill id."""
    if not charging_enabled():
        return 0
    power = round(float(power), 4)
    if power <= 0:
        return 0
    initialize(db)
    tenant = commander.tenant()
    changed = db.execute(
        "UPDATE pay_account SET power=power-? WHERE tenant_id=? AND power>=?",
        (power, tenant, power),
    )
    if changed.rowcount != 1:
        raise commander.ChatError("算力余额不足，请先充值。")
    cursor = db.execute(
        "INSERT INTO pay_bills (tenant_id,order_no,kind,amount,power_amount,note,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (tenant, ref or None, "consume", _money(power), power, note[:120], now()),
    )
    tenancy.audit_in(db, tenant, "power.consume", ref or str(cursor.lastrowid),
                     f"power={power}; note={note[:60]}")
    return cursor.lastrowid


def charge(power, note, ref=""):
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        return charge_in(db, power, note, ref)


def adjust_in(db, bill_id, actual, note=None):
    """Settle a reservation down to the actual power used (refund the rest)."""
    if not bill_id:
        return
    actual = round(max(0.0, float(actual)), 4)
    tenant = commander.tenant()
    row = db.execute(
        "SELECT * FROM pay_bills WHERE id=? AND tenant_id=? AND kind='consume'",
        (int(bill_id), tenant),
    ).fetchone()
    if row is None:
        return
    reserved = round(float(row["power_amount"]), 4)
    actual = min(actual, reserved)
    refund = round(reserved - actual, 4)
    if refund <= 0:
        return
    db.execute("UPDATE pay_account SET power=power+? WHERE tenant_id=?", (refund, tenant))
    if actual <= 0:
        db.execute("DELETE FROM pay_bills WHERE id=? AND tenant_id=?", (int(bill_id), tenant))
    else:
        db.execute(
            "UPDATE pay_bills SET power_amount=?,amount=?,note=? WHERE id=? AND tenant_id=?",
            (actual, _money(actual), (note or row["note"])[:120], int(bill_id), tenant),
        )
    tenancy.audit_in(db, tenant, "power.refund", str(bill_id), f"power={refund}")


def refund(bill_id):
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        adjust_in(db, bill_id, 0)


def chat_reserve(messages, max_output_tokens=1024, ref=""):
    """Reserve the worst-case chat cost before calling the provider."""
    reserve = token_charge(estimate_message_tokens(messages), max_output_tokens)
    return charge(reserve, "对话消费", ref)


def _usage_value(usage, *keys):
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def settle_chat(bill_id, usage, messages, reply_text, label="对话消费"):
    """Settle a chat reservation from provider usage, or local estimates."""
    if not bill_id:
        return 0.0
    prompt = _usage_value(usage, "prompt_tokens", "input_tokens")
    completion = _usage_value(usage, "completion_tokens", "output_tokens")
    if prompt is None:
        prompt = estimate_message_tokens(messages)
    if completion is None:
        completion = estimate_text_tokens(reply_text)
    actual = token_charge(prompt, completion)
    note = f"{label}（{int(prompt)} 入 / {int(completion)} 出）"
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        adjust_in(db, bill_id, actual, note)
    return actual


def _products(db):
    rows = db.execute("SELECT * FROM pay_products WHERE active=1 ORDER BY sort_order,id").fetchall()
    return [{
        "id": row["id"], "name": row["name"], "power_amount": row["power_amount"],
        "sell_price": row["sell_price"], "original_price": row["original_price"], "tag": row["tag"],
    } for row in rows]


def _channels(db):
    rows = db.execute("SELECT * FROM pay_channels WHERE enabled=1 ORDER BY sort_order,key").fetchall()
    return [{"key": row["key"], "name": row["name"]} for row in rows]


def _order(row):
    status = int(row["pay_status"])
    order_type = int(row["order_type"])
    return {
        "order_no": row["order_no"],
        "product_name": row["product_name"],
        "order_type": order_type,
        "order_type_text": "功能订阅" if order_type == 5 else "充值算力",
        "amount": float(row["amount"]),
        "power_amount": int(row["power_amount"]),
        "pay_status": status,
        "pay_status_text": STATUS_TEXT.get(status, "未知"),
        "pay_method_text": row["channel_name"] if status == 1 else "",
        "createtime_text": display_time(row["created_at"]),
        "pay_time_text": display_time(row["paid_at"]),
        "can_pay": status == 0,
        "can_cancel": status == 0,
    }


def _bill(row):
    kind = row["kind"]
    direction = "income" if kind in {"recharge", "card", "commission", "refund", "signin"} else "expense"
    amount = float(row["amount"])
    if kind == "card":
        amount = float(row["power_amount"])
    return {
        "id": row["id"], "order_no": row["order_no"], "type": kind,
        "title": row["note"] or ("充值" if direction == "income" else "消费"),
        "direction": direction, "amount": amount, "power": _power_value(row["power_amount"]),
        "balance": "", "createtime": display_time(row["created_at"]),
        "created_at": row["created_at"],
    }


def _complete_in_db(db, order_no):
    paid_at = now()
    tenant = commander.tenant()
    changed = db.execute(
        "UPDATE pay_orders SET pay_status=1,paid_at=? WHERE order_no=? AND pay_status=0 AND tenant_id=?",
        (paid_at, order_no, tenant),
    )
    if changed.rowcount != 1:
        return
    row = db.execute("SELECT * FROM pay_orders WHERE order_no=? AND tenant_id=?",
                     (order_no, tenant)).fetchone()
    db.execute("UPDATE pay_account SET power=power+?, total_recharge=total_recharge+? WHERE tenant_id=?",
               (row["power_amount"], row["amount"], tenant))
    db.execute("INSERT INTO pay_bills (tenant_id,order_no,kind,amount,power_amount,note,created_at) "
               "VALUES (?,?,?,?,?,?,?)",
               (tenant, order_no, "recharge", row["amount"], row["power_amount"], row["product_name"], paid_at))
    tenancy.audit_in(db, tenant, "order.paid", order_no,
                     f"amount={row['amount']}; power={row['power_amount']}")


def _complete(order_no):
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        _complete_in_db(db, order_no)


def _maybe_mock_pay(db, order_no):
    if os.environ.get("EV_PAY_MOCK", "0") != "1":
        return
    row = db.execute("SELECT * FROM pay_orders WHERE order_no=? AND tenant_id=?",
                     (order_no, commander.tenant())).fetchone()
    if row is None or int(row["pay_status"]) != 0:
        return
    try:
        created = datetime.fromisoformat(row["created_at"])
    except ValueError:
        return
    if datetime.now(timezone.utc) - created >= timedelta(seconds=6):
        _complete_in_db(db, order_no)


def _new_order_no():
    return "R" + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2).upper()


def read(path, query):
    path = canonical(path)
    with _connection() as db:
        initialize(db)
        if path == PRODUCTS:
            return _products(db)
        if path == CHANNELS:
            return _channels(db)
        if path == USER_ASSET or path == PERSONAL_CENTER:
            row = db.execute("SELECT * FROM pay_account WHERE tenant_id=?",
                             (commander.tenant(),)).fetchone()
            return {
                "balance_power": float(row["power"]), "power": float(row["power"]),
                "balance_commission": float(row["commission"]), "freeze_commission": 0,
                "total_recharge": float(row["total_recharge"]),
            }
        if path == QUERY:
            order_no = (query.get("order_no") or [""])[0]
            _maybe_mock_pay(db, order_no)
            row = db.execute("SELECT pay_status FROM pay_orders WHERE order_no=? AND tenant_id=?",
                             (order_no, commander.tenant())).fetchone()
            if row is None:
                raise commander.ChatError("订单不存在。")
            status = int(row["pay_status"])
            return {"order_no": order_no, "status": {0: "pending", 1: "paid", 2: "refunded", 3: "closed"}.get(status, "pending")}
        if path == ORDERS:
            try:
                page = max(1, int((query.get("page") or ["1"])[0]))
                size = max(1, min(100, int((query.get("pageSize") or query.get("page_size") or ["20"])[0])))
            except ValueError as error:
                raise commander.ChatError("分页参数无效。") from error
            status = (query.get("pay_status") or [""])[0]
            where, values = "WHERE tenant_id=?", [commander.tenant()]
            if status not in ("", None):
                try:
                    where += " AND pay_status=?"
                    values.append(int(status))
                except ValueError as error:
                    raise commander.ChatError("订单状态无效。") from error
            for row in db.execute(f"SELECT * FROM pay_orders {where}", values).fetchall():
                _maybe_mock_pay(db, row["order_no"])
            rows = db.execute(
                f"SELECT * FROM pay_orders {where} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                [*values, size, (page - 1) * size],
            ).fetchall()
            total = db.execute(f"SELECT COUNT(*) FROM pay_orders {where}", values).fetchone()[0]
            return {"list": [_order(row) for row in rows], "total": total}
        if path == ORDER_DETAIL:
            order_no = (query.get("order_no") or [""])[0]
            _maybe_mock_pay(db, order_no)
            row = db.execute("SELECT * FROM pay_orders WHERE order_no=? AND tenant_id=?",
                             (order_no, commander.tenant())).fetchone()
            if row is None:
                raise commander.ChatError("订单不存在。")
            return _order(row)
        if path == BILLS:
            try:
                page = max(1, int((query.get("page") or ["1"])[0]))
                size = max(1, min(100, int((query.get("pageSize") or query.get("page_size") or ["20"])[0])))
            except ValueError as error:
                raise commander.ChatError("分页参数无效。") from error
            kind = (query.get("type") or [""])[0]
            keyword = (query.get("keyword") or [""])[0].strip()
            where, values = "WHERE tenant_id=?", [commander.tenant()]
            if kind == "recharge":
                where += " AND kind IN ('recharge','card')"
            elif kind == "consume":
                where += " AND kind='consume'"
            if keyword:
                where += " AND instr(lower(note), lower(?)) > 0"
                values.append(keyword)
            rows = db.execute(
                f"SELECT * FROM pay_bills {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*values, size, (page - 1) * size],
            ).fetchall()
            total = db.execute(f"SELECT COUNT(*) FROM pay_bills {where}", values).fetchone()[0]
            all_rows = db.execute("SELECT * FROM pay_bills WHERE tenant_id=?",
                                  (commander.tenant(),)).fetchall()
            today = datetime.now(DISPLAY_TZ).date()
            yesterday = today - timedelta(days=1)
            total_recharge = sum(float(row["amount"]) for row in all_rows if row["kind"] == "recharge")
            total_consume = sum(float(row["amount"]) for row in all_rows if row["kind"] == "consume")
            def used_on(day):
                total = 0.0
                for row in all_rows:
                    if row["kind"] != "consume":
                        continue
                    try:
                        when = datetime.fromisoformat(row["created_at"]).astimezone(DISPLAY_TZ).date()
                    except ValueError:
                        continue
                    if when == day:
                        total += float(row["power_amount"])
                return _power_value(total)
            return {
                "list": [_bill(row) for row in rows], "total": total,
                "today_used": used_on(today), "yesterday_used": used_on(yesterday),
                "total_recharge": total_recharge, "total_consume": total_consume,
            }
        if path == ASSETS:
            rows = db.execute("SELECT * FROM pay_assets WHERE tenant_id=? ORDER BY id DESC",
                              (commander.tenant(),)).fetchall()
            return {"list": [dict(row) for row in rows], "total": len(rows)}
    raise commander.ChatError("接口不存在。")


def power_prices(body):
    """Per-feature prices for the frontend cost hints (read-only)."""
    items = body.get("items")
    if not isinstance(items, list):
        raise commander.ChatError("请提供要查询的功能项。")
    result = []
    for item in items[:50]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")[:32]
        key = str(item.get("key") or "")[:64]
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        if kind == "model":
            prompt = _usage_value(usage, "prompt_tokens", "input_tokens")
            completion = _usage_value(usage, "completion_tokens", "output_tokens")
            credits = (token_charge(prompt or 0, completion or 0)
                       if prompt is not None or completion is not None else 0.0)
            result.append({
                "type": kind, "key": key, "priced": True,
                "billing_type": "per_token",
                "credits_in_per_1k": input_token_price(),
                "credits_out_per_1k": output_token_price(),
                "credits": round(credits, 4),
            })
            continue
        result.append({"type": kind, "key": key, "priced": False,
                       "billing_type": "", "credits": 0})
    return {"ok": True, "list": result, "balance": balance(),
            "enforce": charging_enabled()}


def write(path, body):
    path = canonical(path)
    if path == RECHARGE or path == SKILL_PLAN:
        return _create_order(path, body)
    if path == POWER_PRICES:
        return power_prices(body)
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        if path == ORDER_CANCEL:
            order_no = body.get("order_no")
            if not isinstance(order_no, str) or not order_no:
                raise commander.ChatError("缺少订单号。")
            row = db.execute("SELECT pay_status FROM pay_orders WHERE order_no=? AND tenant_id=?",
                             (order_no, commander.tenant())).fetchone()
            if row is None:
                raise commander.ChatError("订单不存在。")
            if int(row["pay_status"]) != 0:
                raise commander.ChatError("只有待支付订单可以取消。")
            db.execute("UPDATE pay_orders SET pay_status=3,closed_at=? WHERE order_no=? AND tenant_id=?",
                       (now(), order_no, commander.tenant()))
            tenancy.audit_in(db, commander.tenant(), "order.cancel", order_no)
            return {"ok": True, "message": "订单已取消"}
        if path == CARD_EXCHANGE:
            code = body.get("card_no") or body.get("code") or body.get("card_code")
            if not isinstance(code, str) or not code.strip():
                raise commander.ChatError("请输入卡号。")
            code = code.strip().upper()
            used_at = now()
            changed = db.execute(
                "UPDATE pay_cards SET used=1,used_at=? WHERE code=? AND used=0", (used_at, code)
            )
            if changed.rowcount != 1:
                row = db.execute("SELECT used FROM pay_cards WHERE code=?", (code,)).fetchone()
                raise commander.ChatError("兑换码无效。" if row is None else "兑换码已使用。")
            row = db.execute("SELECT power_amount FROM pay_cards WHERE code=?", (code,)).fetchone()
            db.execute("UPDATE pay_account SET power=power+? WHERE tenant_id=?",
                       (row["power_amount"], commander.tenant()))
            db.execute("INSERT INTO pay_bills (tenant_id,order_no,kind,amount,power_amount,note,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (commander.tenant(), None, "card", 0, row["power_amount"],
                        "兑换码 " + code, used_at))
            tenancy.audit_in(db, commander.tenant(), "card.redeem", "",
                             f"power={row['power_amount']}")
            return {"ok": True, "power_amount": row["power_amount"],
                    "desc": f"{row['power_amount']} 算力", "message": "兑换成功"}
        if path == ASSET_CREATE:
            name = body.get("name")
            if not isinstance(name, str) or not name.strip():
                raise commander.ChatError("请输入资产名称。")
            kind = body.get("kind") if isinstance(body.get("kind"), str) else "other"
            amount = float(body.get("amount") or 0)
            cursor = db.execute(
                "INSERT INTO pay_assets (tenant_id,name,kind,amount,created_at) VALUES (?,?,?,?,?)",
                (commander.tenant(), name.strip()[:120], kind[:32], amount, now()))
            tenancy.audit_in(db, commander.tenant(), "asset.create", str(cursor.lastrowid),
                             f"kind={kind[:32]}")
            return {"ok": True, "id": cursor.lastrowid}
        if path == ASSET_DELETE:
            try:
                key = int(body.get("id"))
            except (TypeError, ValueError) as error:
                raise commander.ChatError("资产编号无效。") from error
            db.execute("DELETE FROM pay_assets WHERE id=? AND tenant_id=?",
                       (key, commander.tenant()))
            tenancy.audit_in(db, commander.tenant(), "asset.delete", str(key))
            return {"ok": True}
    raise commander.ChatError("接口不存在。")


def _create_order(path, body):
    with _connection() as db:
        initialize(db)
        channel = body.get("channel")
        if not isinstance(channel, str) or not channel:
            raise commander.ChatError("请选择支付方式。")
        channel_row = db.execute("SELECT * FROM pay_channels WHERE key=? AND enabled=1", (channel,)).fetchone()
        if channel_row is None:
            raise commander.ChatError("支付方式不可用。")
        product = None
        if path == RECHARGE and body.get("product_id") is not None:
            try:
                product = db.execute("SELECT * FROM pay_products WHERE id=? AND active=1",
                                     (int(body["product_id"]),)).fetchone()
            except (TypeError, ValueError) as error:
                raise commander.ChatError("算力包无效。") from error
            if product is None:
                raise commander.ChatError("算力包不存在。")
            amount = float(product["sell_price"])
            power = int(product["power_amount"])
            name = product["name"]
            product_id = product["id"]
            order_type = 1
        else:
            try:
                amount = round(float(body.get("amount")), 2)
            except (TypeError, ValueError) as error:
                raise commander.ChatError("充值金额无效。") from error
            if not 0 < amount <= 100000:
                raise commander.ChatError("充值金额无效。")
            power = int(amount * 10)
            name = f"{power} 算力"
            product_id = None
            order_type = 5 if path == SKILL_PLAN else 1
            if order_type == 5:
                name = "功能订阅"
                power = 0
        if os.environ.get("EV_PAY_MOCK", "0") != "1":
            raise commander.ChatError("支付渠道未配置，暂不能下单。")
        order_no = _new_order_no()
        db.execute(
            "INSERT INTO pay_orders (order_no,tenant_id,product_id,product_name,order_type,amount,power_amount,channel,channel_name,pay_status,created_at,target) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
            (order_no, commander.tenant(), product_id, name, order_type, amount, power, channel,
             channel_row["name"], now(), str(body.get("target") or "")[:32]),
        )
        tenancy.audit_in(db, commander.tenant(), "order.create", order_no,
                         f"amount={amount}; power={power}; channel={channel}")
        return {
            "order_no": order_no, "pay_type": "qrcode", "qr_code": "mockpay://" + order_no,
            "amount": amount,
        }


# ---------------------------------------------------------------------------
# Startup reconciliation: refund orphan chat reservations


def mark_image_archived_in(db, bill_id):
    """Record delivery in the same transaction as the archived work."""
    if bill_id:
        db.execute(
            "UPDATE pay_bills SET note=REPLACE(note,'生图消费：','生图消费（已归档）：') "
            "WHERE id=? AND tenant_id=? AND kind='consume' AND note LIKE '生图消费：%'",
            (int(bill_id), commander.tenant()))


def mark_image_done(bill_id):
    """Mark an image charge as completed so reconciliation will not refund it."""
    if not bill_id:
        return
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE pay_bills SET note=REPLACE(REPLACE(note,'生图消费：','生图消费（已完成）：'),"
            "'生图消费（已归档）：','生图消费（已完成）：') "
            "WHERE id=? AND tenant_id=? AND kind='consume' "
            "AND (note LIKE '生图消费：%' OR note LIKE '生图消费（已归档）：%')",
            (int(bill_id), commander.tenant()))


def reconcile_reservations(minutes=60):
    """Refund pre-charges left by a killed process.

    Chat reservations keep the exact note ``对话消费`` until settled (settled
    bills carry ``对话消费（…）``). Image charges that still carry
    ``生图消费：…`` after the timeout have no archived work. Archiving changes
    the note in the work transaction, before final settlement.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(5, int(minutes)))
              ).isoformat(timespec="seconds")
    refunded = 0
    with _connection() as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        rows = db.execute(
            "SELECT id,tenant_id FROM pay_bills WHERE kind='consume' "
            "AND (note='对话消费' OR note LIKE '生图消费：%') AND created_at<=?",
            (cutoff,)).fetchall()
        for row in rows:
            with tenancy.scope(row["tenant_id"], "reconcile", "对账", "member"):
                current = db.execute(
                    "SELECT * FROM pay_bills WHERE id=? AND tenant_id=?",
                    (row["id"], row["tenant_id"])).fetchone()
                if current is None:
                    continue
                if current["note"] == "对话消费":
                    adjust_in(db, current["id"], 0, "对话中断，预扣退回")
                elif current["note"].startswith("生图消费："):
                    adjust_in(db, current["id"], 0, "生图中断，预扣退回")
                else:
                    continue
                refunded += 1
    return refunded
