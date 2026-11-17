"""按账号互斥、按旁聊排队、全局并发上限。不启动浏览器，不访问 muse.ai。

运行：python tests/test_account_concurrency.py
"""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import config  # noqa: E402


class FakeStore:
    def __init__(self, ids):
        self.accounts = [{"id": i, "label": i, "enabled": True, "ok": True,
                          "cookies": {"k": "v"}} for i in ids]
        self.picked = []

    def get_account(self, aid):
        return next((a for a in self.accounts if a["id"] == aid), None)

    def pick_account(self, preferred_id=None, rotate=True, force_rotate=False,
                     exclude_id=None, exclude_ids=None, record_use=True):
        live = [a for a in self.accounts if a["id"] not in (exclude_ids or set())]
        if preferred_id and not rotate and not force_rotate:
            for a in live:
                if a["id"] == preferred_id:
                    return a
        if not live:
            return None
        acc = sorted(live, key=lambda a: a.get("n", 0))[0]
        acc["n"] = acc.get("n", 0) + 1
        self.picked.append(acc["id"])
        return acc

    def mark(self, *a, **k):
        pass


class FakeEngine:
    """记录每个账号同时在跑几个请求，以及全局峰值。"""

    def __init__(self, delay=0.4):
        self.delay = delay
        self.lock = threading.Lock()
        self.running: dict[str, int] = {}
        self.peak_per_account: dict[str, int] = {}
        self.peak_total = 0
        self.total = 0
        self.calls = []
        self.chat_warmup_sent = False
        self.gate = None  # (account_id, prompt) -> None，占着号时调用

    def start(self):
        pass

    def close_current_page(self):
        pass

    def reset_thread(self, *a, **k):
        return ""

    def chat_stream(self, cookies, prompt, exp, timeout, account_id=None, **kw):
        with self.lock:
            self.running[account_id] = self.running.get(account_id, 0) + 1
            self.total += 1
            self.peak_per_account[account_id] = max(
                self.peak_per_account.get(account_id, 0), self.running[account_id])
            self.peak_total = max(self.peak_total, self.total)
            self.calls.append((account_id, prompt, time.monotonic()))
        try:
            if self.gate is not None:
                self.gate(account_id, prompt)
            else:
                time.sleep(self.delay)
            yield f"reply:{prompt}"
        finally:
            with self.lock:
                self.running[account_id] -= 1
                self.total -= 1



def _install(ids, limit=8, delay=0.4, queue_timeout=5):
    fake_store = FakeStore(ids)
    fake_engine = FakeEngine(delay)
    commits = []
    app.store = fake_store
    app.engine = fake_engine
    app.ACCOUNT_POOL = app.AccountPoolManager(fake_store)
    app.GEN_LOCK = app.ConcurrencyLock(limit=limit)
    app.SIDECHAT_GATE = app.SidechatGate()
    app._renew_and_persist = lambda *a, **k: None
    app._sync_cookies = lambda *a, **k: None
    app._invalidate_sidechats = lambda *a, **k: None
    app._commit_sidechat = lambda plan, state, aid, accept_cursor=False: commits.append(
        ((plan or {}).get("ledger_key"), aid, time.monotonic()))
    app.CFG.queue_timeout = queue_timeout
    return fake_store, fake_engine, commits


def _run(results, name, *args, **kw):
    try:
        results[name] = "".join(app.safe_chat_stream(*args, **kw))
    except Exception as exc:  # noqa: BLE001
        results[name] = exc


def test_config_parsing():
    import os
    old = os.environ.get("MUSE2API_CONCURRENCY")
    try:
        os.environ["MUSE2API_CONCURRENCY"] = "auto"
        assert config.concurrency_limit() == 0
        os.environ["MUSE2API_CONCURRENCY"] = "3"
        assert config.concurrency_limit() == 3
        os.environ["MUSE2API_CONCURRENCY"] = "0"
        assert config.concurrency_limit() == 1
        os.environ["MUSE2API_CONCURRENCY"] = "junk"
        assert config.concurrency_limit() == 8
        os.environ.pop("MUSE2API_CONCURRENCY")
        assert config.concurrency_limit() == 8
    finally:
        if old is None:
            os.environ.pop("MUSE2API_CONCURRENCY", None)
        else:
            os.environ["MUSE2API_CONCURRENCY"] = old
    print("PASS: MUSE2API_CONCURRENCY parsing (int / auto)")


def test_dynamic_limit_and_cap():
    n = {"v": 2}
    lock = app.ConcurrencyLock(limit=1, limit_fn=lambda: n["v"])
    assert lock.acquire(timeout=0.05) and lock.acquire(timeout=0.05)
    assert not lock.acquire(timeout=0.05)
    n["v"] = 3
    assert lock.acquire(timeout=0.05)
    assert lock.active == 3
    for _ in range(3):
        lock.release()
    capped = app.ConcurrencyLock(limit=1, limit_fn=lambda: 10, cap=2)
    assert capped.current_limit() == 2
    zero = app.ConcurrencyLock(limit=1, limit_fn=lambda: 0)
    assert zero.current_limit() == 1
    print("PASS: auto limit follows usable accounts, cap applies, floor 1")


def test_different_accounts_run_in_parallel():
    _, eng, _ = _install(["a1", "a2", "a3"], limit=3)
    res = {}
    t0 = time.monotonic()
    ts = [threading.Thread(target=_run, args=(res, i, {}, f"p{i}", None, 30, aid))
          for i, aid in enumerate(["a1", "a2", "a3"])]
    [t.start() for t in ts]
    [t.join() for t in ts]
    took = time.monotonic() - t0
    assert all(res[i] == f"reply:p{i}" for i in range(3)), res
    assert eng.peak_total == 3, eng.peak_total
    assert took < 1.0, took
    print(f"PASS: 3 accounts ran in parallel ({took:.2f}s)")


def test_same_account_is_serialized_not_rotated():
    st, eng, _ = _install(["a1", "a2"], limit=3)
    res = {}
    ts = [threading.Thread(target=_run, args=(res, i, {}, f"p{i}", None, 30, "a1"))
          for i in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(res[i] == f"reply:p{i}" for i in range(3)), res
    assert eng.peak_per_account == {"a1": 1}, eng.peak_per_account
    assert all(c[0] == "a1" for c in eng.calls), eng.calls
    assert st.picked == [], st.picked  # 没有因为忙去挑别的号
    print("PASS: same account runs one at a time and never switches account when busy")


def test_global_limit_bounds_parallelism():
    _, eng, _ = _install(["a1", "a2", "a3", "a4"], limit=2)
    res = {}
    ts = [threading.Thread(target=_run, args=(res, i, {}, f"p{i}", None, 30, aid))
          for i, aid in enumerate(["a1", "a2", "a3", "a4"])]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert eng.peak_total == 2, eng.peak_total
    assert all(isinstance(v, str) for v in res.values()), res
    print("PASS: global limit caps parallel chats")


def test_busy_account_queue_timeout_no_failover():
    st, eng, commits = _install(["a1", "a2"], limit=3, delay=1.5, queue_timeout=1)
    res = {}
    first = threading.Thread(target=_run, args=(res, "first", {}, "p1", None, 30, "a1"))
    first.start()
    time.sleep(0.2)
    plan = {"strategy": "bc", "ledger_key": "role:pm:proj:t2", "session_id": "keep-me"}
    _run(res, "second", {}, "p2", None, 30, "a1", sidechat=plan)
    first.join()
    assert isinstance(res["second"], app.MuseQueueTimeout), res["second"]
    assert "排队超时" in str(res["second"])
    assert [c[0] for c in eng.calls] == ["a1"], eng.calls       # 第二条没发出去
    assert st.picked == [], st.picked                            # 没换号
    assert not any(k == "role:pm:proj:t2" for k, _, _ in commits), commits  # 没写台账
    assert plan["session_id"] == "keep-me"                       # 绑定没被改
    print("PASS: busy account -> clear queue timeout, no failover, no ledger write")


def test_global_slot_timeout_no_failover():
    st, eng, commits = _install(["a1", "a2"], limit=1, delay=1.5, queue_timeout=1)
    res = {}
    first = threading.Thread(target=_run, args=(res, "first", {}, "p1", None, 30, "a1"))
    first.start()
    time.sleep(0.2)
    _run(res, "second", {}, "p2", None, 30, "a2",
         sidechat={"strategy": "bc", "ledger_key": "role:ui:proj:t3"})
    first.join()
    assert isinstance(res["second"], app.MuseQueueTimeout), res["second"]
    assert [c[0] for c in eng.calls] == ["a1"], eng.calls
    assert st.picked == [], st.picked
    assert app.ACCOUNT_POOL.busy_accounts == set()
    assert app.GEN_LOCK.active == 0
    print("PASS: global slot timeout -> clear error, lease released, no failover")


def test_sidechat_gate():
    gate = app.SidechatGate(unstarted_grace=0.3)
    a = gate.acquire("role:pm:proj:x", time.monotonic() + 1)
    a.mark_started()
    other = gate.acquire("role:pm:proj:y", time.monotonic() + 0.1)  # 不同旁聊不互斥
    other.release()
    t0 = time.monotonic()
    try:
        gate.acquire("role:pm:proj:x", time.monotonic() + 0.3)
        raise AssertionError("same sidechat must wait")
    except app.MuseQueueTimeout as exc:
        assert "role:pm:proj:x" in str(exc)
    assert time.monotonic() - t0 >= 0.25
    got = {}

    def later():
        got["lease"] = gate.acquire("role:pm:proj:x", time.monotonic() + 2)
        got["at"] = time.monotonic()

    th = threading.Thread(target=later)
    th.start()
    time.sleep(0.2)
    released_at = time.monotonic()
    a.release()
    a.release()  # 幂等
    th.join()
    assert got["at"] >= released_at
    got["lease"].release()
    # 从未开工的占用会被回收
    stale = gate.acquire("role:ops", time.monotonic() + 1)
    t1 = time.monotonic()
    fresh = gate.acquire("role:ops", time.monotonic() + 2)
    assert time.monotonic() - t1 >= 0.25 and stale.released
    fresh.release()
    assert gate.snapshot() == {"held": [], "waiting": {}}
    none = gate.acquire(None, time.monotonic())  # 策略 A 不排队
    none.release()
    print("PASS: sidechat gate serializes one key, frees others, reclaims unstarted")


def test_endpoint_same_sidechat_waits_for_commit():
    """同一 role+project 的两条请求：第二条要在第一条写回台账之后才读台账。"""
    from fastapi.testclient import TestClient

    _, eng, commits = _install(["a1", "a2"], limit=3, delay=0.5, queue_timeout=10)
    app.CFG.api_key = ""
    routes = []

    def fake_route(role, project):
        routes.append(((role, project), time.monotonic()))
        acc = app.store.get_account("a1" if project == "t1" else "a2")
        return acc, {"strategy": "bc", "ledger_key": app.ledger_key(role, project),
                      "role": role, "project": project, "account_id": acc["id"]}

    app._route_chat_account = fake_route
    app.compose_chat_prompt = lambda msgs, plan, **k: {
        "prompt": msgs[-1].content, "full_prompt": msgs[-1].content, "sidechat": plan}
    app._thread_prompt_args = lambda delivery, full: {}
    app._relay_prepare = lambda **k: None
    app._bind_relay_turn = lambda **k: (None, {})
    client = TestClient(app.app)
    out = {}

    def call(name, project, stream=False):
        r = client.post("/v1/chat/completions",
                        headers={"X-Muse-Role": "pm", "X-Muse-Project": project},
                        json={"model": "muse-spark", "stream": stream,
                              "messages": [{"role": "user", "content": name}]})
        out[name] = (r.status_code, r.text)

    ts = [threading.Thread(target=call, args=("m1", "t1")),
          threading.Thread(target=call, args=("m2", "t1", True)),
          threading.Thread(target=call, args=("m3", "t2"))]
    for t in ts:
        t.start()
        time.sleep(0.05)
    [t.join() for t in ts]
    assert out["m1"][0] == 200 and out["m2"][0] == 200 and out["m3"][0] == 200, out
    import json as _json
    text = "".join(
        (_json.loads(line[6:])["choices"] or [{}])[0].get("delta", {}).get("content") or ""
        for line in out["m2"][1].splitlines()
        if line.startswith("data: {") and '"choices"' in line)
    assert text == "reply:m2", out["m2"][1][:500]
    t1_routes = [ts_ for (rp, ts_) in routes if rp == ("pm", "t1")]
    t1_commits = [ts_ for (k, _, ts_) in commits if k == "role:pm:proj:t1"]
    assert len(t1_routes) == 2 and len(t1_commits) == 2
    assert t1_routes[1] >= t1_commits[0], (routes, commits)   # 第二条等第一条写完台账
    assert eng.peak_per_account.get("a1") == 1
    assert eng.peak_total == 2, eng.peak_total                 # t2 与 t1 并行
    assert app.SIDECHAT_GATE.snapshot() == {"held": [], "waiting": {}}
    print("PASS: endpoint serializes one sidechat until ledger commit; other tickets run in parallel")


def _temp_ledger():
    import os
    import tempfile

    from sidechat import SidechatLedger

    path = tempfile.mktemp(suffix=".json")
    old = app.ledger
    app.ledger = SidechatLedger(path)
    return path, old, os


def _restore_ledger(path, old, os_mod):
    app.ledger = old
    if path and os_mod.path.exists(path):
        os_mod.remove(path)


def test_concurrent_new_tickets_get_different_accounts():
    """同一 role、不同工单同时到达：各占一个空闲号，不挤在 role_account_map 上。"""
    _, eng, _ = _install(["a1", "a2", "a3"], limit=4, queue_timeout=5)
    app.store.accounts[2]["ok"] = False  # a3 会话无效，不能拿来接新工单
    path, old, os_mod = _temp_ledger()
    app.ledger.set_role_account("ops", "a1")
    release = threading.Event()

    def gate(_account_id, _prompt):
        if len(eng.calls) >= 2:
            release.set()
        assert release.wait(2), eng.calls

    eng.gate = gate
    try:
        res = {}

        def go(project):
            acc, plan = app._route_chat_account("ops", project)
            assert plan["spread_idle"] is True
            assert plan["reason"] == "role_map"
            assert plan["account_id"] == "a1"
            assert plan["session_id"] is None
            _run(res, project, acc["cookies"], project, None, 30, acc["id"], sidechat=plan)

        threads = [threading.Thread(target=go, args=(name,)) for name in ("t1", "t2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert res["t1"] == "reply:t1" and res["t2"] == "reply:t2", res
        used = [call[0] for call in eng.calls]
        assert set(used) == {"a1", "a2"}, used
        assert eng.peak_per_account.get("a1") == 1
        assert eng.peak_per_account.get("a2") == 1
        assert eng.peak_total == 2
        assert "a3" not in used
        assert app.ledger.snapshot()["role_account_map"] == {"ops": "a1"}
    finally:
        release.set()
        _restore_ledger(path, old, os_mod)
    print("PASS: two concurrent new tickets land on different accounts")


def test_existing_sidechat_keeps_its_account():
    """已有旁聊的工单继续用原账号，即使映射账号正空闲也不换。"""
    from sidechat import ledger_key

    _, eng, _ = _install(["a1", "a2"], limit=4, queue_timeout=5)
    path, old, os_mod = _temp_ledger()
    app.ledger.set_role_account("ops", "a1")
    app.ledger.record_result(
        ledger_key=ledger_key("ops", "ticket"),
        role="ops", project="ticket",
        account_id="a2", account_label="a2",
        session_id="example-session-not-real", nav="fresh",
    )
    release = threading.Event()

    def gate(_account_id, prompt):
        if prompt == "hold":
            release.wait(3)

    eng.gate = gate
    try:
        res = {}
        hold = threading.Thread(target=_run, args=(res, "hold", {}, "hold", None, 30, "a2"))
        hold.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(call[1] == "hold" for call in eng.calls):
            time.sleep(0.02)
        assert any(call[1] == "hold" for call in eng.calls), eng.calls
        acc, plan = app._route_chat_account("ops", "ticket")
        assert plan["spread_idle"] is False
        assert plan["account_id"] == "a2"
        assert plan["session_id"] == "example-session-not-real"
        ticket = threading.Thread(
            target=_run,
            args=(res, "ticket", acc["cookies"], "ticket", None, 30, acc["id"]),
            kwargs={"sidechat": plan},
        )
        ticket.start()
        time.sleep(0.3)
        assert [call[1] for call in eng.calls] == ["hold"], eng.calls
        release.set()
        hold.join(3)
        ticket.join(3)
        assert res["ticket"] == "reply:ticket", res
        assert [call[0] for call in eng.calls if call[1] == "ticket"] == ["a2"]
        assert eng.peak_per_account["a2"] == 1
        assert all(call[0] != "a1" for call in eng.calls)
        snap = app.ledger.snapshot()
        assert snap["role_account_map"] == {"ops": "a1"}
        entry = next(item for item in snap["entries"] if item["ledger_key"] == "role:ops:proj:ticket")
        assert entry["muse_account_id"] == "a2"
        assert entry["session_id"] == "example-session-not-real"
    finally:
        release.set()
        _restore_ledger(path, old, os_mod)
    print("PASS: existing sidechat keeps its original account")


def test_new_ticket_queues_when_no_account_is_idle():
    """没有空闲号时排队，不抢占正在忙的账号，超时不写台账。"""
    _, eng, commits = _install(["a1", "a2", "a3"], limit=4, queue_timeout=5)
    app.store.accounts[2]["ok"] = False
    path, old, os_mod = _temp_ledger()
    app.ledger.set_role_account("ops", "a1")
    release = threading.Event()

    def gate(_account_id, prompt):
        if str(prompt).startswith("hold"):
            release.wait(3)

    eng.gate = gate
    try:
        res = {}
        holders = []
        for aid in ("a1", "a2"):
            thread = threading.Thread(
                target=_run, args=(res, aid, {}, f"hold-{aid}", None, 30, aid))
            holders.append(thread)
            thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(eng.calls) < 2:
            time.sleep(0.02)
        assert len(eng.calls) == 2, eng.calls

        app.CFG.queue_timeout = 0.4

        def late():
            acc, plan = app._route_chat_account("ops", "late")
            assert plan["spread_idle"] is True
            _run(res, "late", acc["cookies"], "late", None, 30, acc["id"], sidechat=plan)

        late_thread = threading.Thread(target=late)
        late_thread.start()
        late_thread.join(2)
        assert isinstance(res["late"], app.MuseQueueTimeout), res["late"]
        assert "排队超时" in str(res["late"])
        assert not any(call[1] == "late" for call in eng.calls), eng.calls
        assert not any(key == "role:ops:proj:late" for key, _, _ in commits), commits
        assert eng.peak_per_account.get("a1") == 1
        assert eng.peak_per_account.get("a2") == 1

        app.CFG.queue_timeout = 5

        def fresh():
            acc, plan = app._route_chat_account("ops", "fresh")
            _run(res, "fresh", acc["cookies"], "fresh", None, 30, acc["id"], sidechat=plan)

        fresh_thread = threading.Thread(target=fresh)
        fresh_thread.start()
        time.sleep(0.3)
        assert not any(call[1] == "fresh" for call in eng.calls), eng.calls
        release.set()
        fresh_thread.join(3)
        for thread in holders:
            thread.join(3)
        assert res["fresh"] == "reply:fresh", res
        fresh_accounts = [call[0] for call in eng.calls if call[1] == "fresh"]
        assert fresh_accounts in (["a1"], ["a2"]), fresh_accounts
        assert eng.peak_per_account["a1"] == 1
        assert eng.peak_per_account["a2"] == 1
        assert "a3" not in [call[0] for call in eng.calls]
        assert app.ledger.snapshot()["role_account_map"] == {"ops": "a1"}
    finally:
        release.set()
        _restore_ledger(path, old, os_mod)
    print("PASS: no idle account queues instead of preempting")


if __name__ == "__main__":
    test_config_parsing()
    test_dynamic_limit_and_cap()
    test_different_accounts_run_in_parallel()
    test_same_account_is_serialized_not_rotated()
    test_global_limit_bounds_parallelism()
    test_busy_account_queue_timeout_no_failover()
    test_global_slot_timeout_no_failover()
    test_sidechat_gate()
    test_concurrent_new_tickets_get_different_accounts()
    test_existing_sidechat_keeps_its_account()
    test_new_ticket_queues_when_no_account_is_idle()
    test_endpoint_same_sidechat_waits_for_commit()
    print("ALL ACCOUNT CONCURRENCY TESTS PASSED")
