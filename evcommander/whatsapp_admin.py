#!/usr/bin/env python3
"""Administrator CLI for official WhatsApp Cloud API accounts.

Secrets are read at the prompt (or from stdin for automation) and are never
accepted as command-line arguments. ``list``/``show`` never print secrets.
The database comes from ``EV_DATA_DIR`` exactly like serve.py.

Examples:
    python whatsapp_admin.py list
    python whatsapp_admin.py create --tenant demo --name 主号 --phone-number-id 123456789
    python whatsapp_admin.py update --id 1 --enabled 1
    python whatsapp_admin.py reset-token --id 1
    python whatsapp_admin.py probe --id 1
"""

import argparse
import getpass
import sys

import commander
import tenancy
import whatsapp


def read_secret(label, confirm=False):
    if sys.stdin.isatty():
        value = getpass.getpass(f"{label}：")
        if confirm and getpass.getpass(f"再输一次{label}：") != value:
            raise SystemExit("两次输入不一致。")
        return value
    return sys.stdin.readline().rstrip("\r\n")


def read_text(label):
    if sys.stdin.isatty():
        return input(f"{label}：").strip()
    return sys.stdin.readline().rstrip("\r\n").strip()


def print_account(view):
    print(f"  id={view['id']}  名称={view['account_name']}  号码={view['display_phone'] or '-'}  "
          f"已配置={'是' if view['configured'] else '否'}  "
          f"值守={'开' if view['enabled'] else '关'}  "
          f"自动回复={'开' if view['auto_reply'] else '关'}  "
          f"会话={view['threads']}")


def command_list(args):
    with tenancy.scope(args.tenant, user_id="whatsapp-admin", username="管理员", role="admin"):
        accounts = whatsapp.list_accounts(args.tenant)
    if not accounts:
        print("这个租户还没有 WhatsApp 账号。")
    for view in accounts:
        print_account(view)


def command_show(args):
    with tenancy.scope(args.tenant, user_id="whatsapp-admin", username="管理员", role="admin"):
        accounts = whatsapp.list_accounts(args.tenant)
    view = next((item for item in accounts if item["id"] == args.id), None)
    if view is None:
        raise SystemExit("账号不存在。")
    print_account(view)
    print(f"  Phone Number ID={view['phone_number_id'] or '-'}  "
          f"WABA ID={view['waba_id'] or '-'}")
    print(f"  状态={view['status_text']}")
    if view["last_error"]:
        print(f"  最近错误={view['last_error']}")


def command_create(args):
    token = read_secret("Access Token（可留空，先建未配置账号）")
    secret = read_secret("App Secret（可留空）")
    verify = read_secret("Webhook 验证口令（留空自动生成）")
    body = {
        "account_name": args.name,
        "phone": args.phone or "",
        "phone_number_id": args.phone_number_id or "",
        "waba_id": args.waba_id or "",
        "access_token": token,
        "app_secret": secret,
    }
    if args.api_version:
        body["api_version"] = args.api_version
    if verify:
        body["verify_token"] = verify
    with tenancy.scope(args.tenant, user_id="whatsapp-admin", username="管理员", role="admin"):
        result = whatsapp.create_account(body, args.tenant)
    view = result["data"]
    print(f"已创建 WhatsApp 账号：{view['account_name']}（id={view['id']}）")
    if not verify:
        # The operator needs this value to fill Meta's webhook form; show it once.
        with commander.database() as db:
            row = whatsapp._account_row(db, view["id"], args.tenant)
        print(f"Webhook 验证口令（只显示这一次）：{row['verify_token']}")
    print_account(view)


def command_update(args):
    body = {}
    if args.name is not None:
        body["account_name"] = args.name
    if args.phone is not None:
        body["phone"] = args.phone
    if args.phone_number_id is not None:
        body["phone_number_id"] = args.phone_number_id
    if args.enabled is not None:
        body["enabled"] = args.enabled
    if args.auto_reply is not None:
        body["auto_reply"] = args.auto_reply
    if args.sales_config_id is not None:
        body["sales_config_id"] = args.sales_config_id
    if args.credentials:
        token = read_secret("新的 Access Token（留空保持不变）")
        secret = read_secret("新的 App Secret（留空保持不变）")
        verify = read_secret("新的 Webhook 验证口令（留空保持不变）")
        if token:
            body["access_token"] = token
        if secret:
            body["app_secret"] = secret
        if verify:
            body["verify_token"] = verify
    if not body:
        raise SystemExit("没有要更新的字段。")
    with tenancy.scope(args.tenant, user_id="whatsapp-admin", username="管理员", role="admin"):
        view = whatsapp.update_account(args.id, body, args.tenant)["data"]
    print("已更新。")
    print_account(view)


def command_delete(args):
    with tenancy.scope(args.tenant, user_id="whatsapp-admin", username="管理员", role="admin"):
        whatsapp.delete_account(args.id, args.tenant)
    print(f"已删除账号 id={args.id}（历史会话保留）。")


def command_probe(args):
    with tenancy.scope(args.tenant, user_id="whatsapp-admin", username="管理员", role="admin"):
        result = whatsapp.probe_account(args.id, args.tenant)
    print(("成功：" if result["ok"] else "失败：") + result["note"])


def command_reset_token(args):
    with tenancy.scope(args.tenant, user_id="whatsapp-admin", username="管理员", role="admin"):
        result = whatsapp.reset_token(args.id, args.tenant)
    print(f"新的 Webhook 验证口令（只显示这一次，请填入 Meta）：{result['data']['token']}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, handler):
        command = commands.add_parser(name)
        command.add_argument("--tenant", default=tenancy.DEFAULT_TENANT)
        command.set_defaults(handler=handler)
        return command

    add("list", command_list)

    show = add("show", command_show)
    show.add_argument("--id", type=int, required=True)

    create = add("create", command_create)
    create.add_argument("--name", required=True)
    create.add_argument("--phone")
    create.add_argument("--phone-number-id")
    create.add_argument("--waba-id")
    create.add_argument("--api-version")

    update = add("update", command_update)
    update.add_argument("--id", type=int, required=True)
    update.add_argument("--name")
    update.add_argument("--phone")
    update.add_argument("--phone-number-id")
    update.add_argument("--enabled", type=int, choices=(0, 1))
    update.add_argument("--auto-reply", type=int, choices=(0, 1))
    update.add_argument("--sales-config-id", type=int)
    update.add_argument("--credentials", action="store_true")

    delete = add("delete", command_delete)
    delete.add_argument("--id", type=int, required=True)

    probe = add("probe", command_probe)
    probe.add_argument("--id", type=int, required=True)

    token = add("reset-token", command_reset_token)
    token.add_argument("--id", type=int, required=True)

    args = parser.parse_args()
    with commander.database():
        pass
    try:
        args.handler(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
