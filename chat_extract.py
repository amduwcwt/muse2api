"""从 muse.ai 页面提取助手原文，供对话接口逐字节交回下游。

muse.ai 的对话走加密 Noise WebSocket（二进制帧不是 markdown）。
页面解密之后才会 `JSON.parse` 出 `delta.text_append` / `message_done`
或历史消息。所以网络层抓的是解密后的 JSON，而不是密文帧。

优先级：
1. network — 上述事件拼出的原始 markdown
2. dom — 本轮新增的助手气泡按顺序用换行拼起来；代码块读 `<pre><code>` 的
   textContent（不是 innerText），并还原围栏
3. innerText — 旧路径：最后一个非空助手气泡

这里不打印正文、cookie、token 或 WebSocket 地址。
"""
from __future__ import annotations

# 页面里执行。重复注入时直接返回，避免 JSON.parse 被包两层。
INSTALL_JS = r"""
(function (global) {
  if (global.__m2aExtract && global.__m2aExtract.v === 1) return "ok";
  var origParse = global.__m2aOrigParse || JSON.parse;
  var SECRET_KEY = /^(cookies?|cookie_header|authorization|access_token|refresh_token|password|passwd|secret|api_key|token|hatch_sess|hatch_gw|hatch_vml|set-cookie|proxy|proxy_password)$/i;
  var TEXT_CAP = 4000000;
  var msgs = {};
  var order = [];
  var seen = {};
  var seenBodies = {};
  var transcriptParts = [];
  var seenSeq = {};
  var generation = 0;
  var anonSeq = 0;
  var currentId = "turn-0";

  function join(parts) {
    var out = [];
    for (var i = 0; i < parts.length; i++) {
      if (parts[i] && String(parts[i]).trim()) out.push(parts[i]);
    }
    return out.join("\n");
  }
  function filterNew(parts) {
    var out = [];
    for (var i = 0; i < parts.length; i++) {
      if (parts[i] && !seenBodies[parts[i]]) out.push(parts[i]);
    }
    return out;
  }
  function once(key) {
    if (key == null || key === "") return true;
    var id = String(key);
    if (seenSeq[id]) return false;
    seenSeq[id] = 1;
    return true;
  }
  function ensure(id) {
    var msg = msgs[id];
    if (!msg) {
      msg = {id: id, text: "", final: false, lastChunk: ""};
      msgs[id] = msg;
      if (!seen[id]) order.push(id);
    }
    return msg;
  }
  function onAppend(id, chunk, seq) {
    if (!chunk) return;
    if (seq != null && !once("a:" + (id || "") + ":" + seq + ":" + chunk.length)) return;
    if (!id) id = currentId;
    if (seen[id]) return;
    var msg = ensure(id);
    if (msg.final) return;
    if (msg.text.length >= TEXT_CAP) return;
    if (seq == null && chunk.length > 80 && msg.lastChunk === chunk) return;
    msg.text += chunk;
    msg.lastChunk = chunk;
  }
  function onFinal(id, text, seq) {
    if (!text) return;
    if (seq != null && !once("f:" + (id || "") + ":" + seq)) return;
    if (!id) {
      id = currentId;
      var existing = msgs[id];
      if (existing && existing.final && existing.text && text.indexOf(existing.text) !== 0) {
        id = "turn-" + generation + "-" + (++anonSeq);
        currentId = id;
      }
    }
    if (seen[id]) return;
    if (seenBodies[text]) {
      seen[id] = 1;
      return;
    }
    var msg = ensure(id);
    if (msg.text.length >= TEXT_CAP && text.length > TEXT_CAP) return;
    msg.text = text.length > TEXT_CAP ? text.slice(0, TEXT_CAP) : text;
    msg.final = true;
  }
  function assistantBody(obj) {
    if (!obj || typeof obj !== "object") return "";
    if (typeof obj.display_text === "string" && obj.display_text) return obj.display_text;
    if (typeof obj.text === "string" && obj.text && obj.role) return obj.text;
    var content = obj.content;
    if (typeof content === "string") return content;
    if (Object.prototype.toString.call(content) === "[object Array]") {
      var out = [];
      for (var i = 0; i < content.length; i++) {
        var part = content[i];
        if (typeof part === "string") out.push(part);
        else if (part && typeof part.text === "string") {
          var kind = part.type;
          if (kind == null || kind === "text" || kind === "output_text") out.push(part.text);
        }
      }
      return out.join("\n");
    }
    if (typeof obj.transcript === "string") return obj.transcript;
    return "";
  }
  function isRole(value, names) {
    var role = String(value || "");
    for (var i = 0; i < names.length; i++) if (role === names[i]) return true;
    return false;
  }
  function noteTranscript(list) {
    if (Object.prototype.toString.call(list) !== "[object Array]" || !list.length) return false;
    var roles = 0;
    var lastUser = -1;
    for (var i = 0; i < list.length; i++) {
      var item = list[i];
      if (!item || typeof item !== "object" || !item.role) continue;
      roles++;
      if (isRole(item.role, ["user", "human"])) lastUser = i;
    }
    if (!roles || lastUser < 0) return false;
    var parts = [];
    for (var j = lastUser + 1; j < list.length; j++) {
      var row = list[j];
      if (!row || !isRole(row.role, ["assistant", "agent", "model"])) continue;
      var body = assistantBody(row);
      if (body) parts.push(body);
    }
    var fresh = filterNew(parts);
    if (fresh.length) transcriptParts = fresh;
    return true;
  }
  function interesting(text) {
    return text.indexOf("\"role\"") !== -1 || text.indexOf("\"event\"") !== -1
      || text.indexOf("text_append") !== -1 || text.indexOf("display_text") !== -1
      || text.indexOf("\"messages\"") !== -1;
  }
  function walk(obj, depth) {
    if (obj == null || depth > 8) return;
    if (typeof obj === "string") {
      if (obj.length < 2 || obj.length > TEXT_CAP) return;
      if ((obj.charAt(0) !== "{" && obj.charAt(0) !== "[") || !interesting(obj)) return;
      try { walk(origParse(obj), depth + 1); } catch (err) {}
      return;
    }
    var isArray = Object.prototype.toString.call(obj) === "[object Array]";
    if (isArray) {
      if (noteTranscript(obj)) return;
      var n = obj.length < 400 ? obj.length : 400;
      for (var i = 0; i < n; i++) walk(obj[i], depth + 1);
      return;
    }
    if (typeof obj !== "object") return;
    var event = typeof obj.event === "string" ? obj.event : "";
    var payload = obj.payload && typeof obj.payload === "object" ? obj.payload : null;
    if (event === "delta.text_append" || event === "text_append") {
      var src = payload || obj;
      var chunk = typeof src.text === "string" ? src.text : "";
      if (chunk) onAppend(String(src.message_id || src.id || obj.message_id || ""), chunk, src.seq != null ? src.seq : obj.seq);
      return;
    }
    if (event === "delta.message_done" || event === "message.assistant" || event === "message_done") {
      var src2 = payload || obj;
      if (src2.transcript && typeof src2.transcript === "object" && src2.transcript.messages) {
        noteTranscript(src2.transcript.messages);
      }
      var finalText = "";
      if (typeof src2.display_text === "string" && src2.display_text) finalText = src2.display_text;
      else if (typeof src2.content === "string") finalText = src2.content;
      else if (typeof src2.transcript === "string") finalText = src2.transcript;
      else finalText = assistantBody(src2);
      if (finalText) onFinal(String(src2.message_id || src2.id || obj.message_id || ""), finalText, src2.seq != null ? src2.seq : obj.seq);
      return;
    }
    if (obj.messages && noteTranscript(obj.messages)) return;
    if (isRole(obj.role, ["assistant", "agent", "model"]) && !event) {
      var body = assistantBody(obj);
      if (body) {
        onFinal(String(obj.message_id || obj.id || ""), body, obj.seq);
        return;
      }
    }
    var nested = ["payload", "message", "messages", "events", "data", "body", "result", "choices", "delta", "transcript"];
    for (var k = 0; k < nested.length; k++) {
      var key = nested[k];
      if (SECRET_KEY.test(key) || obj[key] == null) continue;
      walk(obj[key], depth + 1);
    }
  }
  function ingestText(text) {
    if (typeof text !== "string") return;
    var trimmed = text.trim();
    if (!trimmed || trimmed.length > TEXT_CAP) return;
    if (trimmed.indexOf("\n") !== -1 && trimmed.charAt(0) !== "[") {
      var lines = trimmed.split("\n");
      var any = false;
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (line.indexOf("data:") === 0) line = line.slice(5).trim();
        if (!line || line === "[DONE]" || line.charAt(0) !== "{") continue;
        try { walk(origParse(line), 0); any = true; } catch (err) {}
      }
      if (any) return;
    }
    if (trimmed.charAt(0) === "{" || trimmed.charAt(0) === "[") {
      try { walk(origParse(trimmed), 0); } catch (err) {}
    }
  }
  function streamParts() {
    var parts = [];
    for (var i = 0; i < order.length; i++) {
      var msg = msgs[order[i]];
      if (!msg || seen[msg.id] || !msg.text) continue;
      parts.push(msg.text);
    }
    return parts;
  }
  function turnText() {
    var stream = join(filterNew(streamParts()));
    var script = join(filterNew(transcriptParts));
    if (stream && script) {
      if (script.indexOf(stream) !== -1 && script.length >= stream.length) return script;
      if (stream.indexOf(script) !== -1) return stream;
      return script.length >= stream.length ? script : stream;
    }
    return stream || script || "";
  }
  function markTurn() {
    var pieces = streamParts().concat(transcriptParts);
    var snap = {};
    for (var i = 0; i < pieces.length; i++) {
      if (pieces[i]) snap[pieces[i]] = 1;
    }
    seenBodies = snap;
    for (var id in msgs) {
      seen[id] = 1;
      msgs[id].text = "";
    }
    order = [];
    transcriptParts = [];
    generation += 1;
    anonSeq += 1;
    currentId = "turn-" + generation;
  }
  function classText(el) {
    if (!el) return "";
    var value = el.className;
    if (value && typeof value === "object" && value.baseVal) return String(value.baseVal);
    if (typeof value === "string" && value) return value;
    return el.getAttribute ? String(el.getAttribute("class") || "") : "";
  }
  function languageOf(el) {
    if (!el || !el.getAttribute) return "";
    var lang = el.getAttribute("data-language") || el.getAttribute("data-lang") || "";
    if (!lang) {
      var matched = classText(el).match(/(?:language|lang)-([A-Za-z0-9_+#.-]+)/);
      lang = matched ? matched[1] : "";
    }
    return String(lang).replace(/[^A-Za-z0-9_+#.-]/g, "");
  }
  function literalText(el) {
    if (!el) return "";
    var node = el;
    if (el.cloneNode) {
      node = el.cloneNode(true);
      if (node.querySelectorAll) {
        var junk = node.querySelectorAll("button,svg,style,script");
        for (var i = junk.length - 1; i >= 0; i--) {
          if (junk[i].parentNode) junk[i].parentNode.removeChild(junk[i]);
        }
      }
    }
    return node.textContent == null ? "" : String(node.textContent);
  }
  var BLOCK = {p:1, div:1, pre:1, ul:1, ol:1, li:1, h1:1, h2:1, h3:1, h4:1, h5:1, h6:1, blockquote:1, section:1, article:1, table:1, tr:1};
  var SKIP = {button:1, svg:1, style:1, script:1, noscript:1, textarea:1, input:1, select:1};
  function renderNode(node) {
    if (!node) return "";
    if (node.nodeType === 3) return node.nodeValue == null ? "" : String(node.nodeValue);
    if (node.nodeType !== 1) return "";
    var tag = String(node.tagName || "").toLowerCase();
    if (SKIP[tag]) return "";
    if (tag === "br") return "\n";
    if (tag === "pre") {
      var code = node.querySelector ? (node.querySelector("code") || node) : node;
      var lang = languageOf(code) || languageOf(node);
      var body = literalText(code);
      if (body.slice(-1) === "\n") return "```" + lang + "\n" + body + "```";
      return "```" + lang + "\n" + body + "\n```";
    }
    if (tag === "code") return "`" + literalText(node) + "`";
    var inner = renderChildren(node);
    if (tag === "strong" || tag === "b") return "**" + inner + "**";
    if (tag === "em" || tag === "i") return "*" + inner + "*";
    return inner;
  }
  function renderChildren(node) {
    var out = "";
    var children = node.childNodes || [];
    for (var i = 0; i < children.length; i++) {
      var child = children[i];
      var piece = renderNode(child);
      if (!piece) continue;
      var tag = child.nodeType === 1 ? String(child.tagName || "").toLowerCase() : "";
      var block = tag && BLOCK[tag];
      if (block && out && out.slice(-1) !== "\n") out += "\n";
      out += piece;
      if (block && out.slice(-1) !== "\n") out += "\n";
    }
    return out;
  }
  function agentBubbles(root) {
    var scope = root || global.document;
    if (!scope || !scope.querySelectorAll) return [];
    var nodes = scope.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]');
    var out = [];
    for (var i = 0; i < nodes.length; i++) {
      if (/hatch-agent-bubble-bg/.test(classText(nodes[i]))) out.push(nodes[i]);
    }
    return out;
  }
  function bubbleText(nodes) {
    var parts = [];
    for (var i = 0; i < nodes.length; i++) {
      var text = renderNode(nodes[i]);
      if (text && String(text).trim()) parts.push(String(text).replace(/^\n+|\n+$/g, ""));
    }
    return parts.join("\n");
  }
  function choose(network, dom, legacy) {
    if (typeof network === "string" && network.trim()) return {txt: network, extract: "network"};
    if (typeof dom === "string" && dom.trim()) return {txt: dom, extract: "dom"};
    return {txt: typeof legacy === "string" ? legacy : "", extract: "innerText"};
  }
  function scrollChat() {
    try {
      var doc = global.document;
      if (!doc || !doc.querySelectorAll || !global.getComputedStyle) return;
      var els = [].slice.call(doc.querySelectorAll("*")).filter(function (e) {
        var s = global.getComputedStyle(e);
        return s && (s.overflowY === "auto" || s.overflowY === "scroll") && e.scrollHeight > e.clientHeight + 100;
      });
      els.sort(function (a, b) { return b.scrollHeight - a.scrollHeight; });
      if (els[0]) els[0].scrollTop = els[0].scrollHeight;
    } catch (err) {}
  }
  function agentRoot(root) {
    var scope = root || global.document;
    if (scope && scope.querySelector) {
      var narrower = scope.querySelector('main,[class*="chat-scroll"],[class*="hatch-chat-scroll"]');
      if (narrower) return narrower;
    }
    return scope;
  }
  function poll(baseline) {
    scrollChat();
    var bubbles = agentBubbles(agentRoot(global.document));
    var nonEmpty = [];
    for (var i = 0; i < bubbles.length; i++) {
      var visible = bubbles[i].innerText == null ? "" : String(bubbles[i].innerText);
      if (visible.trim()) nonEmpty.push(bubbles[i]);
    }
    if (baseline == null) baseline = nonEmpty.length;
    baseline = baseline | 0;
    if (baseline < 0) baseline = 0;
    if (baseline > nonEmpty.length) baseline = nonEmpty.length;
    var legacy = nonEmpty.length ? String(nonEmpty[nonEmpty.length - 1].innerText || "").trim() : "";
    var dom = "";
    var network = "";
    try { dom = bubbleText(nonEmpty.slice(baseline)); } catch (err) { dom = ""; }
    try { network = turnText() || ""; } catch (err2) { network = ""; }
    var chosen = choose(network, dom, legacy);
    var stop = false;
    try {
      var doc = global.document;
      stop = !!(doc && (doc.querySelector('[data-testid="hatch-composer-stop-button"]')
        || doc.querySelector('button[aria-label*="Stop" i]')
        || doc.querySelector('button[aria-label*="停止"]')));
    } catch (err3) { stop = false; }
    return JSON.stringify({
      cnt: nonEmpty.length,
      total: bubbles.length,
      txt: chosen.txt,
      stop: stop,
      extract: chosen.extract
    });
  }
  if (!global.__m2aParseHooked) {
    global.__m2aOrigParse = origParse;
    JSON.parse = function (text, reviver) {
      var value = reviver === undefined ? origParse(text) : origParse(text, reviver);
      try { walk(value, 0); } catch (err) {}
      return value;
    };
    global.__m2aParseHooked = true;
  }
  global.__m2aExtract = {
    v: 1,
    markTurn: markTurn,
    turnText: turnText,
    ingestText: ingestText,
    poll: poll,
    choose: choose,
    extractDom: function (root, baseline) {
      var bubbles = agentBubbles(agentRoot(root || global.document));
      var nonEmpty = [];
      for (var i = 0; i < bubbles.length; i++) {
        var visible = bubbles[i].innerText == null ? (bubbles[i].textContent || "") : String(bubbles[i].innerText);
        if (String(visible).trim() || String(bubbles[i].textContent || "").trim()) nonEmpty.push(bubbles[i]);
      }
      var start = baseline == null ? 0 : (baseline | 0);
      if (start < 0) start = 0;
      return bubbleText(nonEmpty.slice(start));
    }
  };
  return "ok";
})(typeof globalThis !== "undefined" ? globalThis : window);
"""


_LEGACY_POLL_JS = r"""(function(){
var els=[...document.querySelectorAll('*')].filter(function(e){
var s=getComputedStyle(e);
return (s.overflowY==='auto'||s.overflowY==='scroll')&&e.scrollHeight>e.clientHeight+100;});
els.sort(function(a,b){return b.scrollHeight-a.scrollHeight;});
if(els[0])els[0].scrollTop=els[0].scrollHeight;
var scope=document.querySelector('main,[class*="chat-scroll"],[class*="hatch-chat-scroll"]')||document.body;
var bs=[].slice.call(scope.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]'))
.filter(function(b){return /hatch-agent-bubble-bg/.test(b.className||'');});
var nonEmpty=bs.filter(function(b){return ((b.innerText||'').trim().length)>0;});
var txt=nonEmpty.length?(nonEmpty[nonEmpty.length-1].innerText||'').trim():'';
var stop=!!(document.querySelector('[data-testid="hatch-composer-stop-button"]')
||document.querySelector('button[aria-label*="Stop" i]')
||document.querySelector('button[aria-label*="停止"]'));
return JSON.stringify({cnt:nonEmpty.length,total:bs.length,txt:txt,stop:stop,extract:'innerText'});})()"""


def poll_expression(baseline: int | None) -> str:
    """页面轮询表达式。库没装上时走原来的最后一个气泡 innerText。"""
    if baseline is None:
        arg = "null"
    else:
        arg = str(int(baseline))
    return (
        "(function(b){if(window.__m2aExtract&&window.__m2aExtract.poll)"
        "return window.__m2aExtract.poll(b);return "
        + _LEGACY_POLL_JS
        + ";})("
        + arg
        + ")"
    )
