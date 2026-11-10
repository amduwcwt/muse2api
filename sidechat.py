"""旁聊台账（B×C）：role 钉账号，role[:project] 钉一条 Muse 旁聊 sessionId。

运行时文件与 accounts.json 同目录：data/sidechat_ledger.json。
仓库只提交 data/sidechat_ledger.example.json，不写真实 sessionId、cookie 或 API key。

没有 role/project 的请求不进台账，由调用方走策略 A（每次新开 /thread/new，
不再按气泡 <24 复用上一条旁聊）。

显式账号（X-Muse-Account / muse_account / account_id）压过角色映射。
跨账号时 sessionId 只进 prior，不能换号续用。
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid

from store import _read, _write, quota_exhausted

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
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
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


def parse_account_id(value, field: str) -> str | None:
    """空值返回 None。非空必须是账号池 id 字符集，不在这里查池子。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SidechatKeyError(f"{field} 必须是字符串")
    text = value.strip()
    if not text:
        return None
    if not _ACCOUNT_ID.fullmatch(text):
        raise SidechatKeyError(
            f"{field} 只能是字母、数字、下划线或连字符（1–64），且必须是账号池里的 id"
        )
    return text


def resolve_explicit_account(*, header_account=None, muse_account=None,
                             body_account=None) -> str | None:
    """显式钉号。优先级：X-Muse-Account > body muse_account > body account_id。

    不解析 OpenAI `user`，避免把普通 user id 当成账号。
    """
    for name, value in (
        ("X-Muse-Account", header_account),
        ("muse_account", muse_account),
        ("account_id", body_account),
    ):
        if isinstance(value, str) and not value.strip():
            continue
        if value is None:
            continue
        return parse_account_id(value, name)
    return None


def _has_base(account: dict | None) -> bool:
    if not account or not account.get("id"):
        return False
    if not account.get("enabled", True):
        return False
    has_cookies = account.get("has_cookies")
    if has_cookies is None:
        has_cookies = bool(account.get("cookies"))
    return bool(has_cookies)


def classic_block_reason(account: dict | None) -> str | None:
    """禁用 / 无 cookie / ok=false。冷却和额度不在这里。"""
    if not account or not account.get("id"):
        return "missing"
    if not account.get("enabled", True):
        return "disabled"
    if not _has_base(account):
        return "no_cookie"
    if account.get("ok") is False:
        return "unhealthy"
    return None


def account_health_reason(account: dict | None, *, require_picker: bool = False,
                          now: float | None = None) -> str | None:
    """冷却、额度耗尽、Wonder/Ember 无选卡。经典不可用返回 None（由 classic_block_reason 管）。"""
    if not account or classic_block_reason(account):
        return None
    fo = account.get("failover") if isinstance(account.get("failover"), dict) else {}
    now_ts = int(time.time() if now is None else now)
    try:
        cooldown_until = int(fo.get("cooldown_until") or 0)
    except (TypeError, ValueError):
        cooldown_until = 0
    if cooldown_until > now_ts:
        return "cooldown"
    if quota_exhausted(account.get("quota")):
        return "quota"
    if require_picker and fo.get("no_picker"):
        return "no_picker"
    return None


def busy_degrade_decision(*, busy: bool, allow: bool, live: bool,
                          alternate_id: str | None) -> dict:
    """显式账号正忙时：LIVE 切到空闲号；否则只报告本会切到谁。"""
    if not (busy and allow):
        return {"action": "stay"}
    if not alternate_id:
        return {"action": "wait", "reason": "busy"}
    if live:
        return {"action": "degrade", "account_id": alternate_id, "reason": "busy"}
    return {"action": "dry", "would_account_id": alternate_id, "reason": "busy"}


def enforce_same_account_session(plan: dict | None, account_id: str | None) -> dict | None:
    """旁聊 session 不能换号续用。账号变了就丢掉 session_id。"""
    if not plan or not account_id or not plan.get("session_id"):
        return plan
    owner = plan.get("bound_account_id") or plan.get("account_id")
    if owner and owner != account_id:
        plan = dict(plan)
        plan["retire_session_id"] = plan.get("session_id")
        plan["session_id"] = None
        plan["failover"] = True
        plan["account_from"] = plan.get("account_from") or owner
        if plan.get("reason") in (None, "resume", "entry", "role_map", "assign"):
            plan["reason"] = "explicit" if plan.get("explicit_account") else "failover"
    return plan


def follow_role_account(ledger, plan: dict | None, account_id: str | None) -> bool:
    """换号成功后把 role_account_map 改到新账号。没有真正 failover 时不改。"""
    if not plan or plan.get("strategy") != "bc" or not plan.get("failover"):
        return False
    role = plan.get("role")
    if not role or not account_id:
        return False
    current = (ledger.snapshot().get("role_account_map") or {}).get(role)
    if current == account_id:
        return False
    ledger.set_role_account(role, account_id)
    return True


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
             accounts: list[dict] | None, *,
             explicit_account: str | None = None,
             require_picker: bool = False,
             live: bool = False,
             now: float | None = None) -> dict:
        """决定策略 A 或 B×C，以及钉住的账号和可续用的 sessionId。

        只读。账号列表用 has_cookies / enabled / ok / failover，不要把 cookie 值传进来。
        选号优先级：显式账号 > 可续用的 active 旁聊所在账号 > role_account_map > 池默认。
        已有 active 旁聊时优先续用它所在的账号（session 不能跨号）。
        live=False 时，冷却 / 额度 / no-picker 不改实际账号，只填 would_*。
        """
        accounts = list(accounts or [])
        now_ts = time.time() if now is None else now
        by_id = {a.get("id"): a for a in accounts if a.get("id")}

        def blank(strategy: str, **extra) -> dict:
            row = {
                "strategy": strategy,
                "ledger_key": None,
                "role": role,
                "project": project,
                "account_id": None,
                "account_label": None,
                "session_id": None,
                "label": None,
                "reason": "anonymous",
                "failover": False,
                "retire_session_id": None,
                "explicit_account": explicit_account,
                "would_failover": False,
                "would_account_id": None,
                "would_reason": None,
                "account_from": None,
                "bound_account_id": None,
                "attempts": 1,
            }
            row.update(extra)
            return row

        def usable_stay(account) -> bool:
            if classic_block_reason(account):
                return False
            if not live:
                return True
            return account_health_reason(
                account, require_picker=require_picker, now=now_ts) is None

        def pick_pool(avoid: set[str], *, strict: bool) -> dict | None:
            def ok(account) -> bool:
                if strict:
                    return (classic_block_reason(account) is None
                            and account_health_reason(
                                account, require_picker=require_picker, now=now_ts) is None)
                return classic_block_reason(account) is None

            primary = [a for a in accounts if ok(a) and a.get("id") not in avoid]
            pool = primary or [a for a in accounts if ok(a)]
            if not pool and not strict:
                soft = [a for a in accounts if _has_base(a) and a.get("id") not in avoid]
                pool = soft or [a for a in accounts if _has_base(a)]
            if not pool:
                return None
            pool.sort(key=lambda a: (a.get("last_used") or 0, a.get("use_count") or 0))
            return pool[0]

        if not role and not project:
            anon = blank("A")
            if not explicit_account:
                return anon
            chosen = by_id.get(explicit_account)
            if usable_stay(chosen):
                anon["account_id"] = explicit_account
                anon["account_label"] = (chosen or {}).get("label") or ""
                anon["reason"] = "explicit"
                why = account_health_reason(chosen, require_picker=require_picker, now=now_ts)
                if why and not live:
                    alt = pick_pool({explicit_account}, strict=True)
                    if alt and alt.get("id") == explicit_account:
                        alt = None
                    anon["would_failover"] = bool(alt)
                    anon["would_account_id"] = (alt or {}).get("id")
                    anon["would_reason"] = why
                    anon["account_from"] = explicit_account
                return anon
            alt = pick_pool({explicit_account}, strict=bool(live or require_picker))
            if live and require_picker and account_health_reason(
                    chosen, require_picker=True, now=now_ts) == "no_picker" and not alt:
                anon["reason"] = "no_picker"
                anon["failover"] = True
                anon["account_from"] = explicit_account
                return anon
            if not alt:
                anon["reason"] = "no_account"
                anon["failover"] = True
                anon["account_from"] = explicit_account
                return anon
            why = classic_block_reason(chosen) or account_health_reason(
                chosen, require_picker=require_picker, now=now_ts) or "failover"
            if not live and not classic_block_reason(chosen):
                anon["account_id"] = explicit_account
                anon["account_label"] = (chosen or {}).get("label") or ""
                anon["reason"] = "explicit"
                anon["would_failover"] = True
                anon["would_account_id"] = alt.get("id")
                anon["would_reason"] = why
                anon["account_from"] = explicit_account
                return anon
            anon["account_id"] = alt.get("id")
            anon["account_label"] = alt.get("label") or ""
            anon["reason"] = why if why in ("quota", "no_picker", "cooldown") else "failover"
            anon["failover"] = True
            anon["account_from"] = explicit_account
            return anon

        if project and not role:
            raise SidechatKeyError(
                "传入了 project 但没有 role；请同时提供 X-Muse-Role 或 muse_role"
            )
        key = ledger_key(role, project)
        base = blank(
            "bc",
            ledger_key=key,
            role=role,
            project=project,
            label=key,
            reason="assign",
        )
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
            found = by_id.get(aid)
            if found:
                return found.get("label") or ""
            return (entry or {}).get("muse_account_label") or ""

        def finish(account_id, session_id, reason, failover=False, retire=None,
                   account_from=None):
            bound = account_id if session_id else None
            return {
                **base,
                "account_id": account_id,
                "account_label": label_of(account_id),
                "session_id": session_id,
                "reason": reason,
                "failover": failover,
                "retire_session_id": retire,
                "account_from": account_from,
                "bound_account_id": bound,
            }

        def other_than(avoid_id: str | None) -> dict | None:
            alt = pick_pool({avoid_id} if avoid_id else set(), strict=True)
            if alt and alt.get("id") == avoid_id:
                return None
            return alt

        def dry_wrap(result: dict) -> dict:
            if live or not result.get("account_id"):
                return result
            chosen = by_id.get(result["account_id"])
            why = account_health_reason(chosen, require_picker=require_picker, now=now_ts)
            if not why:
                return result
            alt = other_than(result["account_id"])
            result = dict(result)
            result["would_failover"] = bool(alt)
            result["would_account_id"] = (alt or {}).get("id")
            result["would_reason"] = why
            result["account_from"] = result["account_id"]
            return result

        if explicit_account:
            chosen = by_id.get(explicit_account)
            if usable_stay(chosen):
                same = (entry_status == "active" and entry_session
                        and entry_id == explicit_account and not rollover)
                if same:
                    return dry_wrap(finish(explicit_account, entry_session, "explicit"))
                if (rollover and entry_id == explicit_account and entry_session
                        and usable_stay(chosen)):
                    return dry_wrap(finish(
                        explicit_account, None, "rollover", retire=entry_session))
                dropped = entry_session if entry_id and entry_id != explicit_account else None
                moved = bool(dropped or (mapped and mapped != explicit_account))
                return dry_wrap(finish(
                    explicit_account, None, "explicit",
                    failover=moved, retire=dropped,
                    account_from=(entry_id or mapped) if moved else None,
                ))
            why = classic_block_reason(chosen) or account_health_reason(
                chosen, require_picker=require_picker, now=now_ts)
            if not live and not classic_block_reason(chosen):
                same = (entry_status == "active" and entry_session
                        and entry_id == explicit_account and not rollover)
                stayed = finish(
                    explicit_account,
                    entry_session if same else None,
                    "explicit",
                    retire=entry_session if entry_id and entry_id != explicit_account else None,
                    failover=bool(entry_id and entry_id != explicit_account and entry_session),
                    account_from=explicit_account,
                )
                return dry_wrap(stayed)
            alt = pick_pool({explicit_account}, strict=True if live else False)
            if not alt and live and why == "no_picker":
                return finish(None, None, "no_picker", failover=True,
                              retire=entry_session, account_from=explicit_account)
            if not alt:
                return finish(None, None, "no_account", failover=bool(entry_session),
                              retire=entry_session, account_from=explicit_account)
            reason = why if why in ("quota", "no_picker", "cooldown") else "failover"
            return finish(
                alt.get("id"), None, reason, failover=True,
                retire=entry_session, account_from=explicit_account,
            )

        entry_acc = by_id.get(entry_id)
        left_reason = None
        if entry_status == "active" and entry_session and usable_stay(entry_acc):
            if rollover:
                return dry_wrap(finish(entry_id, None, "rollover", retire=entry_session))
            return dry_wrap(finish(entry_id, entry_session, "resume"))
        if entry_status == "active" and entry_session and entry_acc is not None:
            left_reason = (
                classic_block_reason(entry_acc)
                or account_health_reason(entry_acc, require_picker=require_picker, now=now_ts)
            )

        map_acc = by_id.get(mapped) if mapped else None
        if mapped and usable_stay(map_acc):
            retire = entry_session if entry_id and entry_id != mapped else None
            reason = left_reason if left_reason in ("quota", "no_picker", "cooldown") else "role_map"
            failover = bool(retire or reason != "role_map")
            return dry_wrap(finish(
                mapped, None, reason,
                failover=failover,
                retire=entry_session if failover else retire,
                account_from=entry_id if failover else None,
            ))
        if mapped and map_acc is not None and not usable_stay(map_acc):
            left_reason = left_reason or (
                classic_block_reason(map_acc)
                or account_health_reason(map_acc, require_picker=require_picker, now=now_ts)
            )

        if entry_id and usable_stay(entry_acc):
            if entry_session and rollover:
                return dry_wrap(finish(entry_id, None, "rollover", retire=entry_session))
            return dry_wrap(finish(entry_id, entry_session, "entry"))

        avoid = {x for x in (entry_id, mapped) if x}
        strict = bool(live and left_reason in ("quota", "no_picker", "cooldown"))
        chosen = pick_pool(avoid, strict=strict)
        if not chosen and strict:
            return finish(None, None, left_reason or "no_picker", failover=True,
                          retire=entry_session, account_from=entry_id or mapped)
        if not chosen:
            return finish(None, None, "no_account", failover=bool(entry_session),
                          retire=entry_session, account_from=entry_id or mapped)
        health_reason = left_reason if left_reason in ("quota", "no_picker", "cooldown") else None
        switched = bool(entry_session or (mapped and mapped != chosen.get("id")) or health_reason)
        reason = health_reason or ("failover" if switched else "assign")
        return finish(
            chosen.get("id"), None, reason,
            failover=switched,
            retire=entry_session,
            account_from=(entry_id or mapped) if switched else None,
        )

    def record_result(self, *, ledger_key: str, role: str | None, project: str | None,
                      account_id: str | None, account_label: str = "",
                      session_id: str | None = None, bubble_hint: int | None = None,
                      renamed: bool = False, nav: str | None = None,
                      retire_session_id: str | None = None,
                      failover: bool = False,
                      failover_reason: str | None = None) -> dict | None:
        """首条发出后回填 sessionId。没有新 id 且旧线程已作废时改为 pending，避免再续用。

        跨账号时丢掉原来的 sessionId，只把它记进 prior_session_id。
        """
        if not ledger_key or not account_id:
            return None
        if session_id and not _SESSION_ID.fullmatch(str(session_id)):
            session_id = None
        now = int(time.time())
        with self._lock:
            self._reload_if_disk_changed()
            entry = self._find(ledger_key)
            old_account = (entry or {}).get("muse_account_id")
            old_sid = (entry or {}).get("session_id")
            if old_account and account_id and old_account != account_id:
                if session_id and session_id == old_sid:
                    session_id = None
                failover = True
                if old_sid and not retire_session_id:
                    retire_session_id = old_sid
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
            if failover:
                entry["failover_count"] = int(entry.get("failover_count") or 0) + 1
                entry["last_failover_at"] = now
                if failover_reason:
                    entry["last_failover_reason"] = str(failover_reason)[:64]
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
