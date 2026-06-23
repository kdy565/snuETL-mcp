"""SNU eTL (Canvas) MCP 서버 — Phase 1: 읽기 전용 도구.

실행:
    .venv/bin/python server.py            # stdio 모드 (Claude Desktop 등 연결용)
    .venv/bin/mcp dev server.py           # MCP Inspector 로 디버깅
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import canvas_client as cc
import materials
import schedule
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
def list_discussions(course_id: int) -> list[dict]:
    """특정 강의(course_id)의 토론 게시판(열린게시판) 글 목록을 반환한다.

    공지(list_announcements)와 별개로, eTL '열린게시판'에 올라오는 글이 여기에 잡힌다.
    각 글의 제목·작성자·작성일·답글 수를 주며, 본문과 답글 전체는 get_discussion 으로 본다.
    """
    return cc.list_discussions(course_id)


@mcp.tool()
def get_discussion(
    course_id: int, topic_id: int, include_replies: bool = True
) -> dict:
    """토론 게시판 글 1건(topic_id)의 본문과 댓글·답글(중첩 포함)을 반환한다.

    topic_id 는 list_discussions 가 돌려준 각 글의 id 다.
    include_replies=False 면 답글 없이 본문만 빠르게 가져온다.
    """
    return cc.get_discussion(course_id, topic_id, include_replies=include_replies)


@mcp.tool()
def get_grades(active_only: bool = True) -> list[dict]:
    """강의별 현재 성적(점수) 요약을 반환한다."""
    return cc.get_grades(active_only=active_only)


@mcp.tool()
def list_files(course_id: int) -> list[dict]:
    """특정 강의(course_id)의 강의자료 파일 목록과 다운로드 URL을 반환한다."""
    return cc.list_files(course_id)


@mcp.tool()
def list_modules(course_id: int) -> list[dict]:
    """특정 강의(course_id)의 주차/모듈 구조와 각 모듈 항목을 순서대로 반환한다.

    '1주차 모듈' 같은 주차 단위로 파일·과제·외부링크가 묶여 있어,
    주차별 강의록 정리나 노션/문서 적재의 골격으로 쓴다.
    """
    return cc.list_modules(course_id)


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
    skip_video: bool = False,
    include_ext: list[str] | None = None,
    exclude_ext: list[str] | None = None,
    max_size_mb: float | None = None,
    only_file_ids: list[int] | None = None,
    exclude_file_ids: list[int] | None = None,
) -> dict:
    """강의 자료를 의예과 폴더 구조(<base>/<학기>/<과목명>/)에 맞춰 다운로드한다.

    dest_dir 로 base 를 바꿀 수 있다(생략 시 ETL_DOWNLOAD_DIR 또는 의예과 기본 경로).
    이미 있는 과목 폴더는 재사용하고, 같은 크기 파일은 건너뛴다(증분).

    어떤 파일을 받을지 취사선택하는 필터(영상 등 제외에 사용):
      - skip_video: 영상 확장자(mp4·mov·mkv 등) 제외.
      - include_ext: 이 확장자만 받음(예 ['pdf','pptx']).
      - exclude_ext: 이 확장자 제외.
      - max_size_mb: 이 용량(MB) 초과 파일 제외(대용량 영상 차단).
      - only_file_ids: 이 파일 id 만 받음(콕 집어 선택; 다른 필터 무시).
      - exclude_file_ids: 이 파일 id 제외.

    먼저 dry_run=True 로 호출하면 각 파일의 id·크기(MB)·확장자와 현재 필터
    적용 시 선택 여부(selected)를 목록으로 돌려준다 → 사용자에게 보여주고
    고르게 한 뒤, only_file_ids 등으로 실제 다운로드하면 된다.
    """
    return materials.download_course_files(
        course_id, dest_dir=dest_dir, overwrite=overwrite, dry_run=dry_run,
        skip_video=skip_video, include_ext=include_ext, exclude_ext=exclude_ext,
        max_size_mb=max_size_mb, only_file_ids=only_file_ids,
        exclude_file_ids=exclude_file_ids,
    )


@mcp.tool()
def organize_course_files(
    course_id: int,
    dest_dir: str | None = None,
    mapping: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """snuETL-mcp이 받은 강의 파일을 '강의록'/'과제물' 하위 폴더로 분류 이동한다.

    Canvas 파일 목록으로 대상만 계산하므로 사용자의 다른 작업물은 건드리지 않는다.
    규칙 기반 분류가 애매하면 mapping={파일명: '강의록'|'과제물'}로 덮어쓸 수 있다(LLM 판단).
    dry_run=True면 이동 계획만 반환한다.
    """
    return materials.organize_course_files(
        course_id, dest_dir=dest_dir, mapping=mapping, dry_run=dry_run
    )


# --- 일정 (Schedule) — 목표 1 지원 -------------------------------------------


@mcp.tool()
def get_schedule_events(
    start: str | None = None,
    end: str | None = None,
    kinds: list[str] | None = None,
    course_id: int | None = None,
) -> list[dict]:
    """저장소의 구조화된 마감일(과제 due_at 등)을 공용 일정 스키마로 반환한다.

    start/end: 'YYYY-MM-DD' 또는 ISO8601. due 기준으로 [start, end] 범위만(생략 가능).
    kinds: 'assignment'|'quiz'|'exam' 중 일부(생략 시 전체).
    course_id: 지정 시 해당 강의만(생략 시 전체).
    Canvas 가 날짜를 필드로 가진 것만 다루므로 추측이 아니라 정확한 값이다.
    공지 본문 속 일정은 extract_events_from_announcements 를 쓴다.
    먼저 sync_assignments(또는 sync_all)로 저장소를 채워두어야 한다.
    """
    return schedule.get_schedule_events(
        start=start, end=end, kinds=kinds, course_id=course_id
    )


@mcp.tool()
def extract_events_from_announcements(course_id: int | None = None) -> list[dict]:
    """공지 본문에서 일정 후보(날짜·시간·키워드+문맥)를 추출해 반환한다(확정 아님).

    정규식으로 'M월 D일'·숫자 날짜·시간·일정 키워드를 가진 줄만 골라 문맥과 함께 준다.
    상대표현·교시 등 맥락 해석과 최종 확정은 호출하는 LLM 이 한다(needs_confirmation=True).
    캘린더 적재 전 반드시 사람/LLM 확인을 거치는 것을 전제로 한다.
    먼저 sync_announcements(또는 sync_all)로 저장소를 채워두어야 한다.
    """
    return schedule.extract_events_from_announcements(course_id=course_id)


if __name__ == "__main__":
    mcp.run()
