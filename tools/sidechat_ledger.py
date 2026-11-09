#!/usr/bin/env python3
"""旁聊台账的只读查看和过期标记。不调用 Muse session.delete，也不读取 cookie。

示例：
  python tools/sidechat_ledger.py list
  python tools/sidechat_ledger.py cleanup
  python tools/sidechat_ledger.py stale role:ops
  python tools/sidechat_ledger.py map ops <account_id>
  python tools/sidechat_ledger.py map ops -

map 把角色钉到账号池里的 id（与 data/accounts.json 的 id 一致）。
账号不可用时，下一次该角色的请求会换一个可用账号并新建旁聊。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from config import CFG  # noqa: E402
from sidechat import SidechatKeyError, SidechatLedger  # noqa: E402


def _ledger() -> SidechatLedger:
    return SidechatLedger(CFG.sidechat_ledger_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查看或标记 muse2api 旁聊台账")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="打印台账（无 cookie）")

    cleanup = sub.add_parser("cleanup", help="把闲置或超量条目标成 stale")
    cleanup.add_argument("--days", type=float, default=7)
    cleanup.add_argument("--max-per-account", type=int, default=20)

    stale = sub.add_parser("stale", help="按 ledger_key 标 stale")
    stale.add_argument("ledger_key")

    mapper = sub.add_parser("map", help="设置或清除 role → account id")
    mapper.add_argument("role")
    mapper.add_argument("account_id", nargs="?", default=None,
                        help="账号 id；用 - 清除该角色")

    args = parser.parse_args(argv)
    ledger = _ledger()
    try:
        if args.cmd == "list":
            print(json.dumps(ledger.snapshot(), ensure_ascii=False, indent=2))
        elif args.cmd == "cleanup":
            print(json.dumps(ledger.cleanup(
                stale_after=args.days * 86400,
                max_per_account=args.max_per_account,
            ), ensure_ascii=False, indent=2))
        elif args.cmd == "stale":
            if not ledger.mark_stale_key(args.ledger_key, reason="cli"):
                print("台账里没有这个 ledger_key", file=sys.stderr)
                return 1
            print(json.dumps({"ok": True, "ledger_key": args.ledger_key, "status": "stale",
                              "deleted_remote": False}, ensure_ascii=False))
        elif args.cmd == "map":
            account_id = None if args.account_id in (None, "-", "") else args.account_id
            if args.account_id is None:
                print(json.dumps(ledger.snapshot().get("role_account_map") or {},
                                 ensure_ascii=False, indent=2))
            else:
                print(json.dumps(ledger.set_role_account(args.role, account_id),
                                 ensure_ascii=False, indent=2))
    except SidechatKeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
