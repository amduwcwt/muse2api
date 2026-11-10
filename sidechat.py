"""旁聊台账（B×C）：role 钉账号，role[:project] 钉一条 Muse 旁聊 sessionId。

运行时文件与 accounts.json 同目录：data/sidechat_ledger.json。
仓库只提交 data/sidechat_ledger.example.json，不写真实 sessionId、cookie 或 API key。

没有 role/project 的请求不进台账，由调用方走策略 A（每次新开 /thread/new，
不再按气泡 <24 复用上一条旁聊）。
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid

from store import _read, _write

LEDGER_VERSION = 1
STALE_AFTER_SECONDS = 7 * 86400
MAX_ACTIVE_PER_ACCOUNT = 20
BUBBLE_ROLLOVER = 20

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_USER_KEY = re.compile(
    r"^role:(?P<role>[a-z0-9][a-z0-9_-]{0,63})"
    r"(?::proj:(?P<project>[a-z0-9][a-z0-9_-]{0,63}))?$"
)
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_RESERVED_THREAD = {"new", "main"}
_SECRET_KEYS = {
    "cookies", "cookie", "cookie_header", "authorization", "api_key",
    "password", "proxy",
}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class SidechatKeyError(ValueError):
    """role/project 不符合台账 key 约定。"""


def _lock_for(path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[path] = lock
        return lock


def _blank(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def parse_slug(value, field: str) -> str | None:
    """空值返回 None；非空必须是小写 slug。大写会先折成小写。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SidechatKeyError(f"{field} 必须是字符串")
    text = value.strip()
    if not text:
        return None
    slug = text.lower()
    if not _SLUG.fullmatch(slug):
        raise SidechatKeyError(
            f"{field} 只能是字母、数字、下划线或连字符（1–64），不能含空格或中文"
        )
    return slug


def ledger_key(role: str, project: str | None = None) -> str:
    if project:
        return f"role:{role}:proj:{project}"
    return f"role:{role}"


def session_id_from_path(path: str | None) -> str | None:
    """从 location.pathname 取出旁聊 sessionId。草稿 /thread/new 与主聊返回 None。"""
    if not path or not isinstance(path, str):
        return None
    clean = path.split("?", 1)[0].split("#", 1)[0]
    match = re.fullmatch(r"/thread/([^/]+)/?", clean)
    if not match:
        return None
    sid = match.group(1)
    if sid.lower() in _RESERVED_THREAD or not _SESSION_ID.fullmatch(sid):
        return None
    return sid


def thread_url(session_id: str | None) -> str:
    if (session_id and _SESSION_ID.fullmatch(session_id)
            and session_id.lower() not in _RESERVED_THREAD):
        return f"https://muse.ai/thread/{session_id}"
    return "https://muse.ai/thread/new"


def resolve_dimensions(*, header_role=None, header_project=None,
                       muse_role=None, muse_project=None,
                       body_role=None, body_project=None,
                       user=None) -> tuple[str | None, str | None]:
    """解析本次请求的 role/project。

    同一字段的优先级：请求头 > body 的 muse_role/muse_project > body 的 role/project。
    两边都没给 role 时，若 OpenAI `user` 整段匹配
    `role:<slug>` 或 `role:<slug>:proj:<slug>`，用这段补齐。
    普通 user id（例如 `user_123`）忽略，请求退回策略 A。
    只给 project 不给 role 会报错。
    """
    def first(pairs) -> str | None:
        for name, value in pairs:
            if isinstance(value, str) and not value.strip():
                continue
            if value is None:
                continue
            return parse_slug(value, name)
        return None

    role = first((
        ("X-Muse-Role", header_role),
        ("muse_role", muse_role),
        ("role", body_role),
    ))
    project = first((
        ("X-Muse-Project", header_project),
        ("muse_project", muse_project),
        ("project", body_project),
    ))
    match = _USER_KEY.fullmatch(_blank(user).lower()) if _blank(user) else None
    if match:
        user_role = match.group("role")
        user_project = match.group("project")
        if role is None and project is None:
            role, project = user_role, user_project
        elif role is None and user_role:
            if project is not None and user_project is not None and project != user_project:
                pass
            else:
                role = user_role
                if project is None:
                    project = user_project
        elif role == user_role and project is None and user_project:
            project = user_project
    if project and not role:
        raise SidechatKeyError(
            "传入了 project 但没有 role；请同时提供 X-Muse-Role 或 muse_role"
        )
    return role, project


def _usable(account: dict, *, allow_unhealthy: bool = False) -> bool:
    if not account or not account.get("id"):
        return False
    if not account.get("enabled", True):
        return False
    has_cookies = account.get("has_cookies")
    if has_cookies is None:
        has_cookies = bool(account.get("cookies"))
    if not has_cookies:
        return False
    if account.get("ok") is False and not allow_unhealthy:
        return False
    return True


def _empty_doc() -> dict:
    return {
        "version": LEDGER_VERSION,
        "updated_at": None,
        "entries": [],
        "role_account_map": {},
    }


def _scrub(value):
    """台账落盘前丢掉任何看起来像凭据的字段。"""
    if isinstance(value, dict):
        return {
            str(k): _scrub(v)
            for k, v in value.items()
            if str(k).lower() not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


class SidechatLedger:
    def __init__(self, path: str):
        self.path = path
        self._lock = _lock_for(path)
        self._disk_sig = None
        with self._lock:
            self.data = self._load()
            self._disk_sig = self._stat_sig()

    def _stat_sig(self):
        """文件身份。外部 CLI 改盘后靠它发现，不必重启进程。"""
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size)

    def _reload_if_disk_changed(self):
        """在 self._lock 内调用。CLI 的 map/cleanup 和本进程不是同一份内存。"""
        sig = self._stat_sig()
        if sig == self._disk_sig:
            return
        self.data = self._load()
        self._disk_sig = self._stat_sig()

    def _load(self) -> dict:
        raw = _read(self.path, None)
        if not isinstance(raw, dict):
            return _empty_doc()
        raw = _scrub(raw)
        raw["version"] = LEDGER_VERSION
        entries = raw.get("entries")
        raw["entries"] = entries if isinstance(entries, list) else []
        role_map = raw.get("role_account_map")
        raw["role_account_map"] = role_map if isinstance(role_map, dict) else {}
        raw.setdefault("updated_at", None)
        return raw

    def _save(self):
        self.data["version"] = LEDGER_VERSION
        self.data["updated_at"] = int(time.time())
        self.data = _scrub(self.data)
        _write(self.path, self.data)
        self._disk_sig = self._stat_sig()

    def snapshot(self) -> dict:
        with self._lock:
            self._reload_if_disk_changed()
            return _scrub({
                "version": self.data.get("version", LEDGER_VERSION),
                "updated_at": self.data.get("updated_at"),
                "entries": [dict(e) for e in self.data.get("entries") or []],
                "role_account_map": dict(self.data.get("role_account_map") or {}),
            })

    def _mapped_account(self, role: str | None) -> str | None:
        if not role:
            return None
        role_map = self.data.get("role_account_map") or {}
        chosen = role_map.get(role) or role_map.get("default")
        if isinstance(chosen, str) and chosen.strip():
            return chosen.strip()
        return None

    def _find(self, key: str) -> dict | None:
        for entry in self.data.get("entries") or []:
            if entry.get("ledger_key") == key and entry.get("status") != "deleted":
                return entry
        return None

    def plan(self, role: str | None, project: str | None,
             accounts: list[dict] | None) -> dict:
        """决定策略 A 或 B×C，以及钉住的账号和可续用的 sessionId。

        只读。账号列表用 has_cookies / enabled / ok，不要把 cookie 值传进来。
        已有 active 旁聊时优先续用它所在的账号（session 不能跨号）。
        role_account_map 用于新建，或当前条目的账号已不可用时。
        """
        accounts = list(accounts or [])
        if not role and not project:
            return {
                "strategy": "A",
                "ledger_key": None,
                "role": None,
                "project": None,
                "account_id": None,
                "account_label": None,
                "session_id": None,
                "label": None,
                "reason": "anonymous",
                "failover": False,
                "retire_session_id": None,
            }
        if project and not role:
            raise SidechatKeyError(
                "传入了 project 但没有 role；请同时提供 X-Muse-Role 或 muse_role"
            )
        key = ledger_key(role, project)
        base = {
            "strategy": "bc",
            "ledger_key": key,
            "role": role,
            "project": project,
            "label": key,
            "failover": False,
            "retire_session_id": None,
        }
        with self._lock:
            self._reload_if_disk_changed()
            entry = self._find(key)
            mapped = self._mapped_account(role)
        entry_id = (entry or {}).get("muse_account_id")
        entry_status = (entry or {}).get("status")
        entry_session = (entry or {}).get("session_id") if entry_status == "active" else None
        bubbles = (entry or {}).get("bubble_hint")
        rollover = isinstance(bubbles, int) and bubbles >= BUBBLE_ROLLOVER

        def label_of(aid: str | None) -> str | None:
            if not aid:
                return None
            for account in accounts:
                if account.get("id") == aid:
                    return account.get("label") or ""
            return (entry or {}).get("muse_account_label") or ""

        def finish(account_id, session_id, reason, failover=False, retire=None):
            return {
                **base,
                "account_id": account_id,
                "account_label": label_of(account_id),
                "session_id": session_id,
                "reason": reason,
                "failover": failover,
                "retire_session_id": retire,
            }

        if (entry_status == "active" and entry_session
                and _usable(next((a for a in accounts if a.get("id") == entry_id),
                                 {"id": entry_id, "enabled": False}))):
            if rollover:
                return finish(entry_id, None, "rollover", retire=entry_session)
            return finish(entry_id, entry_session, "resume")

        if mapped and _usable(next((a for a in accounts if a.get("id") == mapped),
                                   {"id": mapped, "enabled": False})):
            retire = entry_session if entry_id and entry_id != mapped else None
            return finish(
                mapped, None, "role_map",
                failover=bool(retire), retire=retire,
            )

        if entry_id and _usable(next((a for a in accounts if a.get("id") == entry_id),
                                     {"id": entry_id, "enabled": False})):
            if entry_session and rollover:
                return finish(entry_id, None, "rollover", retire=entry_session)
            return finish(entry_id, entry_session, "entry")

        healthy = [a for a in accounts if _usable(a)]
        pool = healthy or [a for a in accounts if _usable(a, allow_unhealthy=True)]
        avoid = {x for x in (entry_id, mapped) if x}
        preferred = [a for a in pool if a.get("id") not in avoid] or pool
        if not preferred:
            return finish(None, None, "no_account", failover=bool(entry_session),
                          retire=entry_session)
        preferred.sort(key=lambda a: (a.get("last_used") or 0, a.get("use_count") or 0))
        chosen = preferred[0]
        return finish(
            chosen.get("id"), None,
            "failover" if entry_session or (mapped and mapped != chosen.get("id")) else "assign",
            failover=bool(entry_session or (mapped and mapped not in (None, chosen.get("id")))),
            retire=entry_session,
        )

    def record_result(self, *, ledger_key: str, role: str | None, project: str | None,
                      account_id: str | None, account_label: str = "",
                      session_id: str | None = None, bubble_hint: int | None = None,
                      renamed: bool = False, nav: str | None = None,
                      retire_session_id: str | None = None,
                      failover: bool = False) -> dict | None:
        """首条发出后回填 sessionId。没有新 id 且旧线程已作废时改为 pending，避免再续用。"""
        if not ledger_key or not account_id:
            return None
        if session_id and not _SESSION_ID.fullmatch(str(session_id)):
            session_id = None
        now = int(time.time())
        with self._lock:
            self._reload_if_disk_changed()
            entry = self._find(ledger_key)
            if entry is None:
                entry = {
                    "id": "sc_" + uuid.uuid4().hex[:8],
                    "ledger_key": ledger_key,
                    "role": role,
                    "project": project,
                    "created_at": now,
                    "use_count": 0,
                    "pin": False,
                    "notes": "",
                    "prior_session_id": None,
                    "session_id": None,
                    "status": "pending",
                }
                self.data["entries"].append(entry)
            old_sid = entry.get("session_id")
            drop_old = nav in ("stale", "rollover") or failover or (
                retire_session_id and retire_session_id != session_id
            )
            if session_id:
                if old_sid and old_sid != session_id:
                    entry["prior_session_id"] = old_sid
                elif retire_session_id and retire_session_id != session_id:
                    entry["prior_session_id"] = retire_session_id
                entry["session_id"] = session_id
                entry["status"] = "active"
            elif drop_old:
                if old_sid:
                    entry["prior_session_id"] = old_sid
                elif retire_session_id:
                    entry["prior_session_id"] = retire_session_id
                entry["session_id"] = None
                entry["status"] = "pending"
            entry["role"] = role
            entry["project"] = project
            entry["muse_account_id"] = account_id
            entry["muse_account_label"] = account_label or entry.get("muse_account_label") or ""
            entry["label"] = ledger_key
            entry["last_used_at"] = now
            entry["use_count"] = int(entry.get("use_count") or 0) + 1
            if bubble_hint is not None:
                try:
                    entry["bubble_hint"] = int(bubble_hint)
                except (TypeError, ValueError):
                    pass
            entry["renamed"] = bool(renamed)
            entry["last_nav"] = nav
            self._save()
            return dict(entry)

    def invalidate_account(self, account_id: str, reason: str = "") -> int:
        """该账号下的旁聊全部标 stale。旧 session 不能换号续用。"""
        if not account_id:
            return 0
        now = int(time.time())
        changed = 0
        with self._lock:
            self._reload_if_disk_changed()
            for entry in self.data.get("entries") or []:
                if entry.get("muse_account_id") != account_id:
                    continue
                if entry.get("status") not in ("active", "pending"):
                    continue
                entry["status"] = "stale"
                entry["stale_reason"] = (reason or "account")[:120]
                entry["stale_at"] = now
                changed += 1
            if changed:
                self._save()
        return changed

    def mark_stale_key(self, key: str, reason: str = "manual") -> bool:
        with self._lock:
            self._reload_if_disk_changed()
            entry = self._find(key)
            if not entry:
                return False
            if entry.get("status") in ("active", "pending"):
                entry["status"] = "stale"
                entry["stale_reason"] = (reason or "manual")[:120]
                entry["stale_at"] = int(time.time())
                self._save()
            return True

    def set_role_account(self, role: str, account_id: str | None) -> dict:
        slug = parse_slug(role, "role")
        if not slug:
            raise SidechatKeyError("role 不能为空")
        with self._lock:
            self._reload_if_disk_changed()
            role_map = self.data.setdefault("role_account_map", {})
            if account_id is None or (isinstance(account_id, str) and not account_id.strip()):
                role_map.pop(slug, None)
            else:
                if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id.strip()):
                    raise SidechatKeyError("account id 只能是字母、数字、下划线或连字符")
                role_map[slug] = account_id.strip()
            self._save()
            return dict(role_map)

    def cleanup(self, *, now: float | None = None,
                stale_after: float = STALE_AFTER_SECONDS,
                max_per_account: int = MAX_ACTIVE_PER_ACCOUNT) -> dict:
        """7 天未用，或同一账号非 pin 旁聊超过上限时，把最旧条目标成 stale。

        不调用 Muse session.delete。侧栏里的线程要在 Muse 里删，或等后续 RPC。
        """
        now_ts = int(now if now is not None else time.time())
        marked: list[str] = []
        with self._lock:
            self._reload_if_disk_changed()
            for entry in self.data.get("entries") or []:
                if entry.get("pin") or entry.get("status") not in ("active", "pending"):
                    continue
                stamp = entry.get("last_used_at") or entry.get("created_at") or 0
                try:
                    stamp = int(stamp)
                except (TypeError, ValueError):
                    stamp = 0
                if stamp and now_ts - stamp > stale_after:
                    entry["status"] = "stale"
                    entry["stale_reason"] = "idle"
                    entry["stale_at"] = now_ts
                    marked.append(entry.get("ledger_key") or "")
            groups: dict[str, list] = {}
            for entry in self.data.get("entries") or []:
                if entry.get("pin") or entry.get("status") not in ("active", "pending"):
                    continue
                aid = entry.get("muse_account_id") or ""
                groups.setdefault(aid, []).append(entry)
            for rows in groups.values():
                if len(rows) <= max_per_account:
                    continue
                rows.sort(key=lambda e: (
                    e.get("last_used_at") or e.get("created_at") or 0,
                    e.get("use_count") or 0,
                ))
                for entry in rows[:len(rows) - max_per_account]:
                    entry["status"] = "stale"
                    entry["stale_reason"] = "lru"
                    entry["stale_at"] = now_ts
                    key = entry.get("ledger_key") or ""
                    if key not in marked:
                        marked.append(key)
            if marked:
                self._save()
        return {"stale": len(marked), "ledger_keys": marked,
                "deleted_remote": False,
                "note": "只改台账 status，没有调用 session.delete"}
