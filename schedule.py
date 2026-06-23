"""일정(Schedule) 정규화 — 목표 1(캘린더 연동) 지원.

두 종류의 일정을 분리해서 다룬다.

1) `get_schedule_events` — 저장소(store.db)의 *구조화된* 날짜 필드
   (과제 due_at/lock_at 등)를 DESIGN.md 의 공용 일정 스키마로 변환한다.
   Canvas 가 날짜를 필드로 들고 있으므로 추측이 아니라 정확한 값이다.

2) `extract_events_from_announcements` — 공지 *본문(자유 텍스트)* 에서
   날짜·키워드 *후보* 만 정규식으로 뽑아 문맥과 함께 반환한다.
   최종 해석/확정은 LLM(Claude) 몫이며, 모든 항목은 needs_confirmation=True.
   → 잘못된 일정이 캘린더에 자동으로 박히는 사고를 막는다(느슨한 결합).

캘린더 *적재*(Google Calendar 등)는 이 모듈 책임 밖이다. Claude 가 위 결과를
Google Calendar MCP 로 넣거나 .ics 로 내보낸다.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import materials
import store

# get_schedule_events 가 다루는 이벤트 종류
_KINDS = ("assignment", "quiz", "exam")

# 과제명으로 종류를 가늠하는 휴리스틱 (라벨 용도 — 출처는 동일하게 과제 due_at)
_EXAM_RE = re.compile(r"시험|기말|중간|midterm|final|exam", re.I)
_QUIZ_RE = re.compile(r"퀴즈|quiz|쪽지", re.I)


# --- 공통 시간 파서 -----------------------------------------------------------


def _parse_dt(s: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    """ISO8601 또는 'YYYY-MM-DD' 를 tz-aware datetime(UTC)으로. 실패 시 None."""
    if not s:
        return None
    raw = s.strip()
    iso = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end_of_day and len(raw) <= 10:  # 날짜만 들어온 경우 그 날 끝까지 포함
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


# --- (1) 구조화된 일정 --------------------------------------------------------


def _assignment_kind(name: Optional[str]) -> str:
    n = name or ""
    if _EXAM_RE.search(n):
        return "exam"
    if _QUIZ_RE.search(n):
        return "quiz"
    return "assignment"


def get_schedule_events(
    start: Optional[str] = None,
    end: Optional[str] = None,
    kinds: Optional[list[str]] = None,
    course_id: Optional[int] = None,
) -> list[dict]:
    """저장소의 구조화된 마감일을 공용 일정 스키마 리스트로 반환한다.

    start/end: 'YYYY-MM-DD' 또는 ISO8601. due 기준으로 [start, end] 안의 것만.
               (생략 시 해당 경계 없음)
    kinds: ['assignment','quiz','exam'] 중 일부. 생략 시 전체.
    course_id: 지정 시 해당 강의만. 생략 시 전체 강의.
    먼저 sync_assignments 로 저장소를 채워두어야 한다(API 호출 없이 캐시만 읽음).
    """
    want = set(kinds) if kinds else set(_KINDS)
    lo = _parse_dt(start)
    hi = _parse_dt(end, end_of_day=True)

    events: list[dict] = []
    for a in store.get_stored_assignments(course_id=course_id):
        due = a.get("due_at")
        due_dt = _parse_dt(due)
        if due_dt is None:
            continue  # 마감 없는 과제는 일정에서 제외
        if lo and due_dt < lo:
            continue
        if hi and due_dt > hi:
            continue
        kind = _assignment_kind(a.get("name"))
        if kind not in want:
            continue
        events.append(
            {
                "uid": f"etl-assignment-{a['id']}",
                "type": kind,
                "course": materials.display_course_name(a.get("course_name")),
                "course_id": a.get("course_id"),
                "title": a.get("name"),
                "start": due,
                "end": None,
                "due": due,
                "url": a.get("html_url"),
                "source": "assignment",
            }
        )
    events.sort(key=lambda e: e["due"])
    return events


# --- (2) 공지 본문 일정 후보 추출 ---------------------------------------------

# 한국어 'M월 D일' / 숫자 날짜 / 시간 / 일정 관련 키워드
_DATE_KR_RE = re.compile(r"(?P<m>\d{1,2})\s*월\s*(?P<d>\d{1,2})\s*일")
_DATE_NUM_RE = re.compile(r"(?P<y>20\d{2})\s*[.\-/]\s*(?P<m>\d{1,2})\s*[.\-/]\s*(?P<d>\d{1,2})")
_TIME_RE = re.compile(r"(?P<h>\d{1,2})\s*(?::\s*(?P<mi>\d{2})|\s*시)")
_PERIOD_RE = re.compile(r"(\d{1,2})\s*교시")
_KW_RE = re.compile(
    r"시험|중간|기말|퀴즈|쪽지|마감|제출|마감일|발표|휴강|보강|특강|과제|레포트|리포트|면담|오리엔테이션|OT"
)


def _guess_iso(year: int, month: int, day: int, h: Optional[int], mi: int) -> str:
    base = f"{year:04d}-{month:02d}-{day:02d}"
    if h is None:
        return base
    return f"{base}T{h:02d}:{mi:02d}:00"


def _resolve_year(month: int, posted_at: Optional[str]) -> int:
    """공지 게시연도 기준으로 연도 추정. 게시 후 한참 이전 달이면 다음 해로 본다."""
    posted = _parse_dt(posted_at)
    if posted is None:
        return datetime.now(timezone.utc).year
    year = posted.year
    if month < posted.month and (posted.month - month) >= 6:
        year += 1
    return year


def _scan_line(line: str, posted_at: Optional[str]) -> Optional[dict]:
    """한 줄에서 날짜 후보를 찾으면 추출 정보를 반환, 없으면 None."""
    year = month = day = None
    date_text = None

    m = _DATE_NUM_RE.search(line)
    if m:
        year, month, day = int(m["y"]), int(m["m"]), int(m["d"])
        date_text = m.group(0)
    else:
        m = _DATE_KR_RE.search(line)
        if m:
            month, day = int(m["m"]), int(m["d"])
            date_text = m.group(0)
    if month is None:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if year is None:
        year = _resolve_year(month, posted_at)

    tm = _TIME_RE.search(line)
    h = mi = None
    time_text = None
    if tm:
        h = int(tm["h"])
        mi = int(tm["mi"]) if tm["mi"] else 0
        if 0 <= h <= 23:
            time_text = tm.group(0)
        else:
            h = None
    pm = _PERIOD_RE.search(line)
    if pm and time_text is None:
        time_text = pm.group(0)  # 교시는 시각 환산 불가 → LLM 에 맡김

    keywords = sorted(set(_KW_RE.findall(line)))
    return {
        "date_text": date_text,
        "time_text": time_text,
        "keywords": keywords,
        "guessed_start": _guess_iso(year, month, day, h, mi or 0),
        "matched_text": line.strip(),
    }


def extract_events_from_announcements(course_id: Optional[int] = None) -> list[dict]:
    """저장소의 공지 본문에서 일정 *후보* 를 추출해 반환한다(확정 아님).

    정규식으로 'M월 D일'·숫자 날짜·시간·일정 키워드를 가진 줄만 골라
    문맥과 함께 돌려준다. 상대표현('다음 주 화요일')·교시·맥락 해석과
    최종 확정은 호출하는 LLM(Claude) 이 한다. 모든 항목 needs_confirmation=True.

    먼저 sync_announcements 로 저장소를 채워두어야 한다(API 호출 없이 캐시만 읽음).
    """
    out: list[dict] = []
    for ann in store.get_stored_announcements(course_id=course_id):
        text = ann.get("message") or ""
        posted_at = ann.get("posted_at")
        seen: set[str] = set()
        for line in text.splitlines():
            if not line.strip():
                continue
            hit = _scan_line(line, posted_at)
            if hit is None:
                continue
            key = hit["guessed_start"] + "|" + hit["matched_text"]
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "type": "candidate",
                    "source": "announcement",
                    "course": materials.display_course_name(ann.get("course_name")),
                    "course_id": ann.get("course_id"),
                    "announcement_id": ann.get("id"),
                    "announcement_title": ann.get("title"),
                    "posted_at": posted_at,
                    "url": ann.get("url"),
                    "needs_confirmation": True,
                    **hit,
                }
            )
    return out
