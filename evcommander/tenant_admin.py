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


def print_rows(rows):
    for row in rows:
        print("  ".join(str(value) for value in row))


def command_tenant(args):
    if args.action == "list":
        print_rows((row["id"], row["name"], row["status"], row["created_at"])
                   for row in tenancy.tenants())
        return
    key = tenancy.create_tenant(args.name)
    print(f"已开通租户：{args.name}")
    print(f"tenant_id：{key}")


def command_user(args):
    if args.action == "list":
        rows = tenancy.users(args.tenant)
        print_rows((row["tenant_id"], row["username"], row["role"], row["status"]) for row in rows)
        return
    username = args.username
    if args.action == "create":
        key = tenancy.create_user(args.tenant, username, read_password(), args.role)
        print(f"已开通账号：{username}（{key}）")
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

    tenant = commands.add_parser("tenant", help="查看或开通租户")
    tenant.add_argument("action", choices=("list", "create"))
    tenant.add_argument("--name", help="租户名称（create 时必填）")
    tenant.set_defaults(handler=command_tenant)

    user = commands.add_parser("user", help="查看或开通账号")
    user.add_argument("action", choices=("list", "create", "password", "disable", "enable"))
    user.add_argument("--tenant", default=tenancy.DEFAULT_TENANT, help="租户 id（默认 demo）")
    user.add_argument("--username")
    user.add_argument("--role", choices=sorted(tenancy.ROLES), default="member")
    user.set_defaults(handler=command_user)

    seed = commands.add_parser("seed-invite", help="把现有邀请账号迁移为平台管理员")
    seed.set_defaults(handler=command_seed_invite)

    args = parser.parse_args()
    if args.command == "tenant" and args.action == "create" and not args.name:
        parser.error("tenant create 需要 --name。")
    if args.command == "user" and args.action != "list" and not args.username:
        parser.error("user 操作需要 --username。")
    with commander.database():
        pass
    try:
        args.handler(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
