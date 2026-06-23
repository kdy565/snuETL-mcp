"""로컬 누적 저장소 (SQLite) — course / 공지 / 과제 통합.

방식 (A): 원문/메타데이터를 그대로 저장하고 변경분만 증분 갱신한다.
세 엔터티(course, announcement, assignment)를 동일한 패턴으로 다루며,
값이 바뀔 때마다 공통 change_history 테이블에 누적 기록한다(덮어쓰지 않음).
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import canvas_client as cc

DB_PATH = Path(__file__).parent / "store" / "etl.db"

# 엔터티별 변경 추적 필드 (이 값이 바뀌면 change_history 에 기록)
_TRACKED = {
    "course": ["name", "workflow_state"],
    "announcement": ["title", "posted_at", "message"],
    "assignment": ["name", "due_at", "lock_at", "unlock_at", "points_possible"],
    # 열린게시판: 본문 수정뿐 아니라 새 답글(reply_count 증가)도 변경으로 본다
    "discussion": ["title", "posted_at", "message", "reply_count"],
}
# created 이력의 new_value 로 남길 대표 필드
_HEADLINE = {
    "course": "workflow_state", "announcement": "posted_at",
    "assignment": "due_at", "discussion": "posted_at",
}


# --- 유틸 ---------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trunc(v: Any, n: int = 300) -> Optional[str]:
    if v is None:
        return None
    s = str(v)
    return s if len(s) <= n else s[:n] + "…"


def _html_to_text(h: Optional[str]) -> str:
    if not h:
        return ""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", h, flags=re.S)
    t = re.sub(r"<br\s*/?>", "\n", t)
    t = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS courses (
                id INTEGER PRIMARY KEY, name TEXT, course_code TEXT,
                term_id INTEGER, workflow_state TEXT,
                first_seen TEXT, last_synced TEXT, last_changed TEXT
            );
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY, course_id INTEGER, course_name TEXT,
                title TEXT, posted_at TEXT, author TEXT, message TEXT, url TEXT,
                first_seen TEXT, last_synced TEXT, last_changed TEXT
            );
            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY, course_id INTEGER, course_name TEXT,
                name TEXT, due_at TEXT, lock_at TEXT, unlock_at TEXT,
                points_possible REAL, submission_types TEXT, html_url TEXT,
                first_seen TEXT, last_synced TEXT, last_changed TEXT
            );
            CREATE TABLE IF NOT EXISTS discussions (
                id INTEGER PRIMARY KEY, course_id INTEGER, course_name TEXT,
                title TEXT, posted_at TEXT, last_reply_at TEXT, author TEXT,
                message TEXT, reply_count INTEGER, url TEXT,
                first_seen TEXT, last_synced TEXT, last_changed TEXT
            );
            CREATE TABLE IF NOT EXISTS change_history (
                hist_id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT, entity_id INTEGER, course_id INTEGER,
                name TEXT, field TEXT, old_value TEXT, new_value TEXT,
                changed_at TEXT
            );
            """
        )


def _log(conn, etype, eid, course_id, name, field, old, new, ts) -> None:
    conn.execute(
        """INSERT INTO change_history
        (entity_type, entity_id, course_id, name, field, old_value, new_value, changed_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (etype, eid, course_id, name, field, _trunc(old), _trunc(new), ts),
    )


def _upsert(conn, table, etype, values, name_field, ts) -> str:
    """엔터티 1건을 upsert 하고 변경 이력을 누적. 'new'|'changed'|'unchanged' 반환."""
    eid = values["id"]
    name = values.get(name_field)
    existing = conn.execute(f"SELECT * FROM {table} WHERE id=?", (eid,)).fetchone()
    cols = list(values.keys())

    if existing is None:
        allcols = cols + ["first_seen", "last_synced", "last_changed"]
        conn.execute(
            f"INSERT INTO {table} ({','.join(allcols)}) "
            f"VALUES ({','.join(['?'] * len(allcols))})",
            [values[c] for c in cols] + [ts, ts, ts],
        )
        _log(conn, etype, eid, values.get("course_id"), name, "created",
             None, values.get(_HEADLINE[etype]), ts)
        return "new"

    diffs = [
        (f, existing[f], values.get(f))
        for f in _TRACKED[etype]
        if (existing[f] or None) != (values.get(f) or None)
    ]
    if diffs:
        setcols = [c for c in cols if c != "id"]
        conn.execute(
            f"UPDATE {table} SET {','.join(f'{c}=?' for c in setcols)}, "
            f"last_synced=?, last_changed=? WHERE id=?",
            [values[c] for c in setcols] + [ts, ts, eid],
        )
        for f, old, new in diffs:
            _log(conn, etype, eid, values.get("course_id"), name, f, old, new, ts)
        return "changed"

    conn.execute(f"UPDATE {table} SET last_synced=? WHERE id=?", (ts, eid))
    return "unchanged"


def _targets(course_id: Optional[int], active_only: bool) -> list[tuple[int, str]]:
    if course_id is not None:
        course = cc.get_canvas().get_course(course_id)
        return [(course_id, getattr(course, "name", str(course_id)))]
    return [(c["id"], c["name"]) for c in cc.list_courses(active_only=active_only)]


def _summary(counts: dict, **extra) -> dict:
    return {"synced_at": _now(), **extra, **counts}


# --- 동기화 (Sync) ------------------------------------------------------------


def sync_courses(active_only: bool = True) -> dict:
    """수강 강의 목록을 저장소에 누적 반영한다."""
    init_db()
    ts = _now()
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    with _conn() as conn:
        for c in cc.list_courses(active_only=active_only):
            values = {
                "id": c["id"], "name": c["name"], "course_code": c["course_code"],
                "term_id": c["term_id"], "workflow_state": c["workflow_state"],
            }
            counts[_upsert(conn, "courses", "course", values, "name", ts)] += 1
    return _summary(counts)


def sync_announcements(course_id: Optional[int] = None, active_only: bool = True) -> dict:
    """공지를 저장소에 누적 반영한다(본문은 텍스트로 정리해 저장, 수정 시 이력 기록)."""
    init_db()
    ts = _now()
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    targets = _targets(course_id, active_only)
    with _conn() as conn:
        for cid, cname in targets:
            course = cc.get_canvas().get_course(cid)
            for d in course.get_discussion_topics(only_announcements=True):
                a = cc.announcement_to_dict(d)
                values = {
                    "id": a["id"], "course_id": cid, "course_name": cname,
                    "title": a["title"], "posted_at": a["posted_at"],
                    "author": a["author"], "message": _html_to_text(a["message"]),
                    "url": a["html_url"],
                }
                counts[_upsert(conn, "announcements", "announcement", values, "title", ts)] += 1
    return _summary(counts, courses=len(targets))


def sync_discussions(course_id: Optional[int] = None, active_only: bool = True) -> dict:
    """열린게시판(토론) 글을 저장소에 누적 반영한다.

    본문은 텍스트로 정리해 저장하고, 본문 수정·새 답글(reply_count 증가) 시 이력을 남긴다.
    eTL 은 only_announcements 를 명시할 때만 토픽을 돌려주므로 False 를 명시한다.
    """
    init_db()
    ts = _now()
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    targets = _targets(course_id, active_only)
    with _conn() as conn:
        for cid, cname in targets:
            course = cc.get_canvas().get_course(cid)
            for d in course.get_discussion_topics(only_announcements=False):
                t = cc.discussion_to_dict(d)
                values = {
                    "id": t["id"], "course_id": cid, "course_name": cname,
                    "title": t["title"], "posted_at": t["posted_at"],
                    "last_reply_at": t["last_reply_at"], "author": t["author"],
                    "message": _html_to_text(t["message"]),
                    "reply_count": t["reply_count"], "url": t["html_url"],
                }
                counts[_upsert(conn, "discussions", "discussion", values, "title", ts)] += 1
    return _summary(counts, courses=len(targets))


def sync_assignments(course_id: Optional[int] = None, active_only: bool = True) -> dict:
    """과제를 저장소에 누적 반영한다(마감일 변경 시 이력 기록)."""
    init_db()
    ts = _now()
    counts = {"new": 0, "changed": 0, "unchanged": 0}
    targets = _targets(course_id, active_only)
    with _conn() as conn:
        for cid, cname in targets:
            course = cc.get_canvas().get_course(cid)
            for ass in course.get_assignments():
                a = cc.assignment_to_dict(ass)
                values = {
                    "id": a["id"], "course_id": cid, "course_name": cname,
                    "name": a["name"], "due_at": a["due_at"], "lock_at": a["lock_at"],
                    "unlock_at": a["unlock_at"], "points_possible": a["points_possible"],
                    "submission_types": json.dumps(a["submission_types"], ensure_ascii=False),
                    "html_url": a["html_url"],
                }
                counts[_upsert(conn, "assignments", "assignment", values, "name", ts)] += 1
    return _summary(counts, courses=len(targets))


def sync_all(active_only: bool = True) -> dict:
    """강의/공지/과제를 한 번에 동기화한다."""
    return {
        "courses": sync_courses(active_only=active_only),
        "announcements": sync_announcements(active_only=active_only),
        "discussions": sync_discussions(active_only=active_only),
        "assignments": sync_assignments(active_only=active_only),
    }


# --- 조회 (Read, API 호출 없이 캐시) ------------------------------------------


def _query(table: str, course_id: Optional[int], order: str) -> list[dict]:
    init_db()
    q = f"SELECT * FROM {table}"
    params: tuple = ()
    if course_id is not None and table != "courses":
        q += " WHERE course_id = ?"
        params = (course_id,)
    q += f" ORDER BY {order}"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def get_stored_courses() -> list[dict]:
    return _query("courses", None, "term_id DESC, name")


def get_stored_announcements(course_id: Optional[int] = None) -> list[dict]:
    return _query("announcements", course_id, "posted_at DESC")


def get_stored_discussions(course_id: Optional[int] = None) -> list[dict]:
    return _query("discussions", course_id, "posted_at DESC")


def get_stored_assignments(course_id: Optional[int] = None) -> list[dict]:
    return _query("assignments", course_id, "(due_at IS NULL), due_at")


def get_change_history(
    entity_type: Optional[str] = None,
    course_id: Optional[int] = None,
    entity_id: Optional[int] = None,
) -> list[dict]:
    """변경 이력(마감일/공지·게시판 수정 등)을 시간순으로 반환한다.

    entity_type: 'course' | 'announcement' | 'discussion' | 'assignment' (생략 시 전체).
    """
    init_db()
    q = "SELECT * FROM change_history"
    conds, params = [], []
    if entity_type:
        conds.append("entity_type = ?"); params.append(entity_type)
    if course_id is not None:
        conds.append("course_id = ?"); params.append(course_id)
    if entity_id is not None:
        conds.append("entity_id = ?"); params.append(entity_id)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY changed_at, hist_id"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]
