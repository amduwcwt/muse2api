"""对话正文台账。不使用真实 cookie、密钥或线上账号。

运行：python tests/test_relay_ledger.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from redact import redact_text, scrub  # noqa: E402
from relay import MSG_CAP, TURN_CAP, RelayStore, content_hash  # noqa: E402

SECRET_TOKEN = "fakemuse-token-value"
SECRET_KEY = "m2a_0123456789abcdef0123456789abcdef"
SECRET_PEM = "SUPERSECRETKEYMATERIAL"
SECRET_PROXY = "s3cret-pass"
SECRET_COOKIE = "not-a-real-cookie"
SAMPLE = (
    f"please help Bearer {SECRET_TOKEN}\n"
    f"key {SECRET_KEY}\n"
    "-----BEGIN PRIVATE KEY-----\n"
    f"{SECRET_PEM}\n"
    "-----END PRIVATE KEY-----\n"
    f"socks5://user:{SECRET_PROXY}@127.0.0.1:1080\n"
    f"cookie_header=hatch_sess={SECRET_COOKIE}"
)


def _blob(path: str) -> bytes:
    data = Path(path).read_bytes()
    for suffix in ("-wal", "-shm"):
        extra = path + suffix
        if os.path.exists(extra):
            data += Path(extra).read_bytes()
    return data


def _assert_clean(blob: bytes):
    text = blob.decode("utf-8", "ignore")
    for secret in (SECRET_TOKEN, SECRET_KEY, SECRET_PEM, SECRET_PROXY, SECRET_COOKIE):
        assert secret not in text, secret


def _messages(store: RelayStore, conversation_id: str, source: str | None = None):
    rows = store.messages_for(conversation_id, restore=False)
    if source:
        rows = [row for row in rows if row["source"] == source]
    return rows


def test_redact_and_scrub():
    cleaned = redact_text(SAMPLE)
    _assert_clean(cleaned.encode())
    assert "«redacted»" in cleaned
    payload = scrub({
        "notes": SAMPLE,
        "token": "should-drop",
        "password": "should-drop",
        "private_key": "should-drop",
        "session_id": "example-session-not-real",
        "nested": [{"api_key": "nope", "label": "ops"}],
    })
    assert "token" not in payload
    assert "password" not in payload
    assert "private_key" not in payload
    assert "api_key" not in payload["nested"][0]
    assert payload["session_id"] == "example-session-not-real"
    _assert_clean(json.dumps(payload).encode())
    print("PASS: redact")


def test_schema_and_incremental():
    tmp = tempfile.mkdtemp(prefix="relay-")
    path = os.path.join(tmp, "relay.sqlite")
    side = os.path.join(tmp, "accounts.json")
    media = os.path.join(tmp, "media")
    os.makedirs(media)
    Path(side).write_text('{"accounts":[]}', encoding="utf-8")
    Path(os.path.join(media, "keep.png")).write_bytes(b"png")
    store = RelayStore(path, retention_days=14, max_bytes=400 * 1024 * 1024)
    try:
        journal = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(journal).lower() == "wal"
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert store._conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        names = {row[0] for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"conversations", "messages", "actions"} <= names
        assert store._conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0

        first = store.record_turn(
            client_id="ticket-42", client_supplied=True,
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-fixture", account_label="fixture",
            session_id="sess-example-1", model="muse-spark", nav="fresh",
            request_id="chatcmpl-example1",
            messages=[
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hello-unique " + SAMPLE},
                {"role": "assistant", "content": "old-assistant-should-not-stick"},
                {"role": "user", "content": "later-user-not-on-first-turn"},
            ],
            reply="muse-reply-ok",
            prompt_chars=120, prompt_hash="ab" * 32,
        )
        cid = first["conversation_id"]
        assert cid.startswith("conv_")
        assert first["client_id"] == "ticket-42"
        request_rows = _messages(store, cid, "request_delta")
        assert [row["content"] for row in request_rows][0] == "be brief"
        assert len(request_rows) == 2
        assert "hello-unique" in request_rows[1]["content"]
        assert "later-user-not-on-first-turn" not in json.dumps(request_rows)
        assert "old-assistant-should-not-stick" not in json.dumps(request_rows)
        _assert_clean(_blob(path))

        second = store.record_turn(
            client_id="ticket-42", client_supplied=True,
            account_id="acc-other", account_label="other",
            session_id="sess-rolled-02", model="muse-wonder", nav="rollover",
            request_id="chatcmpl-example2", warmup=True,
            messages=[
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hello-unique " + SAMPLE},
                {"role": "assistant", "content": "muse-reply-ok"},
                {"role": "user", "content": "second-question"},
            ],
            reply="muse-reply-two",
            prompt_chars=80, prompt_hash="cd" * 32,
        )
        assert second["conversation_id"] == cid
        assert second["session_id"] == "sess-rolled-02"
        assert second["account_id"] == "acc-other"
        assert len(store.list_conversations()) == 1
        hello = [row for row in _messages(store, cid, "request_delta")
                 if "hello-unique" in row["content"]]
        assert len(hello) == 1
        assert any(row["content"] == "second-question" for row in _messages(store, cid, "request_delta"))
        replies = _messages(store, cid, "muse_reply")
        assert [row["content"] for row in replies] == ["muse-reply-ok", "muse-reply-two"]
        assert any(row["source"] == "warmup" and row["content"] == "." for row in store.messages_for(cid))
        meta = _messages(store, cid, "meta")
        blob = " ".join(row["content"] for row in meta)
        assert "prompt_chars" in blob and "prompt_hash" in blob
        assert "背景与任务设定" not in _blob(path).decode("utf-8", "ignore")

        restored = store.messages_for(cid, restore=True)
        assert all(row["source"] != "warmup" for row in restored)
        assert "." not in [row["content"] for row in restored]
        assert store.lookup("ticket-42")["id"] == cid
        assert store.lookup(cid)["session_id"] == "sess-rolled-02"

        anon_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "only-last-user"},
        ]
        anon = store.record_turn(messages=anon_messages, reply="anon-reply", client_supplied=False)
        anon_req = _messages(store, anon["conversation_id"], "request_delta")
        assert [row["content"] for row in anon_req] == ["only-last-user"]
        assert anon["conversation_id"] != cid

        huge = "H" * (MSG_CAP + 4096)
        clipped = store.record_turn(
            client_id="ticket-clip", client_supplied=True,
            messages=[{"role": "user", "content": huge}],
            reply="R" * (MSG_CAP + 4096),
        )
        bodies = _messages(store, clipped["conversation_id"])
        total = 0
        for row in bodies:
            size = len(row["content"].encode("utf-8"))
            assert size <= MSG_CAP
            total += size
            if row["source"] in ("request_delta", "muse_reply"):
                assert row["truncated"] == 1
                assert row["char_count"] > MSG_CAP
                assert row["content"].endswith("«truncated»")
        assert total <= TURN_CAP

        exported = "".join(store.iter_jsonl(clipped["conversation_id"]))
        assert exported.count("\n") >= 2
        for line in exported.splitlines():
            json.loads(line)
        _assert_clean(exported.encode())
        _assert_clean(_blob(path))
        assert Path(side).read_text(encoding="utf-8").startswith("{")
        assert Path(os.path.join(media, "keep.png")).read_bytes() == b"png"
    finally:
        store.close()
    print("PASS: incremental")


def test_cleanup_age_and_size():
    tmp = tempfile.mkdtemp(prefix="relay-clean-")
    path = os.path.join(tmp, "relay.sqlite")
    side = os.path.join(tmp, "accounts.json")
    media = os.path.join(tmp, "media")
    os.makedirs(media)
    Path(side).write_text("{}", encoding="utf-8")
    Path(os.path.join(media, "keep.png")).write_bytes(b"png")
    store = RelayStore(path, retention_days=14, max_bytes=400 * 1024 * 1024)
    try:
        old = store.record_turn(
            client_id="old-conv", client_supplied=True, at=1_000,
            messages=[{"role": "user", "content": "old-body"}], reply="old",
        )
        new = store.record_turn(
            client_id="new-conv", client_supplied=True, at=2_000_000,
            messages=[{"role": "user", "content": "new-body"}], reply="new",
        )
        aged = store.cleanup(now=2_000_000, retention_days=14, max_bytes=10**12)
        assert old["conversation_id"] in aged["conversation_ids"]
        assert store.get_conversation(old["conversation_id"]) is None
        assert store.get_conversation(new["conversation_id"])["client_id"] == "new-conv"
    finally:
        store.close()

    size_store = RelayStore(os.path.join(tmp, "size.sqlite"), retention_days=14, max_bytes=400 * 1024 * 1024)
    try:
        big = "y" * 20000
        older = size_store.record_turn(
            client_id="size-old", client_supplied=True, at=10,
            messages=[{"role": "user", "content": big}], reply="a",
        )
        newer = size_store.record_turn(
            client_id="size-new", client_supplied=True, at=20,
            messages=[{"role": "user", "content": big}], reply="b",
        )
        sized = size_store.cleanup(now=100, retention_days=10000, max_bytes=newer["bytes"])
        assert older["conversation_id"] in sized["conversation_ids"]
        assert size_store.get_conversation(older["conversation_id"]) is None
        assert size_store.get_conversation(newer["conversation_id"]) is not None
        assert Path(side).is_file()
        assert Path(os.path.join(media, "keep.png")).is_file()
        assert "accounts.json" in sized["note"]
    finally:
        size_store.close()
    print("PASS: cleanup")


def test_sidechat_json_stays_router():
    from sidechat import SidechatLedger
    path = os.path.join(tempfile.mkdtemp(prefix="ledger-"), "sidechat_ledger.json")
    Path(path).write_text(json.dumps({
        "version": 1,
        "entries": [{
            "ledger_key": "role:ops",
            "notes": SAMPLE,
            "token": "should-drop",
            "session_id": "example-session-not-real",
            "status": "active",
        }],
        "role_account_map": {"ops": "acc-ops"},
    }), encoding="utf-8")
    ledger = SidechatLedger(path)
    ledger.set_role_account("qa", "acc-qa")
    blob = Path(path).read_text(encoding="utf-8")
    _assert_clean(blob.encode())
    assert "should-drop" not in blob
    assert "example-session-not-real" in blob
    assert "hello-unique" not in blob
    print("PASS: sidechat scrub")


def test_routes_require_auth_and_chat_hook():
    import app
    from engine import MuseEngine
    from fastapi.routing import APIRoute

    eng = MuseEngine.__new__(MuseEngine)
    sent = []

    def select(_card):
        if sent:
            return {"ok": True, "text": "Wonder"}
        return {"ok": False, "err": "missing"}

    eng._select_chat_model_once = select
    eng._send = lambda text: sent.append(text)
    eng._wait_generation_idle = lambda **_kwargs: None
    assert eng._ensure_chat_model("muse-wonder", wait_s=0) == "wonder"
    assert sent == ["."]
    assert eng.chat_warmup_sent is True
    assert eng._ensure_chat_model("auto", wait_s=0) == "auto"
    assert eng.chat_warmup_sent is False

    protected = []
    for route in app.app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path.startswith("/admin/relay") or route.path.startswith("/v1/conversations"):
            calls_dep = []

            def walk(dep):
                if getattr(dep, "call", None):
                    calls_dep.append(dep.call)
                for sub in getattr(dep, "dependencies", []) or []:
                    walk(sub)

            walk(route.dependant)
            assert any(getattr(fn, "__name__", "") == "auth" for fn in calls_dep), route.path
            protected.append(route.path)
    assert "/admin/relay/conversations" in protected
    assert "/admin/relay/export" in protected
    assert "/v1/conversations/messages" in protected
    assert "/v1/conversations/{client_id}/messages" in protected

    from fastapi.testclient import TestClient

    tmp = tempfile.mkdtemp(prefix="relay-app-")
    store = RelayStore(os.path.join(tmp, "relay.sqlite"))
    ledger_path = os.path.join(tmp, "sidechat_ledger.json")
    old = {
        "relay": app.relay,
        "ledger": app.ledger,
        "route": app._route_chat_account,
        "stream": app.safe_chat_stream,
        "key": app.CFG.api_key,
    }
    app.relay = store
    from sidechat import SidechatLedger
    app.ledger = SidechatLedger(ledger_path)

    def route(_role, _project):
        return (
            {"id": "acc-routed", "label": "routed", "cookies": {"hatch_sess": SECRET_COOKIE},
             "cookies_exp": None},
            {"strategy": "bc", "ledger_key": "role:ops", "role": "ops", "project": None,
             "account_id": "acc-routed", "account_label": "fixture",
             "session_id": "sess-example-1"},
        )

    def fake_stream(cookies, prompt, expires, timeout, account_id=None, chat_model=None,
                    sidechat=None, on_finished=None, capture=None):
        reply = "muse-reply-two" if "second-question" in (prompt or "") else "muse-reply-ok"
        snap = {
            "account_id": "acc-actual",
            "warmup": bool(chat_model and "wonder" in str(chat_model)),
            "state": {
                "nav": "rollover" if "second-question" in (prompt or "") else "resumed",
                "session_id": "sess-rolled-02" if "second-question" in (prompt or "") else "sess-example-1",
            },
            "reply": reply,
            "error": None,
            "latency_ms": 4,
        }
        if on_finished:
            on_finished(snap)
        yield reply

    app._route_chat_account = route
    app.safe_chat_stream = fake_stream
    app.CFG.api_key = "m2a_test_only_key"
    handler = logging.Handler()
    handler.messages = []

    def emit(record):
        handler.messages.append(record.getMessage())

    handler.emit = emit
    app.log.addHandler(handler)
    try:
        client = TestClient(app.app)
        bare = client.get("/admin/relay/conversations")
        assert bare.status_code == 401
        assert client.get("/admin/relay/export").status_code == 401
        assert client.post("/admin/relay/cleanup").status_code == 401
        assert client.get("/v1/conversations/messages").status_code == 401
        assert client.get("/v1/conversations/ticket-42/messages").status_code == 401
        assert client.get(
            "/admin/relay/conversations",
            headers={"Authorization": "Bearer wrong-key"},
        ).status_code == 401
        auth = {"Authorization": "Bearer m2a_test_only_key"}
        assert client.get("/admin/relay/conversations", headers=auth).status_code == 200

        headers = {
            **auth,
            "X-Muse-Conversation": "ticket-42",
            "X-Muse-Role": "ops",
        }
        first = client.post("/v1/chat/completions", headers=headers, json={
            "model": "muse-spark",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hello-unique " + SAMPLE},
            ],
        })
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["choices"][0]["message"]["content"] == "muse-reply-ok"
        assert body["muse"]["client_id"] == "ticket-42"
        assert body["muse"]["session_id"] == "sess-example-1"
        assert body["muse"]["nav"] == "resumed"
        assert body["muse"]["account_id"] == "acc-actual"
        assert first.headers["x-muse-conversation"] == "ticket-42"
        assert first.headers["x-muse-session-id"] == "sess-example-1"
        assert "object" in body and body["object"] == "chat.completion"

        second = client.post("/v1/chat/completions", headers=headers, json={
            "model": "muse-wonder",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hello-unique " + SAMPLE},
                {"role": "assistant", "content": "muse-reply-ok"},
                {"role": "user", "content": "second-question"},
            ],
        })
        assert second.status_code == 200, second.text
        assert second.json()["choices"][0]["message"]["content"] == "muse-reply-two"
        assert second.json()["muse"]["conversation_id"] == body["muse"]["conversation_id"]
        assert second.json()["muse"]["session_id"] == "sess-rolled-02"
        cid = body["muse"]["conversation_id"]
        listed = client.get("/admin/relay/conversations", headers=auth).json()
        assert len(listed["conversations"]) == 1
        detail = client.get(f"/admin/relay/conversations/{cid}", headers=auth)
        assert detail.status_code == 200
        sources = [row["source"] for row in detail.json()["messages"]]
        assert sources.count("request_delta") >= 1
        assert "warmup" in sources
        hello = [row for row in detail.json()["messages"]
                 if row["source"] == "request_delta" and "hello-unique" in row["content"]]
        assert len(hello) == 1
        restored = client.get("/v1/conversations/messages", headers={
            **auth, "X-Muse-Conversation": "ticket-42",
        })
        assert restored.status_code == 200
        restored_body = restored.json()
        assert restored_body["conversation_id"] == cid
        assert all(row["source"] != "warmup" for row in restored_body["messages"])
        assert all(row["content"] != "." for row in restored_body["messages"])
        by_path = client.get("/v1/conversations/ticket-42/messages", headers=auth)
        assert by_path.status_code == 200
        assert by_path.json()["session_id"] == "sess-rolled-02"
        exported = client.get(f"/admin/relay/conversations/{cid}/export", headers=auth)
        assert exported.status_code == 200
        assert "application/x-ndjson" in exported.headers["content-type"]
        _assert_clean(exported.content)
        _assert_clean(_blob(store.path))
        assert SECRET_COOKIE not in _blob(store.path).decode("utf-8", "ignore")
        assert "背景与任务设定" not in _blob(store.path).decode("utf-8", "ignore")
        ledger_blob = Path(ledger_path).read_text(encoding="utf-8") if os.path.exists(ledger_path) else ""
        assert "muse-reply-two" not in ledger_blob
        assert "hello-unique" not in ledger_blob
        joined = "\n".join(handler.messages)
        for secret in (SECRET_TOKEN, SECRET_KEY, SECRET_PEM, "hello-unique", "muse-reply-two"):
            assert secret not in joined

        streamed = client.post("/v1/chat/completions", headers=headers, json={
            "model": "muse-spark",
            "stream": True,
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hello-unique " + SAMPLE},
                {"role": "assistant", "content": "muse-reply-ok"},
                {"role": "user", "content": "second-question"},
                {"role": "assistant", "content": "muse-reply-two"},
                {"role": "user", "content": "third-question"},
            ],
        })
        assert streamed.status_code == 200, streamed.text[:400]
        assert '"content": "mu"' in streamed.text
        assert "x-muse-conversation-id" in streamed.headers
        assert '"muse"' in streamed.text

        replied = client.post("/v1/responses", headers=headers, json={
            "model": "muse-spark",
            "input": "response-only-user",
        })
        assert replied.status_code == 200, replied.text
        assert replied.json()["output"][0]["content"][0]["text"] == "muse-reply-ok"
        assert replied.json()["muse"]["client_id"] == "ticket-42"

        def boom(*_args, **_kwargs):
            raise OSError("disk")

        app.relay.record_turn = boom
        app.relay.ensure_conversation = boom
        failed = client.post("/v1/chat/completions", headers=auth, json={
            "model": "muse-spark",
            "messages": [{"role": "user", "content": "still-ok " + SECRET_TOKEN}],
        })
        assert failed.status_code == 200, failed.text
        assert failed.json()["choices"][0]["message"]["content"] == "muse-reply-ok"
        assert any("relay persist failed" in line for line in handler.messages)
        assert SECRET_TOKEN not in "\n".join(handler.messages)
    finally:
        app.log.removeHandler(handler)
        app.relay = old["relay"]
        app.ledger = old["ledger"]
        app._route_chat_account = old["route"]
        app.safe_chat_stream = old["stream"]
        app.CFG.api_key = old["key"]
        store.close()
    print("PASS: auth and chat hook")


if __name__ == "__main__":
    test_redact_and_scrub()
    test_schema_and_incremental()
    test_cleanup_age_and_size()
    test_sidechat_json_stays_router()
    test_routes_require_auth_and_chat_hook()
    print("ALL RELAY TESTS PASSED")
