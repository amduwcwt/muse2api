"""旁聊台账的 key、钉账号和对话换页。不启动浏览器。"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

import app  # noqa: E402
import engine as engine_mod  # noqa: E402
from sidechat import (  # noqa: E402
    BUBBLE_ROLLOVER,
    SidechatKeyError,
    SidechatLedger,
    ledger_key,
    resolve_dimensions,
    session_id_from_path,
)
from store import Store  # noqa: E402


def _accounts():
    return [
        {"id": "acc-ops", "label": "ops", "enabled": True, "ok": True,
         "has_cookies": True, "last_used": 10, "use_count": 2},
        {"id": "acc-research", "label": "research", "enabled": True, "ok": True,
         "has_cookies": True, "last_used": 20, "use_count": 1},
        {"id": "acc-dead", "label": "dead", "enabled": True, "ok": False,
         "has_cookies": True, "last_used": 0, "use_count": 9},
    ]


def test_keys_and_dimensions():
    assert ledger_key("ops") == "role:ops"
    assert ledger_key("ops", "bridgeai") == "role:ops:proj:bridgeai"
    assert session_id_from_path("/thread/new") is None
    assert session_id_from_path("/thread/main") is None
    assert session_id_from_path("/") is None
    assert session_id_from_path("/thread/example-session-not-real") == "example-session-not-real"

    role, project = resolve_dimensions(
        header_role="Ops", header_project="BridgeAI",
        muse_role="qa", muse_project="other",
        body_role="docs", body_project="nope",
        user="role:cloud:proj:ignored",
    )
    assert (role, project) == ("ops", "bridgeai")

    role, project = resolve_dimensions(
        header_role=None, header_project=None,
        muse_role=None, muse_project=None,
        body_role=None, body_project=None,
        user="user_123",
    )
    assert (role, project) == (None, None)

    role, project = resolve_dimensions(
        header_role=None, header_project=None,
        muse_role="ops", muse_project=None,
        body_role="qa", body_project=None,
        user="role:ops:proj:bridgeai",
    )
    assert (role, project) == ("ops", "bridgeai")
    role, project = resolve_dimensions(
        header_role=None, header_project=None,
        muse_role="ops", muse_project=None,
        body_role="qa", body_project="bridge",
        user="role:ops:proj:ignored",
    )
    assert (role, project) == ("ops", "bridge")

    try:
        resolve_dimensions(header_role=None, header_project="bridge",
                           muse_role=None, muse_project=None,
                           body_role=None, body_project=None, user="alice")
        raise AssertionError("project without role should fail")
    except SidechatKeyError:
        pass
    try:
        resolve_dimensions(header_role="运维", header_project=None,
                           muse_role=None, muse_project=None,
                           body_role=None, body_project=None, user=None)
        raise AssertionError("non-slug role should fail")
    except SidechatKeyError:
        pass
    print("PASS: keys and dimensions")


def test_plan_sticky_and_failover():
    path = tempfile.mktemp(suffix=".json")
    ledger = SidechatLedger(path)
    try:
        anon = ledger.plan(None, None, _accounts())
        assert anon["strategy"] == "A" and anon["session_id"] is None
        assert anon["ledger_key"] is None

        ledger.set_role_account("ops", "acc-ops")
        ledger.set_role_account("research", "acc-dead")
        first = ledger.plan("ops", None, _accounts())
        assert first["strategy"] == "bc"
        assert first["ledger_key"] == "role:ops"
        assert first["account_id"] == "acc-ops"
        assert first["session_id"] is None
        assert first["reason"] == "role_map"

        ledger.record_result(
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-ops", account_label="ops",
            session_id="example-session-not-real", bubble_hint=4, nav="fresh",
        )
        again = ledger.plan("ops", None, _accounts())
        assert again["session_id"] == "example-session-not-real"
        assert again["account_id"] == "acc-ops"
        assert again["reason"] == "resume"

        proj = ledger.plan("ops", "bridge", _accounts())
        assert proj["ledger_key"] == "role:ops:proj:bridge"
        assert proj["account_id"] == "acc-ops"
        assert proj["session_id"] is None

        dead = ledger.plan("research", None, _accounts())
        assert dead["account_id"] != "acc-dead"
        assert dead["session_id"] is None
        assert dead["account_id"] in ("acc-ops", "acc-research")

        ledger.record_result(
            ledger_key="role:research", role="research", project=None,
            account_id="acc-dead", account_label="dead",
            session_id="dead-session-id-value", nav="fresh",
        )
        moved = ledger.plan("research", None, _accounts())
        assert moved["session_id"] is None
        assert moved["account_id"] != "acc-dead"
        assert moved["failover"] is True
        assert moved["retire_session_id"] == "dead-session-id-value"

        full = ledger.plan("ops", None, _accounts())
        assert full["session_id"] == "example-session-not-real"
        ledger.record_result(
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-ops", account_label="ops",
            session_id="example-session-not-real",
            bubble_hint=BUBBLE_ROLLOVER, nav="reused",
        )
        rolled = ledger.plan("ops", None, _accounts())
        assert rolled["account_id"] == "acc-ops"
        assert rolled["session_id"] is None
        assert rolled["reason"] == "rollover"
        assert rolled["retire_session_id"] == "example-session-not-real"

        text = Path(path).read_text(encoding="utf-8")
        assert "hatch_sess" not in text
        assert "cookies" not in text
        assert "example-session-not-real" in text
    finally:
        if os.path.exists(path):
            os.remove(path)
    print("PASS: sticky plan and failover")


def test_cleanup_and_invalidate():
    path = tempfile.mktemp(suffix=".json")
    now = 1_700_000_000
    entries = []
    for i in range(22):
        entries.append({
            "id": f"sc_{i:02d}",
            "ledger_key": f"role:bot{i}",
            "role": f"bot{i}",
            "project": None,
            "muse_account_id": "acc-ops",
            "muse_account_label": "ops",
            "session_id": f"session-id-{i:02d}-example",
            "status": "active",
            "pin": i == 0,
            "created_at": now - 1000 + i,
            "last_used_at": now - 1000 + i,
            "use_count": i,
        })
    entries.append({
        "id": "sc_old",
        "ledger_key": "role:old",
        "role": "old",
        "project": None,
        "muse_account_id": "acc-research",
        "session_id": "session-old-example",
        "status": "active",
        "pin": False,
        "created_at": now - 8 * 86400,
        "last_used_at": now - 8 * 86400,
        "use_count": 1,
    })
    Path(path).write_text(json.dumps({
        "version": 1, "entries": entries, "role_account_map": {"ops": "acc-ops"},
    }), encoding="utf-8")
    ledger = SidechatLedger(path)
    try:
        result = ledger.cleanup(now=now)
        assert result["deleted_remote"] is False
        snap = ledger.snapshot()
        by_key = {e["ledger_key"]: e for e in snap["entries"]}
        assert by_key["role:bot0"]["status"] == "active"  # pin 保留
        assert by_key["role:bot1"]["status"] == "stale"
        assert by_key["role:old"]["status"] == "stale"
        active_open = [e for e in snap["entries"]
                       if e["muse_account_id"] == "acc-ops"
                       and e["status"] == "active" and not e.get("pin")]
        assert len(active_open) == 20
        assert ledger.invalidate_account("acc-ops", reason="disabled") >= 1
        snap = ledger.snapshot()
        assert all(e["status"] == "stale"
                   for e in snap["entries"] if e["muse_account_id"] == "acc-ops")
        assert all("cookie" not in json.dumps(e) for e in snap["entries"])
    finally:
        if os.path.exists(path):
            os.remove(path)
    print("PASS: cleanup and invalidate")


def test_store_pick_prefers_pinned_account():
    tmp = tempfile.mkdtemp()
    try:
        store = Store(SimpleNamespace(
            accounts_file=os.path.join(tmp, "accounts.json"),
            tasks_file=os.path.join(tmp, "tasks.json"),
        ))
        first = store.add_account({"hatch_sess": "a"}, label="first")
        second = store.add_account({"hatch_sess": "b"}, label="second")
        store.mark(first["id"], True, "")
        store.mark(second["id"], True, "")
        picked = store.pick_account(preferred_id=second["id"], rotate=False)
        assert picked["id"] == second["id"]
        store.mark(second["id"], False, "dead")
        fallback = store.pick_account(preferred_id=second["id"], rotate=False)
        assert fallback["id"] == first["id"]
    finally:
        for name in os.listdir(tmp):
            os.remove(os.path.join(tmp, name))
        os.rmdir(tmp)
    print("PASS: pick_account sticky")


class _Page:
    def __init__(self):
        self.path = "/thread/new"
        self.bubbles = 0
        self.blocked = False
        self.ready = True
        self.legacy = False
        self.dead = set()
        self.calls = []

    def js(self, script):
        if "function(forChat)" in script:
            return self.legacy
        if "m2a-path" in script:
            return self.path
        if "m2a-blocked" in script:
            return self.blocked
        if "m2a-bubbles" in script:
            return self.bubbles
        if "m2a-composer-ready" in script:
            return self.ready
        if "Connecting..." in script:
            return "ready"
        return None

    def send(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        if method != "Page.navigate":
            return
        url = params.get("url") or ""
        path = url.split("muse.ai", 1)[-1]
        if not path.startswith("/"):
            path = "/" + path
        sid = ""
        if "/thread/" in path:
            sid = path.split("/thread/", 1)[1].strip("/")
        if sid and sid in self.dead:
            self.path = "/"
            return
        self.path = path.split("?", 1)[0]
        if self.path.rstrip("/").endswith("/thread/new"):
            self.bubbles = 0
            self.blocked = False


def _engine(page):
    eng = engine_mod.MuseEngine.__new__(engine_mod.MuseEngine)
    eng.page = page
    return eng


def test_reset_thread_navigation():
    import inspect
    import time
    image_src = inspect.getsource(engine_mod.MuseEngine.reset_thread)
    chat_src = inspect.getsource(engine_mod.MuseEngine._reset_chat_thread)
    assert "bubbleCount >= 24" in image_src
    assert "pathname !== '/thread/new'" in image_src
    assert "bubbleCount >= 24" not in chat_src
    assert "/thread/new" in chat_src
    original = time.sleep
    time.sleep = lambda *_a, **_k: None
    try:
        page = _Page()
        eng = _engine(page)
        page.bubbles = 10
        page.path = "/thread/example-session-not-real"
        assert eng.reset_thread(for_chat=True) == "new"
        assert page.path.rstrip("/").endswith("/thread/new")
        assert any(c[0] == "Page.navigate" and c[1]["url"].endswith("/thread/new")
                   for c in page.calls)

        page.calls.clear()
        page.path = "/thread/new"
        page.bubbles = 0
        assert eng.reset_thread(for_chat=True) == "fresh"
        assert page.calls == []

        page.path = "/thread/example-session-not-real"
        page.bubbles = 3
        assert eng.reset_thread(for_chat=True, session_id="example-session-not-real") == "reused"
        assert page.calls == []

        page.bubbles = BUBBLE_ROLLOVER
        assert eng.reset_thread(for_chat=True, session_id="example-session-not-real") == "rollover"
        assert page.path.rstrip("/").endswith("/thread/new")

        page.path = "/thread/new"
        page.bubbles = 0
        nav = eng.reset_thread(for_chat=True, session_id="another-session-example")
        assert nav == "resumed"
        assert page.path == "/thread/another-session-example"

        page.dead.add("missing-session-example")
        page.path = "/thread/new"
        assert eng.reset_thread(for_chat=True, session_id="missing-session-example") == "stale"
        assert page.path.rstrip("/").endswith("/thread/new")
        assert any("missing-session-example" in c[1].get("url", "") for c in page.calls)

        page.calls.clear()
        page.legacy = False
        page.path = "/thread/example-session-not-real"
        page.bubbles = 3
        assert eng.reset_thread(for_chat=False) == "kept"
        assert page.calls == []
        page.legacy = True
        assert eng.reset_thread(for_chat=False) == "new"
        assert page.path.rstrip("/").endswith("/thread/new")
    finally:
        time.sleep = original
    print("PASS: reset_thread navigation")


def test_chat_route_and_openai_body():
    tmp = tempfile.mkdtemp()
    old_store, old_ledger = app.store, app.ledger
    try:
        store = Store(SimpleNamespace(
            accounts_file=os.path.join(tmp, "accounts.json"),
            tasks_file=os.path.join(tmp, "tasks.json"),
        ))
        ops = store.add_account({"hatch_sess": "ops"}, label="ops")
        other = store.add_account({"hatch_sess": "other"}, label="other")
        store.mark(ops["id"], True, "")
        store.mark(other["id"], True, "")
        ledger = SidechatLedger(os.path.join(tmp, "sidechat_ledger.json"))
        ledger.set_role_account("ops", ops["id"])
        app.store = store
        app.ledger = ledger

        acc, plan = app._route_chat_account("ops", "bridge")
        assert acc["id"] == ops["id"]
        assert plan["ledger_key"] == "role:ops:proj:bridge"
        assert plan["session_id"] is None
        app.ledger.record_result(
            ledger_key=plan["ledger_key"], role="ops", project="bridge",
            account_id=ops["id"], account_label="ops",
            session_id="bridge-session-example", nav="fresh",
        )
        acc, plan = app._route_chat_account("ops", "bridge")
        assert acc["id"] == ops["id"]
        assert plan["session_id"] == "bridge-session-example"

        _acc, anon = app._route_chat_account(None, None)
        assert anon is None

        store.mark(ops["id"], False, "dead")
        acc, plan = app._route_chat_account("ops", None)
        assert acc["id"] == other["id"]
        assert plan["session_id"] is None
        assert plan["failover"] is True

        req = SimpleNamespace(muse_role="qa", muse_project=None, role="docs",
                              project=None, user="role:ops:proj:from-user")
        assert app._chat_dimensions(req, "ops", None) == ("ops", "from-user")
        try:
            app._chat_dimensions(
                SimpleNamespace(muse_role=None, muse_project="x", role=None,
                                project=None, user="alice"),
                None, None)
            raise AssertionError("expected bad project")
        except HTTPException as exc:
            assert exc.status_code == 400

        from fastapi.testclient import TestClient
        client = TestClient(app.app)
        headers = {}
        if app.CFG.api_key:
            headers["Authorization"] = "Bearer " + app.CFG.api_key
        empty = client.post("/v1/chat/completions", headers=headers, json={
            "model": "muse-spark",
            "messages": [],
            "muse_role": "ops",
            "user": "role:ops",
        })
        assert empty.status_code == 400
        assert empty.json()["error"]["message"]
        bad = client.post("/v1/chat/completions", headers={**headers, "X-Muse-Role": "has space"},
                          json={"model": "muse-wonder",
                                "messages": [{"role": "user", "content": "hi"}]})
        assert bad.status_code == 400
        assert "X-Muse-Role" in bad.json()["error"]["message"]
    finally:
        app.store = old_store
        app.ledger = old_ledger
        for name in os.listdir(tmp):
            os.remove(os.path.join(tmp, name))
        os.rmdir(tmp)
    print("PASS: chat route and openai body")


def test_running_ledger_reloads_cli_map():
    """CLI 另开进程写盘后，长寿命的那份台账不用重建对象也能看见映射。"""
    path = tempfile.mktemp(suffix=".json")
    try:
        running = SidechatLedger(path)
        assert running.snapshot()["role_account_map"] == {}
        cli = SidechatLedger(path)
        cli.set_role_account("ops", "acc-ops")
        assert running.snapshot()["role_account_map"]["ops"] == "acc-ops"
        plan = running.plan("ops", None, _accounts())
        assert plan["account_id"] == "acc-ops"
        assert plan["reason"] == "role_map"
        running.record_result(
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-ops", account_label="ops",
            session_id="example-session-not-real", nav="fresh",
        )
        assert running.snapshot()["role_account_map"]["ops"] == "acc-ops"
        assert SidechatLedger(path).snapshot()["role_account_map"]["ops"] == "acc-ops"
    finally:
        if os.path.exists(path):
            os.remove(path)
    print("PASS: reload role map from disk")


def test_example_file_has_no_secrets():
    example = json.loads((ROOT / "data" / "sidechat_ledger.example.json").read_text(encoding="utf-8"))
    blob = json.dumps(example)
    assert "hatch_" not in blob
    assert "cookie" not in blob.lower()
    assert example["entries"][0]["session_id"] == "example-session-not-real"
    assert example["role_account_map"]["ops"] == "example_acc_ops"
    print("PASS: example ledger")


if __name__ == "__main__":
    test_keys_and_dimensions()
    test_plan_sticky_and_failover()
    test_cleanup_and_invalidate()
    test_store_pick_prefers_pinned_account()
    test_reset_thread_navigation()
    test_chat_route_and_openai_body()
    test_running_ledger_reloads_cli_map()
    test_example_file_has_no_secrets()
    print("ALL SIDECHAT TESTS PASSED")
