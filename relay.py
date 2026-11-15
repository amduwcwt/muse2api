"""对话正文台账。SQLite 在 data/relay.sqlite，和旁聊 JSON 分开。

旁聊 JSON 只做 session 路由。这里按轮增量写入已经脱敏的消息，
不保存 build_chat_prompt() 拼出来的整段历史。
P2 的选卡 / 点击只留 actions 空表，本模块不写它。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid

from redact import redact_text

MSG_CAP = 32 * 1024
TURN_CAP = 64 * 1024
MARKER = "«truncated»"
_HASH_RE_LEN = 64

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def clip_text(text: str, cap: int) -> tuple[str, int, int]:
    """按 UTF-8 字节截断。返回 (存储文本, truncated, 原文字数)。"""
    full = text or ""
    char_count = len(full)
    raw = full.encode("utf-8")
    if cap < 0:
        cap = 0
    if len(raw) <= cap and cap > 0:
        return full, 0, char_count
    if cap == 0:
        return "", 1 if full else 0, char_count
    marker = "\n" + MARKER
    marker_b = marker.encode("utf-8")
    if len(marker_b) >= cap:
        marker = MARKER
        marker_b = marker.encode("utf-8")
    budget = cap - len(marker_b)
    if budget <= 0:
        return marker[:], 1, char_count
    cut = raw[:budget]
    while cut:
        try:
            head = cut.decode("utf-8")
            break
        except UnicodeDecodeError:
            cut = cut[:-1]
    else:
        head = ""
    stored = head + marker
    while len(stored.encode("utf-8")) > cap and stored:
        stored = stored[:-1]
    return stored, 1, char_count


def _norm_role(role: str | None) -> str:
    name = (role or "").strip().lower()
    if name in ("system", "developer"):
        return "system"
    if name == "assistant":
        return "assistant"
    if name == "meta":
        return "meta"
    return "user"


def message_text(message: dict) -> str:
    """把一条 OpenAI 消息收成纯文本。不拼整段历史。"""
    raw_role = (message.get("role") or "").strip().lower()
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                kind = item.get("type")
                if kind in (None, "text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif kind == "image_url":
                    parts.append("[图片]")
        text = "\n".join(part for part in parts if part)
    elif content is None:
        text = ""
    else:
        text = str(content)
    if not text.strip() and raw_role == "assistant" and message.get("tool_calls"):
        blob = json.dumps(message.get("tool_calls"), ensure_ascii=False)[:600]
        text = "【请求调用工具】" + blob
    text = text.strip()
    if raw_role == "tool" and text and not text.startswith("【工具执行结果】"):
        text = "【工具执行结果】\n" + text
    return text


def _prepare_incoming(messages: list[dict] | None) -> list[dict]:
    prepared = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = _norm_role(message.get("role"))
        text = message_text(message)
        if not text:
            continue
        redacted = redact_text(text)
        prepared.append({
            "role": role,
            "text": redacted,
            "hash": content_hash(redacted),
        })
    return prepared


def select_new_messages(prepared: list[dict], stored: list[dict],
                        muse_hashes: set[str], *, client_supplied: bool) -> list[dict]:
    """决定这一轮要新插入的请求侧消息。

    有 client_id 且已经有行：只插入前缀之后的新 user/assistant。
    第一轮：system（第一条 user 之前）+ 第一条 user。
    没有 client_id：只留本轮最后一条 user，不把整段旁聊再抄一遍。
    """
    if not client_supplied:
        for row in reversed(prepared):
            if row["role"] == "user":
                return [row]
        return []
    if not stored:
        selected = []
        for row in prepared:
            if row["role"] == "system":
                selected.append(row)
                continue
            if row["role"] == "user":
                selected.append(row)
                break
        return selected
    index = 0
    limit = min(len(prepared), len(stored))
    while index < limit:
        if stored[index]["role"] != prepared[index]["role"]:
            break
        if stored[index]["content_hash"] != prepared[index]["hash"]:
            break
        index += 1
    if index == 0:
        for row in reversed(prepared):
            if row["role"] != "user":
                continue
            last = stored[-1]
            if last["role"] == "user" and last["content_hash"] == row["hash"]:
                return []
            return [row]
        return []
    fresh = []
    for row in prepared[index:]:
        if row["role"] not in ("system", "user", "assistant"):
            continue
        if row["role"] == "assistant" and row["hash"] in muse_hashes:
            continue
        fresh.append(row)
    return fresh


def _short(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = redact_text(value.strip())
    if not text:
        return None
    return text[:limit]


def _session_id(value: str | None) -> str | None:
    text = _short(value, 128)
    if text and _SESSION_ID.fullmatch(text):
        return text
    return None


def _account_id(value: str | None) -> str | None:
    text = _short(value, 64)
    if text and _ACCOUNT_ID.fullmatch(text):
        return text
    return None


def _request_id(value: str | None) -> str | None:
    text = _short(value, 80)
    if text and _REQUEST_ID.fullmatch(text):
        return text
    return None


class RelayStore:
    def __init__(self, path: str, *, retention_days: int = 14,
                 max_bytes: int = 400 * 1024 * 1024):
        self.path = path
        self.retention_days = max(1, int(retention_days))
        self.max_bytes = max(1, int(max_bytes))
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = self._connect(path)
        self._init_schema()

    def _connect(self, path: str) -> sqlite3.Connection:
        fresh = (not os.path.exists(path)) or os.path.getsize(path) == 0
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        if fresh:
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    client_id TEXT,
                    ledger_key TEXT,
                    role TEXT,
                    project TEXT,
                    account_id TEXT,
                    account_label TEXT,
                    session_id TEXT,
                    model TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_nav TEXT,
                    last_request_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    bytes INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_client_active
                    ON conversations(client_id)
                    WHERE client_id IS NOT NULL AND client_id != '' AND status = 'active';
                CREATE INDEX IF NOT EXISTS idx_conversations_updated
                    ON conversations(updated_at);

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    source TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    char_count INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    request_id TEXT,
                    latency_ms INTEGER,
                    nav TEXT,
                    session_id TEXT,
                    model TEXT,
                    account_id TEXT,
                    error TEXT,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conv_seq
                    ON messages(conversation_id, seq);

                -- P2 选卡 / 查看 / 点击。P1 不写、不读、不提供接口。
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    message_id TEXT,
                    kind TEXT,
                    payload TEXT,
                    created_at INTEGER,
                    status TEXT
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _find_client(self, client_id: str | None):
        if not client_id:
            return None
        return self._conn.execute(
            """SELECT * FROM conversations
               WHERE client_id=? AND status='active'
               ORDER BY updated_at DESC LIMIT 1""",
            (client_id,),
        ).fetchone()

    def _find_id(self, conversation_id: str | None):
        if not conversation_id:
            return None
        return self._conn.execute(
            "SELECT * FROM conversations WHERE id=?",
            (conversation_id,),
        ).fetchone()

    def _insert_conversation(self, *, now: int, client_id, ledger_key, role, project,
                             account_id, account_label, session_id, model, nav,
                             request_id) -> sqlite3.Row:
        cid = _new_id("conv_")
        self._conn.execute(
            """INSERT INTO conversations (
                   id, client_id, ledger_key, role, project, account_id, account_label,
                   session_id, model, created_at, updated_at, last_nav, last_request_id,
                   status, bytes
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0)""",
            (cid, client_id, ledger_key, role, project, account_id, account_label,
             session_id, model, now, now, nav, request_id),
        )
        return self._find_id(cid)

    def _touch(self, row, *, now: int, ledger_key, role, project, account_id,
               account_label, session_id, model, nav, request_id, extra_bytes: int):
        # session 换号或气泡滚走时改同一行，不把历史拆成新对话。
        next_session = session_id or row["session_id"]
        self._conn.execute(
            """UPDATE conversations SET
                   ledger_key=COALESCE(?, ledger_key),
                   role=COALESCE(?, role),
                   project=COALESCE(?, project),
                   account_id=COALESCE(?, account_id),
                   account_label=COALESCE(?, account_label),
                   session_id=?,
                   model=COALESCE(?, model),
                   updated_at=?,
                   last_nav=COALESCE(?, last_nav),
                   last_request_id=COALESCE(?, last_request_id),
                   bytes=bytes + ?
               WHERE id=?""",
            (ledger_key, role, project, account_id, account_label, next_session,
             model, now, nav, request_id, extra_bytes, row["id"]),
        )
        return self._find_id(row["id"])

    def _stored_request(self, conversation_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT role, content_hash FROM messages
               WHERE conversation_id=? AND source='request_delta'
               ORDER BY seq""",
            (conversation_id,),
        ).fetchall()
        return [{"role": row["role"], "content_hash": row["content_hash"]} for row in rows]

    def _muse_hashes(self, conversation_id: str) -> set[str]:
        rows = self._conn.execute(
            """SELECT content_hash FROM messages
               WHERE conversation_id=? AND source='muse_reply'
                 AND content_hash IS NOT NULL""",
            (conversation_id,),
        ).fetchall()
        return {row["content_hash"] for row in rows}

    def _next_seq(self, conversation_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        return int(row[0]) + 1

    def _insert_message(self, *, conversation_id: str, turn_id: str, seq: int, role: str,
                        source: str, content: str, content_hash_value: str | None,
                        truncated: int, char_count: int, created_at: int, request_id,
                        latency_ms, nav, session_id, model, account_id, error) -> int:
        self._conn.execute(
            """INSERT INTO messages (
                   id, conversation_id, turn_id, seq, role, source, content, content_hash,
                   truncated, char_count, created_at, request_id, latency_ms, nav,
                   session_id, model, account_id, error
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_new_id("msg_"), conversation_id, turn_id, seq, role, source, content,
             content_hash_value, int(truncated), int(char_count), created_at, request_id,
             latency_ms, nav, session_id, model, account_id, error),
        )
        return len((content or "").encode("utf-8"))

    def _budget(self, pieces: list[dict]) -> int:
        """每条约 32 KiB，一轮合计不超过 64 KiB。超出的截断并标 truncated。"""
        meta = [piece for piece in pieces if piece["source"] == "meta"]
        body = [piece for piece in pieces if piece["source"] != "meta"]
        reserve = min(512, TURN_CAP // 8) if meta else 0
        remaining = TURN_CAP - reserve
        total = 0
        for piece in body:
            cap = min(MSG_CAP, max(0, remaining))
            stored, truncated, char_count = clip_text(piece["full"], cap)
            used = len(stored.encode("utf-8"))
            if used > remaining and stored:
                stored, truncated, char_count = clip_text(piece["full"], remaining)
                used = len(stored.encode("utf-8"))
            remaining -= used
            piece["content"] = stored
            piece["truncated"] = truncated
            piece["char_count"] = char_count
            total += used
        for piece in meta:
            cap = min(MSG_CAP, max(0, TURN_CAP - total))
            stored, truncated, char_count = clip_text(piece["full"], cap)
            piece["content"] = stored
            piece["truncated"] = truncated
            piece["char_count"] = char_count
            total += len(stored.encode("utf-8"))
        return total

    def record_turn(self, *, messages: list[dict] | None = None, reply: str = "",
                    error: str | None = None, client_id: str | None = None,
                    conversation_id: str | None = None, ledger_key: str | None = None,
                    role: str | None = None, project: str | None = None,
                    account_id: str | None = None, account_label: str | None = None,
                    session_id: str | None = None, model: str | None = None,
                    nav: str | None = None, request_id: str | None = None,
                    latency_ms: int | None = None, warmup: bool = False,
                    prompt_chars: int | None = None, prompt_hash: str | None = None,
                    client_supplied: bool = False, at: float | None = None) -> dict:
        """写入一轮。调用方不要把拼好的 prompt 传进来，只要 prompt_chars 和 prompt_hash。"""
        now = int(at if at is not None else time.time())
        supplied_client = _short(client_id, 128) if client_supplied else None
        if supplied_client and any(ord(ch) < 32 for ch in supplied_client):
            supplied_client = None
        ledger_key = _short(ledger_key, 160)
        role = _short(role, 64)
        project = _short(project, 64)
        account_id = _account_id(account_id)
        account_label = _short(account_label, 120)
        session_id = _session_id(session_id)
        model = _short(model, 64)
        nav = _short(nav, 32)
        request_id = _request_id(request_id)
        if prompt_hash and (len(prompt_hash) != _HASH_RE_LEN or any(c not in "0123456789abcdef" for c in prompt_hash)):
            prompt_hash = None
        try:
            prompt_chars = int(prompt_chars) if prompt_chars is not None else None
        except (TypeError, ValueError):
            prompt_chars = None
        if prompt_chars is not None and prompt_chars < 0:
            prompt_chars = None
        try:
            latency_ms = int(latency_ms) if latency_ms is not None else None
        except (TypeError, ValueError):
            latency_ms = None

        prepared = _prepare_incoming(messages)
        reply_red = redact_text(reply or "")
        error_red = redact_text(error) if error else None
        if error_red:
            error_red, _, _ = clip_text(error_red, 1024)

        with self._lock:
            self._begin()
            try:
                row = None
                if conversation_id:
                    row = self._find_id(conversation_id)
                if row is None and supplied_client:
                    row = self._find_client(supplied_client)
                stored = self._stored_request(row["id"]) if row else []
                muse_hashes = self._muse_hashes(row["id"]) if row else set()
                selected = select_new_messages(
                    prepared, stored, muse_hashes, client_supplied=bool(supplied_client),
                )
                if row is None:
                    row = self._insert_conversation(
                        now=now, client_id=supplied_client, ledger_key=ledger_key,
                        role=role, project=project, account_id=account_id,
                        account_label=account_label, session_id=session_id, model=model,
                        nav=nav, request_id=request_id,
                    )
                turn_id = _new_id("turn_")
                pieces = []
                if warmup:
                    pieces.append({
                        "role": "user", "source": "warmup", "full": ".",
                        "hash": content_hash("."), "latency": None, "error": None,
                    })
                for item in selected:
                    pieces.append({
                        "role": item["role"], "source": "request_delta", "full": item["text"],
                        "hash": item["hash"], "latency": None, "error": None,
                    })
                pieces.append({
                    "role": "assistant", "source": "muse_reply", "full": reply_red,
                    "hash": content_hash(reply_red), "latency": latency_ms, "error": error_red,
                })
                summary = {
                    "prompt_chars": prompt_chars,
                    "prompt_hash": prompt_hash,
                    "incoming": len(prepared),
                    "stored_request": len(selected),
                }
                pieces.append({
                    "role": "meta", "source": "meta",
                    "full": json.dumps(summary, ensure_ascii=False, sort_keys=True),
                    "hash": None, "latency": latency_ms, "error": None,
                })
                self._budget(pieces)
                seq = self._next_seq(row["id"])
                added = 0
                for piece in pieces:
                    added += self._insert_message(
                        conversation_id=row["id"], turn_id=turn_id, seq=seq,
                        role=piece["role"], source=piece["source"], content=piece["content"],
                        content_hash_value=piece["hash"] if piece["source"] != "meta" else content_hash(piece["content"]),
                        truncated=piece["truncated"], char_count=piece["char_count"],
                        created_at=now, request_id=request_id, latency_ms=piece["latency"],
                        nav=nav, session_id=session_id, model=model, account_id=account_id,
                        error=piece["error"],
                    )
                    seq += 1
                row = self._touch(
                    row, now=now, ledger_key=ledger_key, role=role, project=project,
                    account_id=account_id, account_label=account_label, session_id=session_id,
                    model=model, nav=nav, request_id=request_id, extra_bytes=added,
                )
                self._commit()
            except Exception:
                self._rollback()
                raise
        info = self._public_conv(row)
        info["prompt_chars"] = prompt_chars
        info["prompt_hash"] = prompt_hash
        info["reply_chars"] = len(reply or "")
        info["inserted"] = len(pieces)
        return info

    def ensure_conversation(self, *, client_id: str | None = None,
                            client_supplied: bool = False,
                            ledger_key: str | None = None, role: str | None = None,
                            project: str | None = None, account_id: str | None = None,
                            account_label: str | None = None, session_id: str | None = None,
                            model: str | None = None) -> dict:
        """流式响应要在正文之前带头。这里只保证有对话行，不写消息。"""
        now = int(time.time())
        supplied_client = _short(client_id, 128) if client_supplied else None
        with self._lock:
            self._begin()
            try:
                row = self._find_client(supplied_client) if supplied_client else None
                if row is None:
                    row = self._insert_conversation(
                        now=now, client_id=supplied_client,
                        ledger_key=_short(ledger_key, 160), role=_short(role, 64),
                        project=_short(project, 64), account_id=_account_id(account_id),
                        account_label=_short(account_label, 120),
                        session_id=_session_id(session_id), model=_short(model, 64),
                        nav=None, request_id=None,
                    )
                self._commit()
            except Exception:
                self._rollback()
                raise
        return self._public_conv(row)

    def _public_conv(self, row) -> dict:
        if row is None:
            return {}
        data = dict(row)
        data["account_label"] = redact_text(data.get("account_label") or "") or None
        data["conversation_id"] = data.get("id")
        data["nav"] = data.get("last_nav")
        return data

    def _public_message(self, row, *, include_type: bool = False) -> dict:
        data = dict(row)
        content = redact_text(data.get("content") or "")
        error = redact_text(data["error"]) if data.get("error") else None
        out = {
            "id": data["id"],
            "conversation_id": data["conversation_id"],
            "turn_id": data.get("turn_id"),
            "seq": data.get("seq"),
            "role": data.get("role"),
            "source": data.get("source"),
            "content": content,
            "content_hash": data.get("content_hash"),
            "truncated": int(data.get("truncated") or 0),
            "char_count": int(data.get("char_count") or 0),
            "created_at": data.get("created_at"),
            "request_id": data.get("request_id"),
            "latency_ms": data.get("latency_ms"),
            "nav": data.get("nav"),
            "session_id": data.get("session_id"),
            "model": data.get("model"),
            "account_id": data.get("account_id"),
            "error": error,
        }
        if include_type:
            out["type"] = "message"
        return out

    def get_conversation(self, conversation_id: str) -> dict | None:
        with self._lock:
            row = self._find_id(conversation_id)
        if row is None:
            return None
        return self._public_conv(row)

    def lookup(self, key: str) -> dict | None:
        """按客户端 id 找活跃对话。key 以 conv_ 开头时也接受内部 id。"""
        text = (key or "").strip()
        if not text:
            return None
        with self._lock:
            row = self._find_client(text)
            if row is None and text.startswith("conv_"):
                row = self._conn.execute(
                    "SELECT * FROM conversations WHERE id=? AND status='active'",
                    (text,),
                ).fetchone()
        if row is None:
            return None
        return self._public_conv(row)

    def list_conversations(self, *, limit: int = 50, offset: int = 0,
                           status: str | None = None, client_id: str | None = None,
                           role: str | None = None) -> list[dict]:
        limit = min(200, max(1, int(limit)))
        offset = max(0, int(offset))
        clauses = []
        args: list = []
        if status:
            clauses.append("status=?")
            args.append(status)
        if client_id:
            clauses.append("client_id=?")
            args.append(client_id)
        if role:
            clauses.append("role=?")
            args.append(role)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM conversations{where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._public_conv(row) for row in rows]

    def messages_for(self, conversation_id: str, *, restore: bool = False) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM messages WHERE conversation_id=?
                   ORDER BY seq, id""",
                (conversation_id,),
            ).fetchall()
        out = []
        for row in rows:
            if restore and row["source"] in ("warmup", "meta"):
                continue
            item = self._public_message(row)
            if restore:
                out.append({
                    "role": item["role"],
                    "content": item["content"],
                    "turn_id": item["turn_id"],
                    "created_at": item["created_at"],
                    "truncated": item["truncated"],
                    "source": item["source"],
                })
            else:
                out.append(item)
        return out

    def iter_jsonl(self, conversation_id: str | None = None):
        """按批吐出 JSONL。不要把整库 json.dump 进内存。"""
        if conversation_id:
            conv = self.get_conversation(conversation_id)
            conv_ids = [conversation_id] if conv else []
        else:
            conv_ids = None
        cursor = ""
        while conv_ids is None:
            with self._lock:
                rows = self._conn.execute(
                    """SELECT * FROM conversations WHERE id>? ORDER BY id LIMIT 20""",
                    (cursor,),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                cursor = row["id"]
                yield from self._yield_conversation(row)
            if len(rows) < 20:
                break
        if conv_ids is not None:
            for cid in conv_ids:
                with self._lock:
                    row = self._find_id(cid)
                if row is not None:
                    yield from self._yield_conversation(row)

    def _yield_conversation(self, row):
        public = self._public_conv(row)
        header = {
            "type": "conversation",
            "id": public.get("id"),
            "client_id": public.get("client_id"),
            "ledger_key": public.get("ledger_key"),
            "role": public.get("role"),
            "project": public.get("project"),
            "account_id": public.get("account_id"),
            "account_label": public.get("account_label"),
            "session_id": public.get("session_id"),
            "model": public.get("model"),
            "created_at": public.get("created_at"),
            "updated_at": public.get("updated_at"),
            "last_nav": public.get("last_nav"),
            "last_request_id": public.get("last_request_id"),
            "status": public.get("status"),
            "bytes": public.get("bytes"),
        }
        yield json.dumps(header, ensure_ascii=False) + "\n"
        seq = -1
        while True:
            with self._lock:
                msgs = self._conn.execute(
                    """SELECT * FROM messages
                       WHERE conversation_id=? AND seq>?
                       ORDER BY seq LIMIT 200""",
                    (row["id"], seq),
                ).fetchall()
            if not msgs:
                break
            for msg in msgs:
                seq = int(msg["seq"])
                yield json.dumps(self._public_message(msg, include_type=True), ensure_ascii=False) + "\n"
            if len(msgs) < 200:
                break

    def _delete_conversation(self, conversation_id: str) -> None:
        self._conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
        self._conn.execute("DELETE FROM actions WHERE conversation_id=?", (conversation_id,))
        self._conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))

    def cleanup(self, *, now: float | None = None, retention_days: int | None = None,
                max_bytes: int | None = None) -> dict:
        """删掉超期或超出容量的对话行。不碰账号、cookie、浏览器配置和 data/media。"""
        now_ts = int(now if now is not None else time.time())
        days = self.retention_days if retention_days is None else int(retention_days)
        limit = self.max_bytes if max_bytes is None else int(max_bytes)
        cutoff = now_ts - days * 86400
        deleted: list[str] = []
        with self._lock:
            self._begin()
            try:
                old = self._conn.execute(
                    "SELECT id FROM conversations WHERE updated_at < ?",
                    (cutoff,),
                ).fetchall()
                for row in old:
                    self._delete_conversation(row["id"])
                    deleted.append(row["id"])
                while True:
                    total = self._conn.execute(
                        "SELECT COALESCE(SUM(bytes), 0) FROM conversations"
                    ).fetchone()[0]
                    if int(total) <= limit:
                        break
                    oldest = self._conn.execute(
                        """SELECT id FROM conversations
                           ORDER BY updated_at ASC, created_at ASC, id ASC LIMIT 1"""
                    ).fetchone()
                    if oldest is None:
                        break
                    self._delete_conversation(oldest["id"])
                    deleted.append(oldest["id"])
                self._commit()
            except Exception:
                self._rollback()
                raise
            try:
                self._conn.execute("PRAGMA incremental_vacuum")
            except sqlite3.DatabaseError:
                pass
        return {
            "deleted": len(deleted),
            "conversation_ids": deleted[:50],
            "retention_days": days,
            "max_bytes": limit,
            "deleted_remote": False,
            "note": "只删除 relay.sqlite 里过期或超出容量的对话，不动 accounts.json、cookie、浏览器配置或 data/media",
        }
