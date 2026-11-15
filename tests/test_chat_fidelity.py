"""回复保真和旁聊增量提示词。不启动浏览器，不访问 muse.ai。

运行：python tests/test_chat_fidelity.py
需要本机 node 来执行页面里的提取脚本。
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import engine as engine_mod  # noqa: E402
from chat_extract import INSTALL_JS, poll_expression  # noqa: E402
from config import max_prompt_chars  # noqa: E402
from sidechat import SidechatLedger  # noqa: E402

CODE = "const t = `hi ${name}`;\nreturn Promise<void>;\n/** keep stars */\nline\\nnext\nconst kind: typeof X = 1;"
PAYLOAD = {
    "name": "connector",
    "arguments": {"pattern": "\\s+\\S+", "code": CODE},
}
RAW_JSON = json.dumps(PAYLOAD, ensure_ascii=False, separators=(",", ":"))
MARKDOWN = "```CONNECTOR_CALL\n" + RAW_JSON + "\n```"


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def test_extract_preserves_connector_json():
    html = (
        '<div class="hatch-chat-groupable-bubble hatch-agent-bubble-bg" '
        'data-inner="CORRUPTED_INNER_TEXT"><p>intro</p><pre><code '
        'class="language-CONNECTOR_CALL"><button>Copy</button>'
        + _html_escape(RAW_JSON)
        + "</code></pre></div>"
        '<div class="hatch-chat-groupable-bubble hatch-agent-bubble-bg">'
        "<p>SECOND_BUBBLE</p></div>"
    )
    harness = r"""
const fs = require("fs");
const spec = JSON.parse(fs.readFileSync(0, "utf8"));
const api = globalThis.__m2aExtract;
function fail(msg) { console.error(msg); process.exit(1); }
function decode(text) {
  return text.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/&quot;/g, '"');
}
function make(tag, attrs, children) {
  const node = {nodeType: 1, tag: tag, attrs: attrs, children: children, parent: null, parentNode: null};
  node.tagName = tag.toUpperCase();
  node.className = attrs.class || "";
  node.getAttribute = (name) => (Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null);
  node.querySelectorAll = (sel) => queryAll(node, sel);
  node.querySelector = (sel) => node.querySelectorAll(sel)[0] || null;
  Object.defineProperty(node, "childNodes", {get() { return node.children; }});
  Object.defineProperty(node, "textContent", {get() { return node.children.map(childText).join(""); }});
  Object.defineProperty(node, "innerText", {get() {
    return Object.prototype.hasOwnProperty.call(attrs, "data-inner") ? attrs["data-inner"] : node.textContent;
  }});
  node.removeChild = (child) => {
    const index = node.children.indexOf(child);
    if (index >= 0) node.children.splice(index, 1);
  };
  node.cloneNode = (deep) => {
    const copy = make(tag, Object.assign({}, attrs), []);
    if (deep) {
      for (const child of node.children) {
        const cloned = child.nodeType === 3
          ? {nodeType: 3, nodeValue: child.nodeValue, parent: copy, parentNode: copy}
          : child.cloneNode(true);
        cloned.parent = copy;
        cloned.parentNode = copy;
        copy.children.push(cloned);
      }
    }
    return copy;
  };
  return node;
}
function childText(node) {
  if (!node) return "";
  if (node.nodeType === 3) return node.nodeValue || "";
  return node.textContent || "";
}
function match(node, sel) {
  if (sel === "code") return node.tag === "code";
  if (sel === "button,svg,style,script") return ["button", "svg", "style", "script"].indexOf(node.tag) !== -1;
  if (sel.indexOf("hatch-chat-groupable-bubble") !== -1) {
    return node.tag === "div" && String(node.className).indexOf("hatch-chat-groupable-bubble") !== -1;
  }
  return false;
}
function queryAll(root, sel) {
  const out = [];
  function walk(node) {
    if (!node || node.nodeType !== 1) return;
    if (match(node, sel)) out.push(node);
    for (const child of node.children) walk(child);
  }
  for (const child of root.children) walk(child);
  return out;
}
function parseHtml(html) {
  const root = make("document", {}, []);
  const stack = [root];
  const re = /<\/([A-Za-z0-9]+)>|<([A-Za-z0-9]+)([^>]*)>|([^<]+)/g;
  let found;
  while ((found = re.exec(html))) {
    if (found[1]) {
      const name = found[1].toLowerCase();
      while (stack.length > 1 && stack[stack.length - 1].tag !== name) stack.pop();
      if (stack.length > 1) stack.pop();
      continue;
    }
    if (found[2]) {
      const tag = found[2].toLowerCase();
      const attrs = {};
      const attrRe = /([A-Za-z0-9:-]+)(?:\s*=\s*"([^"]*)")?/g;
      let attr;
      while ((attr = attrRe.exec(found[3] || ""))) attrs[attr[1]] = attr[2] || "";
      const node = make(tag, attrs, []);
      const parent = stack[stack.length - 1];
      parent.children.push(node);
      node.parent = parent;
      node.parentNode = parent;
      stack.push(node);
      continue;
    }
    if (found[4]) {
      const text = decode(found[4]);
      const parent = stack[stack.length - 1];
      if (text.trim() || parent.tag === "code" || parent.tag === "pre") {
        parent.children.push({nodeType: 3, nodeValue: text, parent: parent, parentNode: parent});
      }
    }
  }
  return root;
}
if (!api) fail("extract api missing");
api.markTurn();
api.ingestText([
  JSON.stringify({event: "delta.text_append", seq: 9, payload: {message_id: "z", text: "AAA"}}),
  JSON.stringify({event: "delta.text_append", seq: 10, payload: {message_id: "z", text: "BBB"}})
].join("\n"));
if (api.turnText() !== "AAABBB") fail("ndjson frames were not concatenated: " + api.turnText());
api.markTurn();
const mid = Math.max(1, Math.floor(spec.markdown.length / 2));
const secret = "sekret-token-value";
JSON.parse(JSON.stringify({
  access_token: secret,
  event: "delta.text_append",
  seq: 1,
  payload: {message_id: "m1", text: spec.markdown.slice(0, mid)}
}));
JSON.parse(JSON.stringify({
  event: "delta.text_append",
  seq: 2,
  payload: {message_id: "m1", text: spec.markdown.slice(mid)}
}));
if (api.turnText() !== spec.markdown) fail("chunks diverged: " + api.turnText().slice(0, 80));
JSON.parse(JSON.stringify({
  event: "delta.text_append",
  seq: 2,
  payload: {message_id: "m1", text: spec.markdown.slice(mid)}
}));
if (api.turnText() !== spec.markdown) fail("duplicate seq changed the text");
if (api.turnText().indexOf(secret) !== -1) fail("secret leaked into reply text");
JSON.parse(JSON.stringify({
  event: "delta.message_done",
  seq: 3,
  payload: {message_id: "m1", display_text: spec.markdown}
}));
if (api.turnText() !== spec.markdown) fail("message_done changed bytes");
JSON.parse(JSON.stringify({
  event: "message.assistant",
  seq: 4,
  payload: {message_id: "m2", role: "assistant", display_text: "SECOND_NET"}
}));
if (api.turnText() !== spec.markdown + "\nSECOND_NET") fail("bubbles not joined in order");
api.markTurn();
JSON.parse(JSON.stringify({
  event: "delta.text_append", seq: 1,
  payload: {message_id: "bad", text: "WRONG_DELTA"}
}));
JSON.parse(JSON.stringify({
  event: "delta.message_done", seq: 2,
  payload: {message_id: "bad", display_text: "RIGHT_FINAL"}
}));
if (api.turnText() !== "RIGHT_FINAL") fail("final text did not replace the delta: " + api.turnText());
api.markTurn();
api.ingestText(JSON.stringify({messages: [
  {role: "user", content: "old question"},
  {role: "assistant", content: "OLD_REPLY_MARKER"},
  {role: "user", content: "new question"},
  {role: "assistant", id: "a1", content: "FRESH_A"},
  {role: "assistant", id: "a2", content: "FRESH_B"}
]}));
if (api.turnText() !== "FRESH_A\nFRESH_B") fail("transcript turn: " + api.turnText());
if (api.turnText().indexOf("OLD_REPLY_MARKER") !== -1) fail("old reply included");
api.markTurn();
const doc = parseHtml(spec.html);
globalThis.document = doc;
const dom = api.extractDom(doc, 0);
const fence = /```CONNECTOR_CALL\n([\s\S]*?)\n```/.exec(dom);
if (!fence) fail("dom fence missing: " + dom.slice(0, 120));
if (fence[1] !== spec.raw) fail("dom code bytes diverged");
if (dom.indexOf("SECOND_BUBBLE") === -1) fail("second bubble dropped");
if (dom.indexOf("CONNECTOR_CALL") > dom.indexOf("SECOND_BUBBLE")) fail("bubble order reversed");
if (dom.indexOf("CORRUPTED_INNER_TEXT") !== -1) fail("fell back to innerText");
if (dom.indexOf("Copy") !== -1) fail("copy button leaked into code");
const tail = api.extractDom(doc, 1);
if (tail.indexOf(spec.raw) !== -1) fail("baseline did not skip the earlier bubble");
if (tail.indexOf("SECOND_BUBBLE") === -1) fail("baseline slice dropped the new bubble");
const polled = JSON.parse(api.poll(0));
if (polled.extract !== "dom") fail("expected dom extract, got " + polled.extract);
if (polled.txt.indexOf(spec.raw) === -1) fail("poll dropped the code block");
const legacy = api.choose("", "", "LEGACY_ONLY");
if (legacy.extract !== "innerText" || legacy.txt !== "LEGACY_ONLY") fail("legacy choose broken");
const network = api.choose(spec.markdown, dom, "LEGACY_ONLY");
if (network.extract !== "network" || network.txt !== spec.markdown) fail("network priority broken");
console.log(JSON.stringify({ok: true, fence: fence[1], dom: dom}));
"""
    path = Path(tempfile.mkstemp(suffix=".js")[1])
    try:
        path.write_text(INSTALL_JS + "\n" + harness, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(path)],
            input=json.dumps({"raw": RAW_JSON, "markdown": MARKDOWN, "html": html}),
            text=True, capture_output=True, check=False,
        )
    finally:
        path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    result = json.loads(proc.stdout)
    assert result["fence"] == RAW_JSON
    parsed = json.loads(result["fence"])
    assert parsed == PAYLOAD
    assert parsed["arguments"]["pattern"] == "\\s+\\S+"
    assert parsed["arguments"]["code"] == CODE
    assert "Promise<void>" in parsed["arguments"]["code"]
    assert "/** keep stars */" in parsed["arguments"]["code"]
    assert "line\\nnext" in parsed["arguments"]["code"]
    assert "console.log" not in INSTALL_JS
    assert "innerText" in poll_expression(None)
    assert "__m2aExtract" in poll_expression(1)
    print("PASS: connector json survives network and dom extract")


def _accounts():
    return [{
        "id": "acc-ops", "label": "ops", "enabled": True, "ok": True,
        "has_cookies": True, "last_used": 1, "use_count": 1,
    }]


def _listen():
    logger = logging.getLogger("muse2api")
    logger.setLevel(logging.INFO)
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Handler()
    logger.addHandler(handler)
    return logger, handler, records


def test_prompt_cursor_and_truncation():
    logger, handler, records = _listen()
    path = tempfile.mktemp(suffix=".json")
    try:
        os.environ["MUSE2API_MAX_PROMPT_CHARS"] = "nope"
        assert max_prompt_chars() == 200000
        os.environ["MUSE2API_MAX_PROMPT_CHARS"] = "0"
        assert max_prompt_chars() == 200000
        os.environ["MUSE2API_MAX_PROMPT_CHARS"] = "150000"
        assert max_prompt_chars() == 150000

        old = {
            "version": 1,
            "entries": [{
                "id": "sc_old",
                "ledger_key": "role:ops",
                "role": "ops",
                "status": "active",
                "muse_account_id": "acc-ops",
                "muse_account_label": "ops",
                "session_id": "example-session-not-real",
                "use_count": 2,
            }],
            "role_account_map": {"ops": "acc-ops"},
        }
        Path(path).write_text(json.dumps(old), encoding="utf-8")
        ledger = SidechatLedger(path)
        plan = ledger.plan("ops", None, _accounts())
        assert plan["reason"] == "resume"
        assert plan["session_id"] == "example-session-not-real"
        assert plan["sent_fp"] is None
        assert plan["sent_count"] is None

        system = app.ChatMessage(role="system", content="SYS_A")
        user_a = app.ChatMessage(role="user", content="USER_A")
        assist = app.ChatMessage(role="assistant", content="ASSIST_A")
        first_messages = [system, user_a, assist]
        first = app.compose_chat_prompt(first_messages, plan, limit=200000)
        assert first["reason"] == "legacy"
        assert first["mode"] == "full"
        assert first["sidechat"]["session_id"] == "example-session-not-real"
        assert "USER_A" in first["prompt"] and "SYS_A" in first["prompt"]
        assert any("reason=legacy" in line and "prompt_chars=" in line for line in records)
        cursor = first["sidechat"]["prompt_cursor"]
        saved = ledger.record_result(
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-ops", session_id="example-session-not-real", nav="resumed",
            sent_count=cursor["sent_count"], sent_fp=cursor["sent_fp"],
            sent_system_fp=cursor["sent_system_fp"],
        )
        kept = ledger.record_result(
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-ops", session_id="example-session-not-real", nav="reused",
        )
        assert kept["sent_fp"] == saved["sent_fp"]
        assert kept["sent_count"] == saved["sent_count"]
        assert SidechatLedger(path).plan("ops", None, _accounts())["sent_fp"] == saved["sent_fp"]

        user_b = app.ChatMessage(role="user", content="USER_B")
        plan2 = ledger.plan("ops", None, _accounts())
        records.clear()
        second = app.compose_chat_prompt(first_messages + [user_b], plan2, limit=200000)
        assert second["reason"] == "resume"
        assert second["mode"] == "incremental"
        assert second["prompt"] == "USER_B"
        assert second["sidechat"]["session_id"] == "example-session-not-real"
        assert "USER_A" not in second["prompt"] and "ASSIST_A" not in second["prompt"]

        changed = [app.ChatMessage(role="system", content="SYS_B"), user_a, assist, user_b]
        with_system = app.compose_chat_prompt(changed, plan2, limit=200000)
        assert with_system["mode"] == "incremental"
        assert "SYS_B" in with_system["prompt"]
        assert "USER_B" in with_system["prompt"]
        assert "USER_A" not in with_system["prompt"]
        assert with_system["sidechat"]["session_id"] == "example-session-not-real"

        records.clear()
        rewritten = [
            system,
            app.ChatMessage(role="user", content="REWRITE_MARKER_9f3"),
            user_b,
        ]
        mismatch = app.compose_chat_prompt(rewritten, plan2, limit=200000)
        assert mismatch["reason"] == "mismatch"
        assert mismatch["mode"] == "full_new"
        assert mismatch["sidechat"]["session_id"] is None
        assert mismatch["sidechat"]["retire_session_id"] == "example-session-not-real"
        assert "REWRITE_MARKER_9f3" in mismatch["prompt"]
        assert "USER_B" in mismatch["prompt"]
        logged = "\n".join(records)
        assert "prefix mismatch" in logged
        assert "REWRITE_MARKER_9f3" not in logged
        assert "action=new_thread" in logged

        drop_one = app.ChatMessage(role="user", content="DROP_ONE_" + ("x" * 3000))
        drop_two = app.ChatMessage(role="assistant", content="DROP_TWO_" + ("y" * 3000))
        recent = app.ChatMessage(role="user", content="RECENT_MARK")
        marker = app.ChatMessage(role="user", content="[省略 2 条早期消息]")
        kept_messages = [system, user_a, marker, recent]
        limit = len(app.build_chat_prompt(kept_messages))
        full = [system, user_a, drop_one, drop_two, recent]
        assert len(app.build_chat_prompt(full)) > limit
        records.clear()
        truncated = app.compose_chat_prompt(full, None, limit=limit)
        assert truncated["omitted"] == 2
        assert truncated["reason"] == "anonymous"
        assert "[省略 2 条早期消息]" in truncated["prompt"]
        assert "SYS_A" in truncated["prompt"]
        assert "USER_A" in truncated["prompt"]
        assert "RECENT_MARK" in truncated["prompt"]
        assert "DROP_ONE_" not in truncated["prompt"]
        assert "DROP_TWO_" not in truncated["prompt"]
        warning = "\n".join(records)
        assert "chat prompt truncated" in warning
        assert "prompt_chars=" in warning
        assert "DROP_ONE_" not in warning
        assert '"MUSE2API_CONCURRENCY"' in (ROOT / "config.py").read_text()
        disk = Path(path).read_text(encoding="utf-8")
        assert "hatch_sess" not in disk
        assert "cookie" not in disk.lower()
    finally:
        logger.removeHandler(handler)
        os.environ.pop("MUSE2API_MAX_PROMPT_CHARS", None)
        if os.path.exists(path):
            os.remove(path)
    print("PASS: incremental prompt, mismatch, truncation, legacy cursor")


def _history_parts():
    system = app.ChatMessage(role="system", content="SYS_KEEP")
    first = app.ChatMessage(role="user", content="FIRST_KEEP")
    drop_one = app.ChatMessage(role="user", content="DROP_ONE_" + ("x" * 3000))
    drop_two = app.ChatMessage(role="assistant", content="DROP_TWO_" + ("y" * 3000))
    recent = app.ChatMessage(role="user", content="RECENT_KEEP")
    return system, first, drop_one, drop_two, recent


def _fit_limit(kept: list) -> int:
    return len(app.build_chat_prompt(kept))


def test_failover_new_thread_uses_full_history():
    logger, handler, records = _listen()
    path = tempfile.mktemp(suffix=".json")
    try:
        system, first, drop_one, drop_two, recent = _history_parts()
        history = [system, first, drop_one, drop_two, recent]
        newest = app.ChatMessage(role="user", content="ONLY_NEW_TURN")
        marker = app.ChatMessage(role="user", content="[省略 2 条早期消息]")
        limit = _fit_limit([system, first, marker, recent, newest])
        ledger = SidechatLedger(path)
        ledger.record_result(
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-ops", session_id="example-session-not-real", nav="fresh",
        )
        seeded = app.compose_chat_prompt(history, {
            "strategy": "bc", "ledger_key": "role:ops", "role": "ops",
            "session_id": "example-session-not-real", "sent_fp": None,
        }, limit=limit)
        assert seeded["reason"] == "legacy"
        ledger.record_result(
            ledger_key="role:ops", role="ops", project=None,
            account_id="acc-ops", session_id="example-session-not-real", nav="resumed",
            sent_count=seeded["sidechat"]["prompt_cursor"]["sent_count"],
            sent_fp=seeded["sidechat"]["prompt_cursor"]["sent_fp"],
            sent_system_fp=seeded["sidechat"]["prompt_cursor"]["sent_system_fp"],
        )
        plan = ledger.plan("ops", None, _accounts())
        records.clear()
        delivery = app.compose_chat_prompt(history + [newest], plan, limit=limit)
        assert delivery["mode"] == "incremental"
        assert delivery["prompt"] == "ONLY_NEW_TURN"
        assert "DROP_ONE_" not in delivery["prompt"]
        assert delivery["full_prompt"] != delivery["prompt"]
        assert "[省略 2 条早期消息]" in delivery["full_prompt"]
        assert "FIRST_KEEP" in delivery["full_prompt"]
        assert "RECENT_KEEP" in delivery["full_prompt"]
        assert "ONLY_NEW_TURN" in delivery["full_prompt"]
        assert "SYS_KEEP" in delivery["full_prompt"]
        assert "DROP_ONE_" not in delivery["full_prompt"]
        assert "DROP_TWO_" not in delivery["full_prompt"]
        assert delivery["sidechat"]["session_id"] == "example-session-not-real"

        choose = engine_mod.MuseEngine.prompt_for_nav
        assert choose(delivery["prompt"], delivery["full_prompt"], "resumed") == "ONLY_NEW_TURN"
        assert choose(delivery["prompt"], delivery["full_prompt"], "reused") == "ONLY_NEW_TURN"
        records.clear()
        for nav in ("new", "fresh", "stale", "rollover"):
            sent = choose(
                delivery["prompt"], delivery["full_prompt"], nav,
                omitted=delivery["full_omitted"],
                before_chars=delivery["full_before_chars"],
                limit=delivery["limit"],
            )
            assert sent == delivery["full_prompt"], nav
            assert sent != "ONLY_NEW_TURN"
        logged = "\n".join(records)
        assert "sidechat prompt failover new-thread full" in logged
        assert "prompt_chars=" in logged
        assert "nav=new" in logged
        assert "chat prompt truncated" in logged
        assert "omitted=" in logged
        assert "before_chars=" in logged
        assert "DROP_ONE_" not in logged
        assert "ONLY_NEW_TURN" not in logged
        assert "FIRST_KEEP" not in logged
        assert "sekret" not in logged
    finally:
        logger.removeHandler(handler)
        if os.path.exists(path):
            os.remove(path)
    print("PASS: failover new thread sends truncated full history")


def test_mismatch_and_legacy_full_truncation():
    logger, handler, records = _listen()
    path = tempfile.mktemp(suffix=".json")
    try:
        system, first, drop_one, drop_two, recent = _history_parts()
        history = [system, first, drop_one, drop_two, recent]
        marker = app.ChatMessage(role="user", content="[省略 2 条早期消息]")
        limit = _fit_limit([system, first, marker, recent])
        ledger = SidechatLedger(path)
        Path(path).write_text(json.dumps({
            "version": 1,
            "entries": [{
                "ledger_key": "role:ops", "role": "ops", "status": "active",
                "muse_account_id": "acc-ops", "session_id": "example-session-not-real",
                "use_count": 1,
            }],
            "role_account_map": {"ops": "acc-ops"},
        }), encoding="utf-8")
        ledger = SidechatLedger(path)
        plan = ledger.plan("ops", None, _accounts())
        assert plan["sent_fp"] is None
        assert plan["session_id"] == "example-session-not-real"
        records.clear()
        legacy = app.compose_chat_prompt(history, plan, limit=limit)
        assert legacy["reason"] == "legacy"
        assert legacy["mode"] == "full"
        assert legacy["sidechat"]["session_id"] == "example-session-not-real"
        assert legacy["prompt"] == legacy["full_prompt"]
        assert "[省略 2 条早期消息]" in legacy["prompt"]
        assert "SYS_KEEP" in legacy["prompt"] and "FIRST_KEEP" in legacy["prompt"]
        assert "RECENT_KEEP" in legacy["prompt"]
        assert "DROP_ONE_" not in legacy["prompt"] and "DROP_TWO_" not in legacy["prompt"]
        logged = "\n".join(records)
        assert "chat prompt truncated" in logged
        assert "prompt_chars=" in logged and "before_chars=" in logged
        assert "omitted=2" in logged and f"limit={limit}" in logged
        assert "reason=legacy" in logged
        assert "DROP_ONE_" not in logged and "FIRST_KEEP" not in logged
        assert ledger.plan("ops", None, _accounts())["sent_fp"] is None

        old_ledger = app.ledger
        app.ledger = ledger
        try:
            app._commit_sidechat(
                legacy["sidechat"],
                {"nav": "resumed", "session_id": "example-session-not-real"},
                "acc-ops", accept_cursor=False,
            )
            assert ledger.plan("ops", None, _accounts())["sent_fp"] is None
            app._commit_sidechat(
                legacy["sidechat"],
                {"nav": "resumed", "session_id": "example-session-not-real"},
                "acc-ops", accept_cursor=True,
            )
            assert ledger.plan("ops", None, _accounts())["sent_fp"] == legacy["sidechat"]["prompt_cursor"]["sent_fp"]
        finally:
            app.ledger = old_ledger

        records.clear()
        mismatched = app.compose_chat_prompt(
            [app.ChatMessage(role="system", content="SYS_KEEP"),
             app.ChatMessage(role="user", content="REWRITTEN_FIRST")] + history[2:],
            ledger.plan("ops", None, _accounts()),
            limit=limit,
        )
        assert mismatched["reason"] == "mismatch"
        assert mismatched["mode"] == "full_new"
        assert mismatched["sidechat"]["session_id"] is None
        assert mismatched["prompt"] == mismatched["full_prompt"]
        assert "REWRITTEN_FIRST" in mismatched["prompt"]
        assert "[省略 2 条早期消息]" in mismatched["prompt"]
        assert "DROP_ONE_" not in mismatched["prompt"]
        mismatch_log = "\n".join(records)
        assert "prefix mismatch" in mismatch_log
        assert "action=new_thread" in mismatch_log
        assert "chat prompt truncated" in mismatch_log
        assert "prompt_chars=" in mismatch_log
        assert "REWRITTEN_FIRST" not in mismatch_log
        assert "DROP_ONE_" not in mismatch_log
        assert max_prompt_chars() == 200000 or os.environ.get("MUSE2API_MAX_PROMPT_CHARS")
        os.environ.pop("MUSE2API_MAX_PROMPT_CHARS", None)
        assert max_prompt_chars() == 200000
    finally:
        logger.removeHandler(handler)
        if os.path.exists(path):
            os.remove(path)
    print("PASS: legacy truncation, yield cursor, mismatch full")


if __name__ == "__main__":
    test_extract_preserves_connector_json()
    test_prompt_cursor_and_truncation()
    test_failover_new_thread_uses_full_history()
    test_mismatch_and_legacy_full_truncation()
    print("ALL CHAT FIDELITY TESTS PASSED")
