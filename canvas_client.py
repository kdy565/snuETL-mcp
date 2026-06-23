"""Canvas LMS 클라이언트 래퍼 (SNU eTL / 표준 Canvas API).

.env 에서 ETL_BASE_URL, ETL_TOKEN 을 읽어 canvasapi.Canvas 인스턴스를 생성한다.
각 헬퍼는 canvasapi 객체를 LLM 친화적인 평범한 dict 로 변환해 반환한다.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from canvasapi import Canvas
from dotenv import load_dotenv

# cron 등 cwd 가 다른 환경에서도 확실히 찾도록 모듈 옆 .env 를 명시적으로 로드
load_dotenv(Path(__file__).resolve().parent / ".env")


@lru_cache(maxsize=1)
def get_canvas() -> Canvas:
    """환경변수로 인증된 Canvas 인스턴스를 반환한다 (프로세스당 1회 생성)."""
    base_url = os.environ.get("ETL_BASE_URL")
    token = os.environ.get("ETL_TOKEN")
    if not base_url or not token:
        raise RuntimeError(
            "ETL_BASE_URL / ETL_TOKEN 환경변수가 필요합니다 (.env 확인)."
        )
    return Canvas(base_url.rstrip("/"), token)


def _get(obj: Any, *names: str) -> Any:
    """canvasapi 객체에서 첫 번째로 존재하는 속성 값을 반환 (없으면 None)."""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


# --- 변환 헬퍼 (canvasapi 객체 -> dict) ---------------------------------------


def course_to_dict(c: Any) -> dict:
    enrollments = _get(c, "enrollments") or []
    grade = None
    if enrollments:
        e = enrollments[0]
        grade = {
            "current_score": e.get("computed_current_score"),
            "final_score": e.get("computed_final_score"),
            "current_grade": e.get("computed_current_grade"),
        }
    return {
        "id": c.id,
        "name": _get(c, "name"),
        "course_code": _get(c, "course_code"),
        "term_id": _get(c, "enrollment_term_id"),
        "workflow_state": _get(c, "workflow_state"),
        "start_at": _get(c, "start_at"),
        "end_at": _get(c, "end_at"),
        "grade": grade,
    }


def assignment_to_dict(a: Any) -> dict:
    return {
        "id": a.id,
        "course_id": _get(a, "course_id"),
        "name": _get(a, "name"),
        "due_at": _get(a, "due_at"),
        "lock_at": _get(a, "lock_at"),
        "unlock_at": _get(a, "unlock_at"),
        "points_possible": _get(a, "points_possible"),
        "submission_types": _get(a, "submission_types"),
        "has_submitted": bool(_get(a, "has_submitted_submissions")),
        "html_url": _get(a, "html_url"),
        "updated_at": _get(a, "updated_at"),
    }


def announcement_to_dict(d: Any) -> dict:
    return {
        "id": d.id,
        "title": _get(d, "title"),
        "posted_at": _get(d, "posted_at"),
        "author": (_get(d, "author") or {}).get("display_name"),
        "message": _get(d, "message"),
        "html_url": _get(d, "html_url"),
    }


def file_to_dict(f: Any) -> dict:
    return {
        "id": f.id,
        "display_name": _get(f, "display_name", "filename"),
        "content_type": _get(f, "content-type", "content_type"),
        "size": _get(f, "size"),
        "url": _get(f, "url"),
        "updated_at": _get(f, "updated_at"),
    }


def module_item_to_dict(it: Any) -> dict:
    return {
        "id": _get(it, "id"),
        "title": _get(it, "title"),
        "type": _get(it, "type"),  # File | ExternalUrl | Assignment | Page | ...
        "content_id": _get(it, "content_id"),  # File 이면 파일 id
        "html_url": _get(it, "html_url"),  # eTL 내 링크
        "external_url": _get(it, "external_url"),  # ExternalUrl 이면 외부 링크
    }


def module_to_dict(m: Any) -> dict:
    return {
        "id": m.id,
        "name": _get(m, "name"),  # 예: '1주차 모듈'
        "position": _get(m, "position"),
        "items": [module_item_to_dict(it) for it in m.get_module_items()],
    }


def todo_to_dict(t: Any) -> dict:
    assignment = _get(t, "assignment") or {}
    return {
        "type": _get(t, "type"),
        "course_id": _get(t, "course_id"),
        "title": assignment.get("name") or _get(t, "title"),
        "due_at": assignment.get("due_at"),
        "points_possible": assignment.get("points_possible"),
        "html_url": _get(t, "html_url"),
    }


# --- 데이터 조회 함수 ---------------------------------------------------------


def list_courses(active_only: bool = True) -> list[dict]:
    canvas = get_canvas()
    kwargs: dict[str, Any] = {"include": ["total_scores"]}
    if active_only:
        kwargs["enrollment_state"] = "active"
    return [course_to_dict(c) for c in canvas.get_courses(**kwargs)]


def list_assignments(course_id: int) -> list[dict]:
    course = get_canvas().get_course(course_id)
    return [assignment_to_dict(a) for a in course.get_assignments()]


def list_announcements(course_id: int) -> list[dict]:
    course = get_canvas().get_course(course_id)
    topics = course.get_discussion_topics(only_announcements=True)
    return [announcement_to_dict(d) for d in topics]


def list_files(course_id: int) -> list[dict]:
    course = get_canvas().get_course(course_id)
    return [file_to_dict(f) for f in course.get_files()]


def list_modules(course_id: int) -> list[dict]:
    """강의의 주차/모듈 구조와 각 모듈의 항목을 순서대로 반환한다."""
    course = get_canvas().get_course(course_id)
    return [module_to_dict(m) for m in course.get_modules()]


def get_upcoming() -> list[dict]:
    """마감 임박 과제/할 일 목록 (Canvas to-do)."""
    canvas = get_canvas()
    return [todo_to_dict(t) for t in canvas.get_todo_items()]


def get_grades(active_only: bool = True) -> list[dict]:
    """강의별 현재 성적 요약."""
    out = []
    for c in list_courses(active_only=active_only):
        out.append(
            {
                "course_id": c["id"],
                "name": c["name"],
                "grade": c["grade"],
            }
        )
    return out


def whoami() -> dict:
    u = get_canvas().get_current_user()
    return {"id": u.id, "name": _get(u, "name"), "login_id": _get(u, "login_id")}
