"""账号钉选、旁聊换号和 failover 观察开关。不启动浏览器。"""
import logging
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
from config import failover_live, failover_max_attempts  # noqa: E402
from sidechat import (  # noqa: E402
    SidechatKeyError,
    SidechatLedger,
    busy_degrade_decision,
    resolve_explicit_account,
)
from store import Store, quota_exhausted  # noqa: E402


def _acc(aid, **extra):
    row = {
        "id": aid, "label": aid, "enabled": True, "ok": True,
        "has_cookies": True, "last_used": extra.pop("last_used", 10),
        "use_count": extra.pop("use_count", 1),
    }
    row.update(extra)
    return row


@contextmanager
def live(on: bool):
    saved = os.environ.get("FAILOVER_LIVE"), os.environ.get("MUSE2API_FAILOVER_LIVE")
    os.environ["FAILOVER_LIVE"] = "1" if on else "0"
    os.environ.pop("MUSE2API_FAILOVER_LIVE", None)
    try:
        yield
    finally:
        for key, old in (("FAILOVER_LIVE", saved[0]), ("MUSE2API_FAILOVER_LIVE", saved[1])):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@contextmanager
def capture_logs():
    found = []

    class _Handler(logging.Handler):
        def emit(self, record):
            found.append(record.getMessage())

    handler = _Handler()
    app.log.addHandler(handler)
    try:
        yield found
    finally:
        app.log.removeHandler(handler)


def test_explicit_account_parse_and_flags():
    assert resolve_explicit_account(
        header_account="Acc_A", muse_account="muse", body_account="body",
    ) == "Acc_A"
    assert resolve_explicit_account(
        header_account="  ", muse_account="from-body", body_account="other",
    ) == "from-body"
    assert resolve_explicit_account(
        header_account=None, muse_account=None, body_account="only_body",
    ) == "only_body"
    assert resolve_explicit_account(header_account=None, muse_account=None, body_account=None) is None
    try:
        resolve_explicit_account(header_account="bad id", muse_account=None, body_account=None)
        raise AssertionError("space should fail")
    except SidechatKeyError:
        pass
    with live(False):
        assert failover_live() is False
    with live(True):
        assert failover_live() is True
    old = os.environ.get("FAILOVER_MAX_ATTEMPTS")
    os.environ["FAILOVER_MAX_ATTEMPTS"] = "9"
    try:
        assert failover_max_attempts() == 3
    finally:
        if old is None:
            os.environ.pop("FAILOVER_MAX_ATTEMPTS", None)
        else:
            os.environ["FAILOVER_MAX_ATTEMPTS"] = old
    assert quota_exhausted(None) is False
    assert quota_exhausted({"found": False, "weekly_used_pct": 100}) is False
    assert quota_exhausted({"found": True, "weekly_used_pct": 100}) is True
    assert quota_exhausted({"found": True, "weekly_used_pct": 100, "extra_left": "2B tokens left"}) is False
    assert quota_exhausted({"found": True, "weekly_used_pct": 40, "raw": "limit reached"}) is True
    assert busy_degrade_decision(busy=True, allow=True, live=False, alternate_id="b")["action"] == "dry"
    assert busy_degrade_decision(busy=True, allow=True, live=True, alternate_id="b")["action"] == "degrade"
    assert busy_degrade_decision(busy=False, allow=True, live=True, alternate_id="b")["action"] == "stay"
    print("PASS: parse, flags, quota, busy decision")


def test_priority_cross_account_and_health():
    path = tempfile.mktemp(suffix=".json")
    ledger = SidechatLedger(path)
    now = time.time()
    ops = _acc("acc-ops", last_used=5)
    other = _acc("acc-other", last_used=30)
    cooled = _acc("acc-ops", failover={"cooldown_until": int(now) + 600, "fail_count": 2,
                                       "last_fail_reason": "auth", "no_picker": False})
    no_pick = _acc("acc-ops", failover={"cooldown_until": 0, "fail_count": 1,
                                        "last_fail_reason": "no_picker", "no_picker": True})
    quota = _acc("acc-ops", quota={"found": True, "weekly_used_pct": 100})
    try:
        ledger.set_role_account("ops", "acc-ops")
        ledger.record_result(
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-ops", session_id="old-session-example", nav="fresh",
        )
        resume = ledger.plan("ops", None, [ops, other])
        assert resume["account_id"] == "acc-ops"
        assert resume["session_id"] == "old-session-example"
        assert resume["reason"] == "resume"
        assert resume["failover"] is False

        # 映射改了，仍先续用原号上的旁聊。
        ledger.set_role_account("ops", "acc-other")
        sticky = ledger.plan("ops", None, [ops, other])
        assert sticky["account_id"] == "acc-ops"
        assert sticky["session_id"] == "old-session-example"

        explicit = ledger.plan(
            "ops", None, [ops, other], explicit_account="acc-other",
        )
        assert explicit["account_id"] == "acc-other"
        assert explicit["session_id"] is None
        assert explicit["retire_session_id"] == "old-session-example"
        assert explicit["failover"] is True
        assert explicit["reason"] == "explicit"

        same = ledger.plan("ops", None, [ops, other], explicit_account="acc-ops")
        assert same["account_id"] == "acc-ops"
        assert same["session_id"] == "old-session-example"
        assert same["failover"] is False

        moved = ledger.record_result(
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-other", session_id="old-session-example", nav="fresh",
        )
        assert moved["muse_account_id"] == "acc-other"
        assert moved["session_id"] is None
        assert moved["prior_session_id"] == "old-session-example"
        assert moved["status"] == "pending"

        fresh = ledger.record_result(
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-other", session_id="new-session-example", nav="fresh",
            failover=True, failover_reason="explicit",
        )
        assert fresh["session_id"] == "new-session-example"
        assert fresh["prior_session_id"] == "old-session-example"
        assert fresh["failover_count"] >= 1
        assert fresh["last_failover_reason"] == "explicit"

        # 把旁聊放回 ops，测 dry / live。
        ledger.record_result(
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-ops", session_id="old-session-example", nav="fresh",
        )
        dry_pick = ledger.plan(
            "ops", None, [no_pick, other], require_picker=True, live=False,
        )
        assert dry_pick["account_id"] == "acc-ops"
        assert dry_pick["session_id"] == "old-session-example"
        assert dry_pick["failover"] is False
        assert dry_pick["would_failover"] is True
        assert dry_pick["would_account_id"] == "acc-other"
        assert dry_pick["would_reason"] == "no_picker"

        live_pick = ledger.plan(
            "ops", None, [no_pick, other], require_picker=True, live=True,
        )
        assert live_pick["account_id"] == "acc-other"
        assert live_pick["session_id"] is None
        assert live_pick["failover"] is True
        assert live_pick["reason"] == "no_picker"
        assert live_pick["retire_session_id"] == "old-session-example"

        spark = ledger.plan("ops", None, [no_pick, other], require_picker=False, live=True)
        assert spark["account_id"] == "acc-ops"
        assert spark["would_failover"] is False

        none = ledger.plan("ops", None, [no_pick, _acc("acc-other", failover={"no_picker": True})],
                           require_picker=True, live=True)
        assert none["account_id"] is None
        assert none["reason"] == "no_picker"

        dry_quota = ledger.plan("ops", None, [quota, other], live=False)
        assert dry_quota["account_id"] == "acc-ops"
        assert dry_quota["would_reason"] == "quota"
        assert dry_quota["would_account_id"] == "acc-other"
        live_quota = ledger.plan("ops", None, [quota, other], live=True)
        assert live_quota["account_id"] == "acc-other"
        assert live_quota["reason"] == "quota"
        assert live_quota["session_id"] is None

        kept = ledger.plan(
            "ops", None,
            [_acc("acc-ops", quota={"found": True, "weekly_used_pct": 100, "extra_left": "2B tokens left"}), other],
            live=True,
        )
        assert kept["account_id"] == "acc-ops"

        dry_cool = ledger.plan("ops", None, [cooled, other], live=False, now=now)
        assert dry_cool["account_id"] == "acc-ops"
        assert dry_cool["would_reason"] == "cooldown"
        live_cool = ledger.plan("ops", None, [cooled, other], live=True, now=now)
        assert live_cool["account_id"] == "acc-other"
        assert live_cool["reason"] == "cooldown"

        anon = ledger.plan(None, None, [ops, other], explicit_account="acc-other")
        assert anon["strategy"] == "A"
        assert anon["ledger_key"] is None
        assert anon["account_id"] == "acc-other"
        assert ledger.snapshot()["entries"]
    finally:
        if os.path.exists(path):
            os.remove(path)
    print("PASS: priority, new session, no-picker, quota, cooldown")


def _workspace():
    tmp = tempfile.mkdtemp()
    store = Store(SimpleNamespace(
        accounts_file=os.path.join(tmp, "accounts.json"),
        tasks_file=os.path.join(tmp, "tasks.json"),
    ))
    ops = store.add_account({"hatch_sess": "ops-cookie"}, label="ops")
    other = store.add_account({"hatch_sess": "other-cookie"}, label="other")
    store.mark(ops["id"], True, "")
    store.mark(other["id"], True, "")
    ledger = SidechatLedger(os.path.join(tmp, "sidechat_ledger.json"))
    return tmp, store, ledger, ops, other


def _restore(tmp, old_store, old_ledger):
    app.store = old_store
    app.ledger = old_ledger
    for name in os.listdir(tmp):
        os.remove(os.path.join(tmp, name))
    os.rmdir(tmp)


def test_route_map_follow_headers_and_busy():
    tmp, store, ledger, ops, other = _workspace()
    old_store, old_ledger = app.store, app.ledger
    app.store = store
    app.ledger = ledger
    try:
        with live(False):
            ledger.set_role_account("ops", ops["id"])
            ledger.record_result(
                ledger_key="role:ops", role="ops", project=None,
                account_id=ops["id"], account_label="ops",
                session_id="old-session-example", nav="fresh",
            )
            acc, anon = app._route_chat_account(None, None)
            assert anon is None
            assert ledger.snapshot()["role_account_map"]["ops"] == ops["id"]

            try:
                app._route_chat_account("ops", None, explicit_account="missing_id")
                raise AssertionError("unknown account")
            except HTTPException as exc:
                assert exc.status_code == 400

            acc, plan = app._route_chat_account(
                "ops", None, explicit_account=other["id"],
            )
            assert acc["id"] == other["id"]
            assert plan["session_id"] is None
            assert plan["failover"] is True
            app._commit_sidechat(
                plan,
                {"nav": "fresh", "session_id": "new-session-example"},
                other["id"],
                follow_map=True,
            )
            snap = ledger.snapshot()
            assert snap["role_account_map"]["ops"] == other["id"]
            entry = next(e for e in snap["entries"] if e["ledger_key"] == "role:ops")
            assert entry["session_id"] == "new-session-example"
            assert entry["prior_session_id"] == "old-session-example"
            assert entry["muse_account_id"] == other["id"]
            assert "hatch" not in str(snap)

            # 旁聊放回 ops，并标成无选卡。dry 留在 ops，不改映射。
            ledger.set_role_account("ops", ops["id"])
            ledger.record_result(
                ledger_key="role:ops", role="ops", project=None,
                account_id=ops["id"], account_label="ops",
                session_id="ops-session-example", nav="fresh",
            )
            store.note_account_failure(ops["id"], "no_picker", no_picker=True)
            with capture_logs() as lines:
                acc, plan = app._route_chat_account("ops", None, require_picker=True)
            assert acc["id"] == ops["id"]
            assert plan["failover"] is False
            assert plan["would_account_id"] == other["id"]
            assert plan["would_reason"] == "no_picker"
            assert any("failover_would=1" in line and "reason=no_picker" in line for line in lines)
            app._commit_sidechat(
                plan, {"nav": "reused", "session_id": "ops-session-example"},
                ops["id"], follow_map=True,
            )
            assert ledger.snapshot()["role_account_map"]["ops"] == ops["id"]

            def fake_stream(cookies, prompt, expires, timeout, account_id=None,
                            chat_model=None, sidechat=None, outcome=None):
                if outcome is not None:
                    outcome.update({
                        "account_id": account_id,
                        "account_label": (sidechat or {}).get("account_label") or "",
                        "session_id": (sidechat or {}).get("session_id") or "",
                        "failover": bool((sidechat or {}).get("failover")),
                        "failover_reason": (sidechat or {}).get("reason") or "",
                        "would_failover": bool((sidechat or {}).get("would_failover")),
                        "would_account_id": (sidechat or {}).get("would_account_id"),
                        "would_reason": (sidechat or {}).get("would_reason"),
                        "attempts": 1,
                    })
                yield "ok"

            original = app.safe_chat_stream
            app.safe_chat_stream = fake_stream
            try:
                client = TestClient(app.app)
                headers = {"X-Muse-Role": "ops", "X-Muse-Account": ops["id"]}
                if app.CFG.api_key:
                    headers["Authorization"] = "Bearer " + app.CFG.api_key
                bad = client.post("/v1/chat/completions", headers={**headers, "X-Muse-Account": "has space"},
                                  json={"model": "muse-spark",
                                        "messages": [{"role": "user", "content": "hi"}]})
                assert bad.status_code == 400
                missing = client.post("/v1/chat/completions",
                                      headers={**headers, "X-Muse-Account": "not_in_pool"},
                                      json={"model": "muse-spark",
                                            "messages": [{"role": "user", "content": "hi"}]})
                assert missing.status_code == 400
                body_pin = client.post(
                    "/v1/chat/completions",
                    headers={k: v for k, v in headers.items() if k != "X-Muse-Account"},
                    json={"model": "muse-spark", "muse_role": "ops", "muse_account": ops["id"],
                          "account_id": other["id"],
                          "messages": [{"role": "user", "content": "hi"}]},
                )
                assert body_pin.status_code == 200, body_pin.text
                assert body_pin.headers["x-muse-account-id"] == ops["id"]
                assert body_pin.json()["muse"]["account_id"] == ops["id"]
                assert "hatch" not in body_pin.text
                wonder = client.post(
                    "/v1/chat/completions",
                    headers={k: v for k, v in headers.items() if k != "X-Muse-Account"},
                    json={"model": "muse-wonder", "muse_role": "ops",
                          "messages": [{"role": "user", "content": "hi"}]},
                )
                assert wonder.status_code == 200, wonder.text
                assert wonder.headers["x-muse-failover"] == "false"
                assert wonder.headers["x-muse-failover-reason"] == "no_picker"
                assert wonder.headers["x-muse-failover-to"] == other["id"]
                assert wonder.headers["x-muse-account-id"] == ops["id"]
                assert wonder.json()["muse"]["failover"] is False
            finally:
                app.safe_chat_stream = original

            store.note_account_failure(other["id"], "no_picker", no_picker=True)
            try:
                with live(True):
                    app._route_chat_account("ops", None, require_picker=True)
                raise AssertionError("expected no picker in the pool")
            except HTTPException as exc:
                assert exc.status_code == 503
            store.clear_no_picker(other["id"])

        with live(True):
            acc, plan = app._route_chat_account("ops", None, require_picker=True)
            assert acc["id"] == other["id"]
            assert plan["reason"] == "no_picker"
            assert plan["session_id"] is None
            app._commit_sidechat(
                plan, {"nav": "fresh", "session_id": "picker-session-example"},
                other["id"], follow_map=True,
            )
            assert ledger.snapshot()["role_account_map"]["ops"] == other["id"]

        # 健康字段：成功清掉认证冷却，额度冷却要等配额刷新。
        fo = store.note_account_failure(ops["id"], "auth", cooldown_seconds=1200)
        assert fo["fail_count"] >= 1
        assert fo["cooldown_until"] > time.time()
        cleared = store.note_account_success(ops["id"])
        assert cleared["fail_count"] == 0
        assert cleared["cooldown_until"] == 0
        store.note_account_failure(ops["id"], "quota", cooldown_seconds=1200)
        kept = store.note_account_success(ops["id"])
        assert kept["cooldown_until"] > time.time()
        store.update_account(ops["id"], quota={"found": True, "weekly_used_pct": 12})
        assert store.get_account(ops["id"])["failover"]["cooldown_until"] == 0

        pool = app.AccountPoolManager(store)
        pool.busy_accounts.add(ops["id"])
        with live(False):
            with capture_logs() as lines:
                started = time.monotonic()
                try:
                    pool.acquire(account_id=ops["id"], timeout=0.35, allow_degrade=True)
                    raise AssertionError("dry busy should keep waiting")
                except app.MuseGenerationError:
                    pass
            assert time.monotonic() - started >= 0.25
            assert any("failover_would=1" in line and "reason=busy" in line and other["id"] in line
                       for line in lines)
        with live(True):
            alt = pool.acquire(account_id=ops["id"], timeout=1.0, allow_degrade=True)
            assert alt["id"] == other["id"]
            pool.release(alt["id"])
    finally:
        _restore(tmp, old_store, old_ledger)
    print("PASS: route, map follow, headers, busy degrade")


if __name__ == "__main__":
    test_explicit_account_parse_and_flags()
    test_priority_cross_account_and_health()
    test_route_map_follow_headers_and_busy()
    print("ALL FAILOVER TESTS PASSED")
