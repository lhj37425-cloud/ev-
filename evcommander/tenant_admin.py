#!/usr/bin/env python3
"""Administrator command line for tenants and accounts.

Passwords are never accepted as command-line arguments. Run the command and
type the password at the prompt (or pipe it on stdin for automation). The
database comes from ``EV_DATA_DIR`` / ``.local`` exactly like serve.py.

Examples:
    python tenant_admin.py tenant list
    python tenant_admin.py tenant create --name 客户A
    python tenant_admin.py user create --tenant <tenant-id> --username client_a
    python tenant_admin.py user password --username client_a
"""

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

import commander
import tenancy


def read_password(confirm=True):
    if sys.stdin.isatty():
        first = getpass.getpass("口令：")
        if confirm and getpass.getpass("再输一次：") != first:
            raise SystemExit("两次口令不一致。")
        return first
    return sys.stdin.readline().rstrip("\r\n")


def read_code():
    if sys.stdin.isatty():
        return input("6 位动态验证码：").strip()
    return sys.stdin.readline().rstrip("\r\n").strip()


def print_rows(rows):
    for row in rows:
        print("  ".join(str(value) for value in row))


def command_tenant(args):
    if args.action == "list":
        print_rows((row["id"], row["name"], row["status"], row["max_users"], row["created_at"])
                   for row in tenancy.tenants())
        return
    if args.action == "update":
        if not args.id:
            raise SystemExit("tenant update 需要 --id。")
        row = tenancy.update_tenant(args.id, name=args.name,
                                    status=args.status, max_users=args.max_users)
        print(f"已更新租户：{row['name']}（{row['id']}）状态={row['status']} "
              f"账号上限={row['max_users']}")
        return
    key = tenancy.create_tenant(args.name, args.max_users or 5)
    print(f"已开通租户：{args.name}")
    print(f"tenant_id：{key}")


def command_audit(args):
    rows = tenancy.audit_log(args.tenant or None, args.limit)
    if not rows:
        print("没有审计记录。")
    for row in rows:
        print(f"  {row['created_at']}  {row['tenant_id']}  {row['actor_username']}  "
              f"{row['action']}  {row['target']}  {row['detail']}")


def command_logins(args):
    rows = tenancy.login_history(args.tenant or None, args.limit)
    if not rows:
        print("没有登录记录。")
    for row in rows:
        print(f"  {row['at']}  {row['username']}  {row['tenant_id'] or '-'}  "
              f"{'成功' if row['success'] else '失败'}  {row['reason'] or '-'}  {row['ip'] or '-'}")


def command_user(args):
    if args.action == "list":
        rows = tenancy.users(args.tenant)
        print_rows((row["tenant_id"], row["username"], row["role"], row["status"]) for row in rows)
        return
    username = args.username
    if args.action == "create":
        key = tenancy.create_user(args.tenant, username, read_password(), args.role or "member")
        print(f"已开通账号：{username}（{key}）")
        return
    if args.action == "role":
        if not args.role:
            raise SystemExit("user role 需要 --role。")
        tenancy.set_user_role(username, args.role)
        print(f"已更新角色：{username} → {args.role}")
        return
    if args.action == "totp-setup":
        data = tenancy.setup_totp(username)
        print(f"两步验证密钥（只显示这一次）：{data['secret']}")
        print(f"otpauth：{data['uri']}")
        print("用验证器添加后，再运行 user totp-enable 输入 6 位验证码启用。")
        return
    if args.action == "totp-enable":
        tenancy.enable_totp(username, read_code())
        print(f"已开启两步验证：{username}")
        return
    if args.action == "totp-disable":
        tenancy.disable_totp(username, read_code())
        print(f"已关闭两步验证：{username}")
        return
    if args.action == "totp-reset":
        tenancy.reset_totp(username)
        print(f"已重置两步验证：{username}（需重新绑定）")
        return
    if args.action == "unlock":
        tenancy.unlock_user(username)
        print(f"已解除登录锁定：{username}")
        return
    if args.action == "password":
        tenancy.set_password(username, read_password())
        print(f"已更新口令：{username}")
        return
    if args.action == "disable":
        tenancy.set_user_status(username, "disabled")
        print(f"已停用账号：{username}")
        return
    tenancy.set_user_status(username, "active")
    print(f"已启用账号：{username}")


def command_seed_invite(_args):
    """Move the legacy invitation account into the users table (once)."""
    config = os.environ.get("EV_INVITE_CONFIG")
    if not config:
        raise SystemExit("请设置 EV_INVITE_CONFIG 指向现有邀请配置文件。")
    data = json.loads(Path(config).read_text(encoding="utf-8"))
    created = tenancy.seed_invite_user(data["username"], data["salt"], data["password_hash"])
    print("已迁移原邀请账号为平台管理员。" if created else "账号表已有账号，未做变更。")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    tenant = commands.add_parser("tenant", help="查看、开通或编辑租户")
    tenant.add_argument("action", choices=("list", "create", "update"))
    tenant.add_argument("--id", help="租户 id（update 时必填）")
    tenant.add_argument("--name", help="租户名称（create 时必填）")
    tenant.add_argument("--status", choices=("active", "disabled"))
    tenant.add_argument("--max-users", type=int, help="账号上限（默认 5）")
    tenant.set_defaults(handler=command_tenant)

    user = commands.add_parser("user", help="查看、开通或编辑账号")
    user.add_argument("action", choices=(
        "list", "create", "password", "disable", "enable", "role",
        "totp-setup", "totp-enable", "totp-disable", "totp-reset", "unlock"))
    user.add_argument("--tenant", default=tenancy.DEFAULT_TENANT, help="租户 id（默认 demo）")
    user.add_argument("--username")
    user.add_argument("--role", choices=sorted(tenancy.ROLES),
                      help="角色：admin（平台）、owner（租户管理员）、member")
    user.set_defaults(handler=command_user)

    audit = commands.add_parser("audit", help="查看管理审计日志")
    audit.add_argument("--tenant")
    audit.add_argument("--limit", type=int, default=200)
    audit.set_defaults(handler=command_audit)

    logins = commands.add_parser("logins", help="查看登录记录")
    logins.add_argument("--tenant")
    logins.add_argument("--limit", type=int, default=200)
    logins.set_defaults(handler=command_logins)

    seed = commands.add_parser("seed-invite", help="把现有邀请账号迁移为平台管理员")
    seed.set_defaults(handler=command_seed_invite)

    args = parser.parse_args()
    if args.command == "tenant" and args.action == "create" and not args.name:
        parser.error("tenant create 需要 --name。")
    if args.command == "user" and args.action != "list" and not args.username:
        parser.error("user 操作需要 --username。")
    tenancy.activate({"tenant_id": tenancy.DEFAULT_TENANT, "user_id": "cli",
                      "username": "管理员CLI", "role": "admin"})
    with commander.database():
        pass
    try:
        args.handler(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
