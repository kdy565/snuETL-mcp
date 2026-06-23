"""SNU eTL '열린게시판' = LearningX OpenBoard (LTI 외부도구) 클라이언트.

Canvas API 로는 안 읽히는 OpenBoard 글/댓글을, LTI 런치를 거쳐 LearningX 보드 API 로
가져온다. SNU SSO 는 불필요하다: Canvas 토큰으로 sessionless_launch 를 받아
OAuth 서명 폼을 /learningx/lti/boards 로 POST 하면 세션쿠키(xn_api_token)가 생긴다.

흐름:
  GET  /api/v1/courses/{cid}/tabs                         → '열린게시판' 외부도구 id
  GET  /api/v1/courses/{cid}/external_tools/sessionless_launch?id={tid} → launch_url
  GET  launch_url                                          → OAuth 서명 폼(HTML)
  POST /learningx/lti/boards (폼)                          → board_id + 세션쿠키
  GET  /learningx/api/v1/boards/{board}/posts?course_id=&page_index=&page_size=
       (Authorization: Bearer xn_api_token)               → 글 목록(JSON)
"""

from __future__ import annotations

import html as _html
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


def _env() -> tuple[str, str]:
    base = os.environ.get("ETL_BASE_URL")
    token = os.environ.get("ETL_TOKEN")
    if not base or not token:
        raise RuntimeError("ETL_BASE_URL / ETL_TOKEN 환경변수가 필요합니다 (.env 확인).")
    return base.rstrip("/"), token


def _strip(s: str | None) -> str:
    """게시판 본문/댓글의 HTML 을 평문으로 정리한다(<br> 은 줄바꿈)."""
    if not s:
        return ""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", s, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t).replace("﻿", "")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


class _LaunchForm(HTMLParser):
    """LTI sessionless_launch 가 돌려주는 자동제출 폼(action + hidden 필드)을 파싱."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list) -> None:
        d = dict(attrs)
        if tag == "form" and d.get("action"):
            self.action = d["action"]
        if tag == "input" and "name" in d:
            self.fields[d["name"]] = d.get("value", "")


def _openboard_tool_id(base: str, token: str, course_id: int) -> int | None:
    """강의 메뉴(tabs)에서 '열린게시판' 외부도구 id 를 찾는다(과목마다 다를 수 있음)."""
    r = requests.get(
        f"{base}/api/v1/courses/{course_id}/tabs",
        headers={"Authorization": f"Bearer {token}"},
        params={"per_page": 100},
        timeout=20,
    )
    for t in r.json():
        if t.get("label") == "열린게시판":
            m = re.search(r"external_tools/(\d+)", t.get("html_url") or t.get("url") or "")
            if m:
                return int(m.group(1))
    return None


def _launch(course_id: int) -> tuple[str, requests.Session, str, str]:
    """OpenBoard LTI 런치를 수행하고 (base, 세션, board_id, xn_api_token) 을 반환한다."""
    base, token = _env()
    tid = _openboard_tool_id(base, token, course_id)
    if tid is None:
        raise RuntimeError("이 강의에는 '열린게시판'(OpenBoard) 메뉴가 없습니다.")
    headers = {"Authorization": f"Bearer {token}"}
    s = requests.Session()
    launch_url = s.get(
        f"{base}/api/v1/courses/{course_id}/external_tools/sessionless_launch",
        headers=headers, params={"id": tid}, timeout=20,
    ).json()["url"]
    form_html = s.get(launch_url, headers=headers, timeout=20).text
    p = _LaunchForm()
    p.feed(form_html)
    if not p.action:
        raise RuntimeError("OpenBoard 런치 폼을 파싱하지 못했습니다.")
    r = s.post(p.action, data=p.fields, allow_redirects=True, timeout=20)
    m = re.search(r"/boards/([a-f0-9]+)/", r.url)
    if not m:
        raise RuntimeError("OpenBoard 런치 실패(board_id 미확인).")
    xn = s.cookies.get("xn_api_token")
    if not xn:
        raise RuntimeError("OpenBoard 세션 토큰(xn_api_token)을 받지 못했습니다.")
    return base, s, m.group(1), xn


def _api(base: str, s: requests.Session, xn: str, path: str, params: dict) -> Any:
    r = s.get(
        f"{base}/learningx/api/v1{path}",
        headers={"Authorization": f"Bearer {xn}", "Accept": "application/json"},
        params=params, timeout=20,
    )
    return r.json()


def _author_name(a: Any) -> str | None:
    if isinstance(a, dict):
        return a.get("user_name") or a.get("user_login")
    return a


def _post_to_dict(p: dict) -> dict:
    return {
        "id": p.get("_id"),
        "title": p.get("title"),
        "author": _author_name(p.get("author")) or p.get("user_login"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "comment_count": p.get("comment_count"),
        "read_count": p.get("read_count"),
        "content": _strip(p.get("content")),
    }


def _comment_to_dict(c: dict) -> dict:
    a = c.get("author") or {}
    return {
        "author": _author_name(a) or c.get("user_login"),
        "dept": a.get("dept_name") if isinstance(a, dict) else None,
        "created_at": c.get("created_at"),
        "comment": _strip(c.get("comment")),
    }


# --- 공개 함수 ----------------------------------------------------------------


def list_openboard_posts(
    course_id: int, page_size: int = 50, page_index: int = 1
) -> dict:
    """열린게시판(OpenBoard) 글 목록을 본문과 함께 반환한다.

    보드는 멀티테넌트라 course_id 로 필터한다. page_size 만큼 한 페이지씩 준다.
    """
    base, s, board_id, xn = _launch(course_id)
    d = _api(base, s, xn, f"/boards/{board_id}/posts",
             {"course_id": course_id, "page_index": page_index, "page_size": page_size})
    return {
        "course_id": course_id,
        "board_id": board_id,
        "total_count": d.get("total_count"),
        "page_index": d.get("page_index"),
        "page_count": d.get("page_count"),
        "posts": [_post_to_dict(p) for p in (d.get("posts") or [])],
    }


def get_openboard_post(
    course_id: int, post_id: str, include_comments: bool = True
) -> dict:
    """열린게시판 글 1건의 본문과 댓글(보완 신청 명단 등)을 반환한다.

    post_id 는 list_openboard_posts 가 돌려준 각 글의 id 다.
    """
    base, s, board_id, xn = _launch(course_id)
    post = _api(base, s, xn, f"/boards/{board_id}/posts/{post_id}",
                {"course_id": course_id})
    out = _post_to_dict(post if isinstance(post, dict) else {})
    out["board_id"] = board_id
    if include_comments:
        cj = _api(base, s, xn, f"/boards/{board_id}/posts/{post_id}/comments",
                  {"course_id": course_id})
        comments = cj if isinstance(cj, list) else (cj.get("comments") or [])
        out["comments"] = [_comment_to_dict(c) for c in comments]
    return out
