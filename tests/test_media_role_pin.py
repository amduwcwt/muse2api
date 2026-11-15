"""Pin image and video generation to role_account_map. No browser, no live account.

Run: python tests/test_media_role_pin.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SENTINEL_DRAMA = "COOKIE_SENTINEL_drama"
SENTINEL_OTHER = "COOKIE_SENTINEL_other"


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body)


class Client:
    def __init__(self, app, headers):
        self.app, self.headers = app, headers

    async def request(self, method, path, json_data=None, data=None, files=None, headers=None):
        hdr = {**self.headers, **(headers or {})}
        body = b""
        if files is not None:
            boundary = "muse-test-boundary"
            chunks = []
            for key, value in (data or {}).items():
                chunks.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
                )
            for key, (filename, payload, mime) in files.items():
                chunks.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                    f"Content-Type: {mime}\r\n\r\n".encode() + payload + b"\r\n"
                )
            body = b"".join(chunks) + f"--{boundary}--\r\n".encode()
            hdr["content-type"] = "multipart/form-data; boundary=" + boundary
        elif json_data is not None:
            body = json.dumps(json_data).encode()
            hdr["content-type"] = "application/json"
        hdr["content-length"] = str(len(body))
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in hdr.items()],
            "server": ("test", 80), "client": ("test", 1),
        }
        messages = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        return Response(status, b"".join(m.get("body", b"") for m in messages))

    async def post(self, path, json=None, **kwargs):
        return await self.request("POST", path, json_data=json, **kwargs)

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)


def load(home):
    os.environ.update(
        MUSE2API_HOME=home,
        MUSE2API_PROFILE_ROOT=home,
        MUSE2API_KEY="test-only",
        MUSE2API_PUBLIC_BASE="",
    )
    spec = importlib.util.spec_from_file_location("media_role_pin_app", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parsed(response, status):
    text = response.body.decode()
    assert SENTINEL_DRAMA not in text and SENTINEL_OTHER not in text, text
    assert "hatch_sess" not in text and "hatch_vml" not in text, text
    data = json.loads(text) if text else {}
    assert response.status_code == status, (status, response.status_code, text)
    return data


def prefer_other(drama, other):
    """Rotation should prefer `other`. A pin that ignores the map will fail the test."""
    drama["last_used"] = time.time()
    drama["use_count"] = 9
    other["last_used"] = 0
    other["use_count"] = 0


async def wait_task(client, path):
    last = None
    for _ in range(100):
        result = await client.get(path)
        last = parsed(result, 200)
        if last.get("status") in ("completed", "failed"):
            return last
        await asyncio.sleep(0.01)
    raise AssertionError(f"task never finished: {last}")


def install_engine(module, used, state):
    def generate(cookies, prompt, expect="image", **kwargs):
        aid = kwargs.get("account_id")
        used.append(aid)
        blob = json.dumps(cookies)
        assert SENTINEL_DRAMA in blob or SENTINEL_OTHER in blob
        if state["fails_left"] > 0:
            state["fails_left"] -= 1
            raise state["error"]
        return {"filename": f"{expect}-pin.webp", "size": 4, "kind": expect, "path": ""}

    def renew(ident, **_kwargs):
        return module.store.get_account(ident)

    module.engine.generate = generate
    module.engine.start = lambda: None
    module.engine.close_current_page = lambda: None
    module.engine.reset_thread = lambda: None
    module._renew_and_persist = renew
    module._sync_cookies = lambda *_a, **_k: {"synced": 0}


async def http_cases(module, drama, other):
    from sidechat import SidechatLedger

    used = []
    state = {"fails_left": 0, "error": None}
    install_engine(module, used, state)
    acquired = []
    orig_acquire = module.ACCOUNT_POOL.acquire

    def spy_acquire(*args, **kwargs):
        acquired.append(dict(kwargs))
        return orig_acquire(*args, **kwargs)

    module.ACCOUNT_POOL.acquire = spy_acquire
    plans = []
    orig_plan = module.ledger.plan

    def spy_plan(*args, **kwargs):
        plans.append(args)
        return orig_plan(*args, **kwargs)

    module.ledger.plan = spy_plan

    def forbid_record(*_args, **_kwargs):
        raise AssertionError("media request wrote a sidechat ledger entry")

    module.ledger.record_result = forbid_record
    client = Client(module.app, {"Authorization": "Bearer test-only"})
    drama_id, other_id = drama["id"], other["id"]
    ext = SidechatLedger(module.ledger.path)

    bad = await client.post(
        "/v1/images/generations",
        json={"prompt": "bad slug", "timeout": 5},
        headers={"X-Muse-Role": "has space"},
    )
    msg = parsed(bad, 400)["error"]["message"]
    assert "X-Muse-Role" in msg
    assert used == [] and acquired == []

    only_project = await client.post(
        "/v1/images/generations",
        json={"prompt": "project only", "timeout": 5},
        headers={"X-Muse-Project": "bridgeai"},
    )
    project_msg = parsed(only_project, 400)["error"]["message"]
    assert "role" in project_msg
    assert used == []

    bad_body = await client.post(
        "/v1/videos",
        json={"prompt": "bad body role", "muse_role": "has space", "timeout": 5},
    )
    assert "muse_role" in parsed(bad_body, 400)["error"]["message"]

    assert (module.ledger.data.get("role_account_map") or {}).get("drama") != drama_id
    ext.set_role_account("drama", drama_id)
    assert module.ledger.data.get("role_account_map", {}).get("drama") != drama_id

    prefer_other(drama, other)
    used.clear()
    acquired.clear()
    rotated = await client.post(
        "/v1/images/generations",
        json={"prompt": "unmapped role", "muse_role": "nomap", "timeout": 5},
    )
    rotated_body = parsed(rotated, 200)
    assert rotated_body["data"][0]["url"]
    assert rotated_body["muse"] == {"role": "nomap", "account_id": other_id}
    assert used == [other_id]
    assert acquired[0].get("account_id") is None
    assert acquired[0].get("force_rotate", False) is False
    print("PASS: unmapped role falls back to rotation")

    prefer_other(drama, other)
    used.clear()
    acquired.clear()
    pinned = await client.post(
        "/v1/images/generations",
        json={"prompt": "pinned poster", "timeout": 5, "muse_role": "someone-else"},
        headers={"X-Muse-Role": "Drama", "X-Muse-Project": "bridgeai"},
    )
    pinned_body = parsed(pinned, 200)
    assert pinned_body["muse"] == {"role": "drama", "account_id": drama_id}
    assert pinned_body["data"][0]["url"].endswith("image-pin.webp")
    assert used == [drama_id]
    assert acquired[0].get("account_id") == drama_id
    assert module.ledger.snapshot()["entries"] == []
    assert module.ledger.snapshot()["role_account_map"]["drama"] == drama_id
    print("PASS: pinned image sync")

    ext.set_role_account("drama", other_id)
    assert module.ledger.data.get("role_account_map", {}).get("drama") == drama_id
    used.clear()
    hot = await client.post(
        "/v1/images/generations",
        json={"prompt": "reloaded map", "timeout": 5},
        headers={"X-Muse-Role": "drama"},
    )
    assert parsed(hot, 200)["muse"]["account_id"] == other_id
    assert used == [other_id]
    ext.set_role_account("drama", drama_id)
    print("PASS: role map hot reload")

    prefer_other(drama, other)
    used.clear()
    queued = await client.post(
        "/v1/images/tasks",
        json={"prompt": "async pinned", "muse_role": "drama", "timeout": 5},
    )
    queued_body = parsed(queued, 202)
    assert queued_body["muse"] == {"role": "drama", "account_id": drama_id}
    task_id = queued_body["task_id"]
    done = await wait_task(client, "/v1/images/tasks/" + task_id)
    assert done["status"] == "completed", done.get("error")
    assert done["account"] == drama_id
    assert done["muse"] == {"role": "drama", "account_id": drama_id}
    assert used == [drama_id]
    print("PASS: pinned image async task")

    prefer_other(drama, other)
    used.clear()
    video = await client.post(
        "/v1/videos",
        json={"prompt": "stage lights", "duration": 4, "timeout": 5},
        headers={"X-Muse-Role": "drama"},
    )
    video_body = parsed(video, 200)
    assert video_body["muse"] == {"role": "drama", "account_id": drama_id}
    video_id = video_body["task_id"]
    finished = await wait_task(client, "/v1/videos/" + video_id)
    assert finished["status"] == "completed", finished.get("error")
    assert finished["account"] == drama_id
    assert finished["muse"] == {"role": "drama", "account_id": drama_id}
    assert used == [drama_id]
    alt = await client.get("/v1/videos/generations/" + video_id)
    assert parsed(alt, 200)["account"] == drama_id
    print("PASS: pinned video task")

    prefer_other(drama, other)
    used.clear()
    edited = await client.post(
        "/v1/images/edits",
        data={"prompt": "pinned edit", "muse_role": "drama"},
        files={"image": ("ref.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
    )
    edited_body = parsed(edited, 200)
    assert edited_body["muse"] == {"role": "drama", "account_id": drama_id}
    assert used == [drama_id]
    print("PASS: multipart edits with muse_role")

    ext.set_role_account("drama", None)
    ext.set_role_account("default", other_id)
    prefer_other(drama, other)
    drama["last_used"] = 0
    drama["use_count"] = 0
    other["last_used"] = time.time()
    other["use_count"] = 9
    used.clear()
    defaulted = await client.post(
        "/v1/images/generations",
        json={"prompt": "default map", "muse_role": "qa", "timeout": 5},
    )
    assert parsed(defaulted, 200)["muse"] == {"role": "qa", "account_id": other_id}
    assert used == [other_id]
    print("PASS: default mapping pins when the role has no entry")

    ext.set_role_account("default", None)
    ext.set_role_account("drama", drama_id)
    module.store.update_account(drama_id, enabled=False)
    used.clear()
    acquired.clear()
    disabled = await client.post(
        "/v1/images/generations",
        json={"prompt": "disabled pin", "timeout": 5},
        headers={"X-Muse-Role": "drama"},
    )
    disabled_msg = parsed(disabled, 409)["error"]["message"]
    assert "角色 drama" in disabled_msg and "不存在或已禁用" in disabled_msg
    assert drama_id not in disabled_msg and other_id not in disabled_msg
    assert used == [] and acquired == []
    module.store.update_account(drama_id, enabled=True)
    print("PASS: disabled pinned account errors")

    ext.set_role_account("drama", "missing-acc")
    used.clear()
    missing = await client.post(
        "/v1/videos",
        json={"prompt": "missing pin", "muse_role": "drama", "timeout": 5},
    )
    missing_msg = parsed(missing, 409)["error"]["message"]
    assert "角色 drama" in missing_msg and "不存在或已禁用" in missing_msg
    assert "missing-acc" not in missing_msg
    assert used == []
    ext.set_role_account("drama", drama_id)
    print("PASS: missing pinned account errors")

    assert plans == []
    assert module.ledger.snapshot()["entries"] == []
    module.ACCOUNT_POOL.acquire = orig_acquire
    return used, state


def retry_and_wait(module, drama, other, used, state):
    drama_id, other_id = drama["id"], other["id"]
    acquired = []
    orig_acquire = module.ACCOUNT_POOL.acquire

    def spy_acquire(*args, **kwargs):
        acquired.append(dict(kwargs))
        return orig_acquire(*args, **kwargs)

    module.ACCOUNT_POOL.acquire = spy_acquire

    prefer_other(drama, other)
    used.clear()
    acquired.clear()
    state["fails_left"] = 1
    state["error"] = module.MuseGenerationError("first attempt failed")
    res, acc = module._run_generation_locked(
        "pinned retry", "image", 30, account_id=drama_id, pinned=True,
        pin_role="drama", deadline=time.monotonic() + 30)
    assert res["kind"] == "image" and acc == drama_id
    assert used == [drama_id, drama_id]
    assert [call.get("account_id") for call in acquired] == [drama_id, drama_id]
    assert all(call.get("force_rotate", False) is False for call in acquired)
    print("PASS: pinned retry does not rotate")

    prefer_other(drama, other)
    used.clear()
    acquired.clear()
    state["fails_left"] = 5
    state["error"] = module.MuseGenerationError("still failing")
    try:
        module._run_generation_locked(
            "pinned fail", "image", 30, account_id=drama_id, pinned=True,
            pin_role="drama", deadline=time.monotonic() + 30)
        raise AssertionError("pinned failures must surface")
    except module.MuseGenerationError as exc:
        text = str(exc)
        assert "角色 drama" in text and "未切换" in text
        assert SENTINEL_DRAMA not in text and SENTINEL_OTHER not in text
    assert used == [drama_id, drama_id]
    assert other_id not in used

    used.clear()
    state["fails_left"] = 1
    state["error"] = module.MuseModelRefusalError("text only")
    try:
        module._run_generation_locked(
            "pinned refusal", "video", 30, account_id=drama_id, pinned=True,
            pin_role="drama", deadline=time.monotonic() + 30)
        raise AssertionError("refusal must propagate")
    except module.MuseModelRefusalError as exc:
        assert "未切换" in str(exc)
    assert used == [drama_id]

    drama["last_used"] = 0
    drama["use_count"] = 0
    other["last_used"] = 0
    other["use_count"] = 0
    used.clear()
    acquired.clear()
    state["fails_left"] = 1
    state["error"] = module.MuseGenerationError("rotate me")
    res, acc = module._run_generation_locked(
        "unpinned retry", "image", 30, deadline=time.monotonic() + 30)
    assert used == [drama_id, other_id], used
    assert acc == other_id
    assert acquired[-1].get("force_rotate") is True
    assert acquired[-1].get("account_id") is None
    print("PASS: unpinned retry still rotates")

    entered = threading.Event()
    orig_wait = module.ACCOUNT_POOL.cond.wait

    def wait(timeout=None):
        entered.set()
        return orig_wait(timeout)

    module.ACCOUNT_POOL.cond.wait = wait
    state["fails_left"] = 0
    try:
        prefer_other(drama, other)
        with module.ACCOUNT_POOL.cond:
            module.ACCOUNT_POOL.busy_accounts.add(drama_id)
        used.clear()
        box = {}

        def run():
            try:
                box["res"] = module._run_generation(
                    "wait for pin", "image", 5, account_id=drama_id,
                    pinned=True, pin_role="drama")
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc

        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(2), "pinned request did not wait for the busy account"
        assert used == []
        module.ACCOUNT_POOL.release(drama_id)
        worker.join(3)
        assert not worker.is_alive()
        assert "err" not in box, box.get("err")
        assert box["res"][1] == drama_id
        assert used == [drama_id]

        with module.ACCOUNT_POOL.cond:
            module.ACCOUNT_POOL.busy_accounts.add(drama_id)
        used.clear()
        try:
            module._run_generation(
                "pin timeout", "image", 1, account_id=drama_id,
                pinned=True, pin_role="drama")
            raise AssertionError("a busy pin must time out instead of rotating")
        except module.MuseGenerationError as exc:
            text = str(exc)
            assert "角色 drama" in text and "未切换" in text
            assert SENTINEL_DRAMA not in text
        assert used == []
        assert not module.GEN_LOCK.locked()
        print("PASS: pinned wait and timeout do not rotate")
    finally:
        module.ACCOUNT_POOL.cond.wait = orig_wait
        module.ACCOUNT_POOL.release(drama_id)
        module.ACCOUNT_POOL.acquire = orig_acquire

    assert module.ledger.snapshot()["entries"] == []
    assert not module.GEN_LOCK.locked()
    assert drama_id not in module.ACCOUNT_POOL.busy_accounts
    assert other_id not in module.ACCOUNT_POOL.busy_accounts


def main():
    with tempfile.TemporaryDirectory(prefix="muse-media-role-") as home:
        module = load(home)
        drama = module.store.add_account(
            {"hatch_sess": SENTINEL_DRAMA, "hatch_vml": SENTINEL_DRAMA},
            label="drama",
        )
        other = module.store.add_account(
            {"hatch_sess": SENTINEL_OTHER, "hatch_vml": SENTINEL_OTHER},
            label="other",
        )
        assert module.GEN_LOCK.limit == module.CFG.max_concurrency
        used, state = asyncio.run(http_cases(module, drama, other))
        retry_and_wait(module, drama, other, used, state)
    print("ALL MEDIA ROLE PIN TESTS PASSED")


if __name__ == "__main__":
    main()
