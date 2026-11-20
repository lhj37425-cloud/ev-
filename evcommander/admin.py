"""Tenant administration API and a small server-rendered admin page.

Roles:
* ``admin``  - platform administrator: all tenants, users, audits;
* ``owner``  - tenant administrator: own tenant's users and audit only;
* ``member`` - normal workspace user: no administration API.

The page is only served when ``EV_ADMIN_PATH`` is configured (must start with
``/admin``); the value itself is deployment-specific and is not committed.
Business data inside a tenant is shared by that tenant's users.
"""

import re

import commander
import tenancy


BASE = "/api/admin"
ME = BASE + "/me"
OVERVIEW = BASE + "/overview"
TENANTS = BASE + "/tenants"
TENANT_PATH = re.compile(r"/api/admin/tenants/([a-f0-9]{32})$")
USERS = BASE + "/users"
USER_PATH = re.compile(r"/api/admin/users/([a-f0-9]{32})$")
AUDIT = BASE + "/audit"
READ_PATHS = {ME, OVERVIEW, TENANTS, USERS, AUDIT}
ROLE_LABELS = {"admin": "平台管理员", "owner": "租户管理员", "member": "普通账号"}


def readable(path):
    return path in READ_PATHS


def writable(method, path):
    if method == "POST" and path in {TENANTS, USERS}:
        return True
    return method == "PUT" and bool(TENANT_PATH.fullmatch(path) or USER_PATH.fullmatch(path))


def normalize_path(value):
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    if not re.fullmatch(r"/admin(?:[-_][A-Za-z0-9_\-]*)?(?:/[A-Za-z0-9_\-]{1,64})*", value):
        raise ValueError("EV_ADMIN_PATH 必须以 /admin 开头，只含字母、数字、-、_、/。")
    return value


def _identity():
    identity = tenancy.current_identity()
    if identity.get("role") not in {"admin", "owner"}:
        raise PermissionError("需要管理员权限。")
    return identity


def _platform(identity):
    if identity.get("role") != "admin":
        raise PermissionError("只有平台管理员可以执行这个操作。")
    return identity


def _target(identity, requested=None, required=False):
    if identity["role"] == "admin":
        if required and not requested:
            raise ValueError("请选择租户。")
        return requested or None
    if requested and requested != identity["tenant_id"]:
        raise PermissionError("只能管理自己租户的数据。")
    return identity["tenant_id"]


def _tenant_row(key):
    with commander.database() as db:
        return db.execute("SELECT * FROM tenants WHERE id=?", (key,)).fetchone()


def _table_exists(db, name):
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _count(db, sql, args=()):
    try:
        return int(db.execute(sql, args).fetchone()[0] or 0)
    except Exception:
        return 0


def _tenant_stats(tenant):
    with commander.database() as db:
        users = _count(db, "SELECT COUNT(*) FROM users WHERE tenant_id=?", (tenant,))
        active = _count(db, "SELECT COUNT(*) FROM users WHERE tenant_id=? AND status='active'",
                        (tenant,))
        balance = total_recharge = orders = threads = customers = geo_projects = 0
        wa_accounts = conversations = 0
        if _table_exists(db, "pay_account"):
            row = db.execute("SELECT power,total_recharge FROM pay_account WHERE tenant_id=?",
                             (tenant,)).fetchone()
            if row:
                balance, total_recharge = float(row[0]), float(row[1])
        if _table_exists(db, "pay_orders"):
            orders = _count(db, "SELECT COUNT(*) FROM pay_orders WHERE tenant_id=?", (tenant,))
        if _table_exists(db, "crm_customers"):
            customers = _count(db, "SELECT COUNT(*) FROM crm_customers WHERE tenant_id=?", (tenant,))
        if _table_exists(db, "geo_projects"):
            geo_projects = _count(db, "SELECT COUNT(*) FROM geo_projects WHERE tenant_id=?",
                                  (tenant,))
        if _table_exists(db, "conversations"):
            conversations = _count(db, "SELECT COUNT(*) FROM conversations WHERE tenant_id=?",
                                   (tenant,))
        if _table_exists(db, "wa_accounts"):
            wa_accounts = _count(db, "SELECT COUNT(*) FROM wa_accounts WHERE tenant_id=?",
                                 (tenant,))
        if _table_exists(db, "wa_threads"):
            threads = _count(db, "SELECT COUNT(*) FROM wa_threads WHERE tenant_id=?", (tenant,))
    return {"users": users, "active_users": active, "balance": balance,
            "total_recharge": total_recharge, "orders": orders, "threads": threads,
            "customers": customers, "geo_projects": geo_projects,
            "wa_accounts": wa_accounts, "conversations": conversations}


def _tenant_view(row):
    return {
        "id": row["id"], "name": row["name"], "status": row["status"],
        "max_users": row["max_users"], "created_at": row["created_at"],
        **_tenant_stats(row["id"]),
    }


def _public_user(row):
    return {"id": row["id"], "tenant_id": row["tenant_id"], "username": row["username"],
            "role": row["role"], "role_label": ROLE_LABELS.get(row["role"], row["role"]),
            "status": row["status"], "created_at": row["created_at"]}


def _find_user(key):
    with commander.database() as db:
        return tenancy.user_row(db, key)


def _find_username(username):
    with commander.database() as db:
        return tenancy.user_by_username(db, username)


def _active_admins(exclude=None):
    with commander.database() as db:
        if exclude:
            row = db.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND status='active' AND id!=?",
                (exclude,)).fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'").fetchone()
        return int(row[0] or 0)


# --------------------------------------------------------------------------
# Reads

def _overview(identity):
    if identity["role"] == "admin":
        views = [_tenant_view(row) for row in tenancy.tenants()]
        totals = {key: sum(view[key] for view in views)
                  for key in ("users", "active_users", "orders", "threads",
                              "customers", "geo_projects", "wa_accounts", "conversations")}
        totals["balance"] = round(sum(view["balance"] for view in views), 2)
        totals["total_recharge"] = round(sum(view["total_recharge"] for view in views), 2)
        totals["tenants"] = len(views)
        return {"data": {"role": "admin", "totals": totals, "tenants": views,
                         "audit_recent": tenancy.audit_log(None, 8)}}
    row = _tenant_row(identity["tenant_id"])
    if row is None:
        raise PermissionError("租户不存在。")
    view = _tenant_view(row)
    return {"data": {"role": identity["role"], "tenant": view,
                     "audit_recent": tenancy.audit_log(identity["tenant_id"], 8)}}


def _tenant_list(identity):
    rows = tenancy.tenants()
    if identity["role"] == "owner":
        rows = [row for row in rows if row["id"] == identity["tenant_id"]]
    return {"data": [_tenant_view(row) for row in rows]}


def _user_list(identity, query):
    requested = (query.get("tenant") or [""])[0]
    tenant = _target(identity, requested)
    rows = tenancy.users(tenant)
    return {"data": [_public_user(row) for row in rows]}


def _audit_list(identity, query):
    requested = (query.get("tenant") or [""])[0]
    tenant = _target(identity, requested)
    try:
        limit = max(1, min(1000, int((query.get("limit") or ["200"])[0])))
    except ValueError:
        limit = 200
    return {"data": tenancy.audit_log(tenant, limit)}


def read(path, query):
    identity = _identity()
    if path == ME:
        return {"data": {"user_id": identity["user_id"], "username": identity["username"],
                         "role": identity["role"],
                         "role_label": ROLE_LABELS.get(identity["role"], identity["role"]),
                         "tenant_id": identity["tenant_id"]}}
    if path == OVERVIEW:
        return _overview(identity)
    if path == TENANTS:
        return _tenant_list(identity)
    if path == USERS:
        return _user_list(identity, query)
    if path == AUDIT:
        return _audit_list(identity, query)
    raise commander.ChatError("管理接口不存在。")


# --------------------------------------------------------------------------
# Writes

def _create_tenant(identity, body):
    _platform(identity)
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
        raise ValueError("请填写 1 到 64 字的租户名称。")
    key = tenancy.create_tenant(name, body.get("max_users", 5))
    row = _tenant_row(key)
    return {"ok": True, "data": _tenant_view(row)}


def _update_tenant(identity, key, body):
    _platform(identity)
    row = tenancy.update_tenant(key, name=body.get("name"), status=body.get("status"),
                                max_users=body.get("max_users"))
    return {"ok": True, "data": _tenant_view(row)}


def _create_user(identity, body):
    requested = body.get("tenant_id")
    tenant = _target(identity, requested, required=True)
    role = body.get("role") or "member"
    if identity["role"] == "owner" and role != "member":
        raise PermissionError("租户管理员只能开通普通账号。")
    username = body.get("username")
    password = body.get("password")
    key = tenancy.create_user(tenant, username, password, role)
    row = _find_user(key)
    return {"ok": True, "data": _public_user(row)}


def _update_user(identity, key, body):
    row = _find_user(key)
    if row is None:
        raise ValueError("账号不存在。")
    if identity["role"] == "owner":
        if row["tenant_id"] != identity["tenant_id"]:
            raise PermissionError("只能管理自己租户的账号。")
        if row["role"] != "member":
            raise PermissionError("租户管理员不能修改管理员账号。")
    role = body.get("role")
    status = body.get("status")
    if role is not None and role not in tenancy.ROLES:
        raise ValueError("账号角色无效。")
    if identity["role"] == "owner" and role not in (None, "member"):
        raise PermissionError("租户管理员只能设置普通账号。")
    if status is not None and status not in {"active", "disabled"}:
        raise ValueError("账号状态无效。")
    if row["id"] == identity["user_id"]:
        if status == "disabled":
            raise ValueError("不能停用当前登录账号。")
        if role is not None and role != row["role"]:
            raise ValueError("不能修改当前登录账号的角色。")
    if row["role"] == "admin" and (role not in (None, "admin") or status == "disabled"):
        if _active_admins(exclude=row["id"]) == 0:
            raise ValueError("至少需要保留一个启用的平台管理员。")
    password = body.get("password")
    if password not in (None, ""):
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise ValueError("口令需为 8 到 128 个字符。")
    if role is not None and role != row["role"]:
        tenancy.set_user_role(row["username"], role)
    if status is not None and status != row["status"]:
        tenancy.set_user_status(row["username"], status)
    if password:
        tenancy.set_password(row["username"], password)
    return {"ok": True, "data": _public_user(_find_user(key))}


def write(method, path, body):
    identity = _identity()
    if method == "POST" and path == TENANTS:
        return _create_tenant(identity, body)
    if method == "PUT" and TENANT_PATH.fullmatch(path):
        return _update_tenant(identity, TENANT_PATH.fullmatch(path)[1], body)
    if method == "POST" and path == USERS:
        return _create_user(identity, body)
    if method == "PUT" and USER_PATH.fullmatch(path):
        return _update_user(identity, USER_PATH.fullmatch(path)[1], body)
    raise commander.ChatError("管理接口不存在。")


# --------------------------------------------------------------------------
# Server-rendered page

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>EV指挥官 · 管理后台</title>
<style>
:root{--bg:#f6f3ef;--panel:#fff;--line:#e5ded7;--text:#2b2120;--muted:#776966;--brand:#971c19;--ok:#1d7a4b;--warn:#a66a00;--err:#a01c19}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,"Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 24px;background:var(--panel);border-bottom:1px solid var(--line)}
h1{margin:0;font-size:19px}h2{margin:0 0 12px;font-size:16px}
main{max-width:1180px;margin:0 auto;padding:24px}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{border:1px solid var(--line);border-radius:10px;padding:12px;background:#fcfaf8}
.card b{display:block;font-size:22px;margin-top:4px}
.muted{color:var(--muted)}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:end}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:4px}
input,select,button{font:inherit;min-height:38px;border-radius:8px;border:1px solid #c9bdb6;padding:6px 10px;background:#fff;color:inherit}
button{cursor:pointer}button.primary{background:var(--brand);border-color:var(--brand);color:#fff}
button.ghost{background:#fff}button.danger{color:var(--err)}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:14px}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}td.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--line);font-size:12px}
.badge.ok{color:var(--ok);border-color:#b9dcc9;background:#f1faf5}.badge.off{color:var(--muted);background:#f4f1ee}
.badge.bad{color:var(--err);border-color:#ecc9c8;background:#fdf3f2}
#notice{position:fixed;right:20px;bottom:20px;max-width:420px;padding:12px 14px;border-radius:10px;background:#2b2120;color:#fff;display:none;z-index:9}
dialog{border:1px solid var(--line);border-radius:12px;padding:18px;max-width:380px;width:90%}
dialog::backdrop{background:rgba(43,33,32,.35)}
.hidden{display:none!important}
@media(max-width:720px){main{padding:12px}header{padding:12px}table{display:block;overflow-x:auto}}
</style>
__LOCAL_BOOT__
</head><body>
<header><div><h1>EV指挥官 · 管理后台</h1><div class="muted" id="who">加载中…</div></div>
<div class="row"><button class="ghost" id="refresh">刷新</button><button class="ghost __LOGOUT_CLASS__" id="logout">退出登录</button></div></header>
<main>
<section><h2>概览</h2><div class="cards" id="cards"></div></section>
<section id="tenantSection" class="hidden"><h2>租户</h2>
<div class="row" style="margin-bottom:12px">
<div><label>租户名称</label><input id="tenantName" maxlength="64" placeholder="客户名称"></div>
<div><label>账号上限</label><input id="tenantMax" type="number" min="1" max="1000" value="5" style="width:110px"></div>
<button class="primary" id="createTenant">开通租户</button></div>
<table><thead><tr><th>租户</th><th>状态</th><th>账号</th><th>余额</th><th>订单</th><th>客户</th><th>GEO</th><th>WhatsApp</th><th>操作</th></tr></thead><tbody id="tenantRows"></tbody></table>
</section>
<section><h2>账号</h2>
<div class="row" style="margin-bottom:12px">
<div id="tenantPick" class="hidden"><label>租户</label><select id="userTenant"></select></div>
<div><label>账号</label><input id="newUser" maxlength="64" autocomplete="off"></div>
<div><label>初始口令</label><input id="newPass" type="password" maxlength="128" autocomplete="new-password"></div>
<div><label>角色</label><select id="newRole"><option value="member">普通账号</option><option value="owner">租户管理员</option><option value="admin">平台管理员</option></select></div>
<button class="primary" id="createUser">开通账号</button></div>
<table><thead><tr><th>账号</th><th>租户</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody id="userRows"></tbody></table>
</section>
<section><h2>审计日志</h2>
<div class="row" style="margin-bottom:8px"><div id="auditPick" class="hidden"><label>租户</label><select id="auditTenant"></select></div><button class="ghost" id="refreshAudit">刷新</button></div>
<table><thead><tr><th>时间</th><th>租户</th><th>操作人</th><th>动作</th><th>对象</th><th>详情</th></tr></thead><tbody id="auditRows"></tbody></table>
</section>
</main>
<dialog id="passDialog"><form method="dialog"><h3 style="margin:0 0 6px">重置口令</h3>
<p class="muted" style="margin:0 0 10px">为新账号设置口令（8-128 个字符）。</p>
<label>新口令</label><input id="passValue" type="password" minlength="8" maxlength="128" autocomplete="new-password" style="width:100%">
<div class="row" style="justify-content:flex-end;margin-top:12px"><button value="cancel" class="ghost">取消</button><button id="passSave" value="save" class="primary">保存</button></div>
</form></dialog>
<div id="notice"></div>
<script>
const LOCAL = __LOCAL_FLAG__;
const ROLE_LABELS = {admin:"平台管理员", owner:"租户管理员", member:"普通账号"};
let me = null;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
function notice(text, bad){ const el=$("notice"); el.textContent=text; el.style.display="block"; el.style.background=bad?"#7c1a17":"#2b2120"; clearTimeout(notice.timer); notice.timer=setTimeout(()=>el.style.display="none", 4500); }
async function api(path, options={}){
  const headers = {"Content-Type":"application/json", ...(options.headers||{})};
  const token = localStorage.getItem("token");
  if(token) headers.Authorization = "Bearer " + token;
  const response = await fetch(path, {credentials:"same-origin", ...options, headers});
  const data = await response.json().catch(()=>({}));
  if(response.status===401){ location.href="/invite"; throw new Error("请先登录"); }
  if(!response.ok) throw new Error(data.detail || data.msg || "请求失败");
  return data;
}
function card(label, value){ return `<div class="card"><span class="muted">${esc(label)}</span><b>${esc(value)}</b></div>`; }
async function loadOverview(){
  const data = (await api("/api/admin/overview")).data;
  const totals = data.role==="admin" ? data.totals : data.tenant;
  $("cards").innerHTML = [
    data.role==="admin" ? card("租户", totals.tenants) : card("账号上限", totals.max_users),
    card("账号", totals.active_users + " / " + totals.users),
    card("余额", Number(totals.balance||0).toFixed(2)),
    card("累计充值", Number(totals.total_recharge||0).toFixed(2)),
    card("订单", totals.orders),
    card("客户", totals.customers),
    card("GEO 项目", totals.geo_projects),
    card("WhatsApp 会话", totals.threads)
  ].join("");
}
async function loadTenants(){
  if(me.role!=="admin") return;
  const items = (await api("/api/admin/tenants")).data;
  $("tenantRows").innerHTML = items.map(t => `<tr>
    <td><b>${esc(t.name)}</b><div class="muted mono">${esc(t.id)}</div></td>
    <td><span class="badge ${t.status==="active"?"ok":"bad"}">${t.status==="active"?"启用":"停用"}</span></td>
    <td>${t.active_users}/${t.max_users}</td><td>${Number(t.balance||0).toFixed(2)}</td>
    <td>${t.orders}</td><td>${t.customers}</td><td>${t.geo_projects}</td><td>${t.threads}</td>
    <td><button class="ghost" onclick="editTenant('${esc(t.id)}')">编辑</button>
    <button class="${t.status==="active"?"danger":"ghost"}" onclick="toggleTenant('${esc(t.id)}','${t.status==="active"?"disabled":"active"}')">${t.status==="active"?"停用":"启用"}</button></td></tr>`).join("");
  const options = '<option value="">全部租户</option>' + items.map(t=>`<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
  for(const id of ["userTenant","auditTenant"]){ const el=$(id); const old=el.value; el.innerHTML=options; if(old) el.value=old; }
  $("tenantPick").classList.remove("hidden"); $("auditPick").classList.remove("hidden");
}
async function loadUsers(){
  const tenant = me.role==="admin" ? ($("userTenant").value||"") : "";
  const items = (await api("/api/admin/users" + (tenant?`?tenant=${encodeURIComponent(tenant)}`:""))).data;
  $("userRows").innerHTML = items.map(u => {
    const roles = me.role==="admin"
      ? `<select onchange="setRole('${esc(u.id)}',this.value)">${["member","owner","admin"].map(r=>`<option value="${r}" ${u.role===r?"selected":""}>${ROLE_LABELS[r]}</option>`).join("")}</select>`
      : esc(u.role_label);
    return `<tr><td>${esc(u.username)}</td><td class="mono">${esc(u.tenant_id)}</td><td>${roles}</td>
      <td><span class="badge ${u.status==="active"?"ok":"off"}">${u.status==="active"?"启用":"停用"}</span></td>
      <td class="muted">${esc(u.created_at)}</td>
      <td><button class="ghost" onclick="resetPassword('${esc(u.id)}')">重置口令</button>
      <button class="${u.status==="active"?"danger":"ghost"}" onclick="toggleUser('${esc(u.id)}','${u.status==="active"?"disabled":"active"}')">${u.status==="active"?"停用":"启用"}</button></td></tr>`;
  }).join("");
}
async function loadAudit(){
  const tenant = me.role==="admin" ? ($("auditTenant").value||"") : "";
  const items = (await api("/api/admin/audit?limit=200" + (tenant?`&tenant=${encodeURIComponent(tenant)}`:""))).data;
  $("auditRows").innerHTML = items.map(a => `<tr><td class="muted">${esc(a.created_at)}</td>
    <td class="mono">${esc(a.tenant_id)}</td><td>${esc(a.actor_username||"-")}</td>
    <td>${esc(a.action)}</td><td>${esc(a.target)}</td><td>${esc(a.detail)}</td></tr>`).join("");
}
async function refreshAll(){ try{ await loadOverview(); await loadTenants(); await loadUsers(); await loadAudit(); }catch(error){ notice(error.message, true); } }
window.editTenant = async id => {
  const name = prompt("租户名称"); if(name===null) return;
  const max = prompt("账号上限（1-1000）"); if(max===null) return;
  try{ await api("/api/admin/tenants/"+id,{method:"PUT",body:JSON.stringify({name,max_users:Number(max)})}); notice("已保存"); refreshAll(); }catch(e){ notice(e.message,true); }
};
window.toggleTenant = async (id,status) => {
  try{ await api("/api/admin/tenants/"+id,{method:"PUT",body:JSON.stringify({status})}); notice("已更新"); refreshAll(); }catch(e){ notice(e.message,true); }
};
window.setRole = async (id,role) => {
  try{ await api("/api/admin/users/"+id,{method:"PUT",body:JSON.stringify({role})}); notice("角色已更新"); refreshAll(); }catch(e){ notice(e.message,true); }
};
window.toggleUser = async (id,status) => {
  try{ await api("/api/admin/users/"+id,{method:"PUT",body:JSON.stringify({status})}); notice("已更新"); refreshAll(); }catch(e){ notice(e.message,true); }
};
let passTarget = null;
window.resetPassword = id => { passTarget=id; $("passValue").value=""; $("passDialog").showModal(); $("passValue").focus(); };
$("passSave").onclick = async event => {
  event.preventDefault();
  const password = $("passValue").value;
  if(!password || !passTarget) return;
  try{ await api("/api/admin/users/"+passTarget,{method:"PUT",body:JSON.stringify({password})}); $("passDialog").close(); notice("口令已更新"); loadAudit(); }catch(e){ notice(e.message,true); }
};
$("refresh").onclick = () => refreshAll();
$("refreshAudit").onclick = () => loadAudit();
$("createTenant").onclick = async () => {
  try{ await api("/api/admin/tenants",{method:"POST",body:JSON.stringify({name:$("tenantName").value,max_users:Number($("tenantMax").value||5)})}); $("tenantName").value=""; notice("租户已开通"); refreshAll(); }catch(e){ notice(e.message,true); }
};
$("createUser").onclick = async () => {
  const body = {username:$("newUser").value,password:$("newPass").value,role:$("newRole").value};
  if(me.role==="admin") body.tenant_id = $("userTenant").value;
  try{ await api("/api/admin/users",{method:"POST",body:JSON.stringify(body)}); $("newUser").value=""; $("newPass").value=""; notice("账号已开通"); refreshAll(); }catch(e){ notice(e.message,true); }
};
$("logout").onclick = async () => { try{ await fetch("/sign-out",{method:"POST",credentials:"same-origin"}); }catch{} location.href="/invite"; };
(async function(){
  try{
    const data = (await api("/api/admin/me")).data; me = data;
    $("who").textContent = `${data.username} · ${data.role_label}`;
    if(data.role==="admin") $("tenantSection").classList.remove("hidden");
    if(data.role!=="admin") $("newRole").innerHTML = '<option value="member">普通账号</option>';
    if($("userTenant")) $("userTenant").onchange = () => loadUsers();
    if($("auditTenant")) $("auditTenant").onchange = () => loadAudit();
    __HIDE_LOGOUT__
    await refreshAll();
  }catch(error){ notice(error.message,true); }
})();
</script>
</body></html>"""


def page(local_token=None):
    boot = ""
    if local_token:
        boot = ('<script>if(location.hostname==="127.0.0.1"||location.hostname==="localhost")'
                f'localStorage.setItem("token","{local_token}");</script>')
    html = PAGE.replace("__LOCAL_BOOT__", boot)
    html = html.replace("__LOCAL_FLAG__", "true" if local_token else "false")
    if local_token:
        html = html.replace("__LOGOUT_CLASS__", "hidden")
        html = html.replace("__HIDE_LOGOUT__", '$("logout").classList.add("hidden");')
    else:
        html = html.replace("__LOGOUT_CLASS__", "")
        html = html.replace("__HIDE_LOGOUT__", "")
    return html.encode("utf-8")


def forbidden_page():
    return ("""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>无权访问</title><body style="font:16px system-ui;padding:40px">
<h1>无权访问管理后台</h1><p>当前账号没有管理权限。</p>
<p><a href="/mb/home">返回首页</a> · <a href="/invite">重新登录</a></p></body></html>"""
            ).encode("utf-8")
