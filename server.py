"""SNU eTL (Canvas) MCP 서버 — Phase 1: 읽기 전용 도구.

실행:
    .venv/bin/python server.py            # stdio 모드 (Claude Desktop 등 연결용)
    .venv/bin/mcp dev server.py           # MCP Inspector 로 디버깅
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import canvas_client as cc
import materials
import store

mcp = FastMCP("snu-etl")


@mcp.tool()
def list_courses(active_only: bool = True) -> list[dict]:
    """수강 중인 강의 목록을 반환한다. active_only=False 면 지난 학기 포함."""
    return cc.list_courses(active_only=active_only)


@mcp.tool()
def list_assignments(course_id: int) -> list[dict]:
    """특정 강의(course_id)의 과제 목록과 마감일을 반환한다."""
    return cc.list_assignments(course_id)


@mcp.tool()
def get_upcoming() -> list[dict]:
    """마감이 다가오는 과제·할 일(To-Do) 목록을 반환한다 (전체 강의 통합)."""
    return cc.get_upcoming()


@mcp.tool()
def list_announcements(course_id: int) -> list[dict]:
    """특정 강의(course_id)의 공지사항 목록을 반환한다."""
    return cc.list_announcements(course_id)


@mcp.tool()
def get_grades(active_only: bool = True) -> list[dict]:
    """강의별 현재 성적(점수) 요약을 반환한다."""
    return cc.get_grades(active_only=active_only)


@mcp.tool()
def list_files(course_id: int) -> list[dict]:
    """특정 강의(course_id)의 강의자료 파일 목록과 다운로드 URL을 반환한다."""
    return cc.list_files(course_id)


@mcp.tool()
def whoami() -> dict:
    """현재 토큰의 소유자(로그인 사용자) 정보를 반환한다."""
    return cc.whoami()


# --- 누적 저장 (Storage) — course / 공지 / 과제 통합 --------------------------


@mcp.tool()
def sync_all(active_only: bool = True) -> dict:
    """강의·공지·과제를 한 번에 eTL에서 조회해 로컬 저장소에 누적 반영한다."""
    return store.sync_all(active_only=active_only)


@mcp.tool()
def sync_courses(active_only: bool = True) -> dict:
    """수강 강의 목록을 저장소에 누적 반영한다."""
    return store.sync_courses(active_only=active_only)


@mcp.tool()
def sync_announcements(course_id: int | None = None, active_only: bool = True) -> dict:
    """공지를 저장소에 누적 반영한다(본문 텍스트 저장, 수정 시 이력 기록).

    course_id 생략 시 활성 강의 전체를 동기화한다.
    """
    return store.sync_announcements(course_id=course_id, active_only=active_only)


@mcp.tool()
def sync_assignments(course_id: int | None = None, active_only: bool = True) -> dict:
    """과제를 저장소에 누적 반영한다. 마감일이 바뀌면 변경 이력을 누적 저장한다.

    course_id 생략 시 활성 강의 전체를 동기화한다.
    """
    return store.sync_assignments(course_id=course_id, active_only=active_only)


@mcp.tool()
def get_stored_courses() -> list[dict]:
    """로컬 저장소의 강의 목록을 반환한다 (API 호출 없이 캐시 조회)."""
    return store.get_stored_courses()


@mcp.tool()
def get_stored_announcements(course_id: int | None = None) -> list[dict]:
    """로컬 저장소의 공지를 최신순으로 반환한다 (API 호출 없이 캐시 조회)."""
    return store.get_stored_announcements(course_id=course_id)


@mcp.tool()
def get_stored_assignments(course_id: int | None = None) -> list[dict]:
    """로컬 저장소의 과제를 마감일 순으로 반환한다 (API 호출 없이 캐시 조회)."""
    return store.get_stored_assignments(course_id=course_id)


@mcp.tool()
def get_change_history(
    entity_type: str | None = None,
    course_id: int | None = None,
    entity_id: int | None = None,
) -> list[dict]:
    """변경 이력을 시간순으로 반환한다.

    entity_type: 'course' | 'announcement' | 'assignment' (생략 시 전체).
    마감일 변경·공지 수정 등이 누적 기록된다.
    """
    return store.get_change_history(
        entity_type=entity_type, course_id=course_id, entity_id=entity_id
    )


# --- 강의록 (Materials) -------------------------------------------------------


@mcp.tool()
def download_course_files(
    course_id: int,
    dest_dir: str | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict:
    """강의 자료를 의예과 폴더 구조(<base>/<학기>/<과목명>/)에 맞춰 다운로드한다.

    dest_dir 로 base 를 바꿀 수 있다(생략 시 ETL_DOWNLOAD_DIR 또는 의예과 기본 경로).
    이미 있는 과목 폴더는 재사용하고, 같은 크기 파일은 건너뛴다(증분).
    dry_run=True 면 다운로드 없이 결정된 저장 경로와 파일 수만 반환한다.
    """
    return materials.download_course_files(
        course_id, dest_dir=dest_dir, overwrite=overwrite, dry_run=dry_run
    )


@mcp.tool()
def organize_course_files(
    course_id: int,
    dest_dir: str | None = None,
    mapping: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """readETL이 받은 강의 파일을 '강의록'/'과제물' 하위 폴더로 분류 이동한다.

    Canvas 파일 목록으로 대상만 계산하므로 사용자의 다른 작업물은 건드리지 않는다.
    규칙 기반 분류가 애매하면 mapping={파일명: '강의록'|'과제물'}로 덮어쓸 수 있다(LLM 판단).
    dry_run=True면 이동 계획만 반환한다.
    """
    return materials.organize_course_files(
        course_id, dest_dir=dest_dir, mapping=mapping, dry_run=dry_run
    )


if __name__ == "__main__":
    mcp.run()
