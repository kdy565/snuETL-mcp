"""SNU eTL 영상 강의(LearningX lecture_attendance) → 대본(transcript) 추출.

eTL 의 영상 강의는 Canvas 네이티브 미디어가 아니라 LearningX(유비온) 'lecture_attendance'
LTI 외부도구로 제공된다. 실제 영상은 SNU LCMS(lcms.snu.ac.kr, CommonsCore2)에 등록된
콘텐츠이며, 다수 과목의 경우 그 실체가 '공개 유튜브 영상'이다. 그래서 자막(대본)은
유튜브 자동/수동 자막에서 바로 가져온다(별도 음성인식 불필요).

체인:
  Canvas 모듈항목(ExternalTool, lecture_attendance)            ── 외부도구 id
    └ sessionless_launch + OAuth 폼 POST                       → xn_api_token 세션
        └ /learningx/api/v1/courses/{cid}/modules               → attendance_item + lcms content_id
            └ lcms.snu.ac.kr/CommonsCore2/v2/contents/{id}      (Referer 헤더 필요)
                └ target_url(youtube)                           → youtube-transcript-api → 대본

유튜브가 아닌 네이티브 LCMS 영상(content_type=video)이면 자막이 없을 수 있고, 그때는
yt-dlp + Whisper 폴백이 필요하다(현재 미구현 — note 로 안내).
"""

from __future__ import annotations

import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from canvas_client import get_canvas
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

_LCMS_COMMONS = "https://lcms.snu.ac.kr/CommonsCore2/v2/contents"
_YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^&\s]*&)*v=|embed/|v/|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)


def _env() -> tuple[str, str]:
    base = os.environ.get("ETL_BASE_URL")
    token = os.environ.get("ETL_TOKEN")
    if not base or not token:
        raise RuntimeError("ETL_BASE_URL / ETL_TOKEN 환경변수가 필요합니다 (.env 확인).")
    return base.rstrip("/"), token


def _youtube_id(url_or_id: str | None) -> str | None:
    """유튜브 URL 또는 11자 영상ID 문자열에서 영상ID를 추출한다."""
    if not url_or_id:
        return None
    s = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = _YT_ID_RE.search(s)
    return m.group(1) if m else None


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


def _lecture_tool_id(base: str, token: str, course_id: int) -> int | None:
    """강의에서 lecture_attendance LTI 외부도구 id 를 찾는다(과목마다 다를 수 있음)."""
    H = {"Authorization": f"Bearer {token}"}
    # 1) 외부도구 목록에서 lecture_attendance 탐색(빠름)
    try:
        r = requests.get(
            f"{base}/api/v1/courses/{course_id}/external_tools",
            headers=H, params={"per_page": 100}, timeout=20,
        )
        for t in r.json():
            if "lecture_attendance" in json.dumps(t, ensure_ascii=False).lower():
                return t.get("id")
    except Exception:
        pass
    # 2) 폴백: 모듈 항목 중 lecture_attendance ExternalTool 의 content_id
    try:
        course = get_canvas().get_course(course_id)
        for m in course.get_modules():
            for it in m.get_module_items():
                if "lecture_attendance" in (getattr(it, "external_url", "") or ""):
                    cid = getattr(it, "content_id", None)
                    if cid:
                        return cid
    except Exception:
        pass
    return None


def _launch(course_id: int) -> tuple[str, requests.Session, str]:
    """lecture_attendance LTI 런치를 수행하고 (base, 세션, xn_api_token) 을 반환한다."""
    base, token = _env()
    tid = _lecture_tool_id(base, token, course_id)
    if tid is None:
        raise RuntimeError("이 강의에는 영상 강의(lecture_attendance) 외부도구가 없습니다.")
    s = requests.Session()
    H = {"Authorization": f"Bearer {token}"}
    launch_url = s.get(
        f"{base}/api/v1/courses/{course_id}/external_tools/sessionless_launch",
        headers=H, params={"id": tid}, timeout=20,
    ).json()["url"]
    p = _LaunchForm()
    p.feed(s.get(launch_url, headers=H, timeout=20).text)
    if not p.action:
        raise RuntimeError("lecture_attendance 런치 폼을 파싱하지 못했습니다.")
    s.post(p.action, data=p.fields, allow_redirects=True, timeout=30)
    xn = s.cookies.get("xn_api_token")
    if not xn:
        raise RuntimeError("LearningX 세션 토큰(xn_api_token)을 받지 못했습니다.")
    return base, s, xn


def _api(base: str, s: requests.Session, xn: str, path: str, params: dict | None = None) -> Any:
    r = s.get(
        f"{base}/learningx/api/v1{path}",
        headers={"Authorization": f"Bearer {xn}", "Accept": "application/json"},
        params=params or {}, timeout=20,
    )
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def _commons_content(content_id: str) -> dict | None:
    """LCMS CommonsCore2 에서 콘텐츠 메타(영상 실 URL·자막 유무 등)를 가져온다.

    Referer 가 없으면 '시청 제한' 페이지를 주므로 myetl Referer 를 넣는다. SNU 로그인 불필요.
    """
    try:
        r = requests.get(
            f"{_LCMS_COMMONS}/{content_id}",
            headers={"Referer": "https://myetl.snu.ac.kr/"}, timeout=20,
        )
        return (r.json() or {}).get("result")
    except Exception:
        return None


def _youtube_transcript(video_id: str, languages: list[str], timestamps: bool) -> dict:
    """유튜브 영상ID 의 자막을 가져와 평문(필요시 타임스탬프 세그먼트)으로 반환한다."""
    try:
        api = YouTubeTranscriptApi()
        try:
            ft = api.fetch(video_id, languages=languages)
        except NoTranscriptFound:
            # 선호 언어가 없으면: 수동 자막 우선, 없으면 아무 자막(자동 생성 포함)
            transcripts = list(api.list(video_id))
            if not transcripts:
                raise
            chosen = next((t for t in transcripts if not t.is_generated), transcripts[0])
            ft = chosen.fetch()
        snippets = list(ft.snippets) if hasattr(ft, "snippets") else list(ft)
    except (TranscriptsDisabled, NoTranscriptFound):
        return {"transcript": None, "note": "이 영상엔 사용 가능한 자막이 없습니다 (Whisper 폴백 대상)."}
    except CouldNotRetrieveTranscript as e:
        return {"transcript": None, "note": f"자막 조회 실패: {type(e).__name__}"}

    text = re.sub(r"\s+", " ", " ".join(x.text for x in snippets)).strip()
    out = {
        "language": getattr(ft, "language_code", None),
        "is_generated": getattr(ft, "is_generated", None),
        "segment_count": len(snippets),
        "char_count": len(text),
        "transcript": text,
    }
    if timestamps:
        out["segments"] = [
            {
                "start": round(getattr(x, "start", 0.0), 2),
                "duration": round(getattr(x, "duration", 0.0), 2),
                "text": x.text,
            }
            for x in snippets
        ]
    return out


def _resolve_item(base: str, s: requests.Session, xn: str, course_id: int, item_id: int) -> dict | None:
    """item_id(출석항목 id 또는 Canvas 모듈항목 id)로 attendance content_data 를 찾는다."""
    d = _api(base, s, xn, f"/courses/{course_id}/attendance_items/{item_id}")
    if isinstance(d, dict) and d.get("item_content_data"):
        return d
    for m in (_api(base, s, xn, f"/courses/{course_id}/modules") or []):
        for it in m.get("module_items", []):
            if it.get("module_item_id") == item_id or it.get("content_id") == item_id:
                return it.get("content_data")
    return None


# --- 공개 함수 ----------------------------------------------------------------


def list_lecture_videos(course_id: int, resolve_source: bool = True) -> list[dict]:
    """강의의 영상 강의(lecture_attendance) 목록을 주차·차시 순으로 반환한다.

    각 항목의 attendance_item_id(get_lecture_transcript 에 넘기는 id)·제목·길이와,
    resolve_source=True 면 실제 영상 출처(youtube_url/video_id, source_type)까지 해석한다.
    resolve_source=False 면 LCMS 조회를 생략해 더 빠르다(출처는 LearningX 값만).
    """
    base, s, xn = _launch(course_id)
    out: list[dict] = []
    for m in (_api(base, s, xn, f"/courses/{course_id}/modules") or []):
        for it in m.get("module_items", []):
            if it.get("content_type") != "attendance_item":
                continue
            cd = it.get("content_data") or {}
            icd = cd.get("item_content_data") or {}
            lcms_id = icd.get("content_id")
            row = {
                "attendance_item_id": it.get("content_id"),
                "module_item_id": it.get("module_item_id"),
                "week": cd.get("week_position"),
                "lesson": cd.get("lesson_position"),
                "title": it.get("title") or cd.get("title"),
                "duration_sec": icd.get("duration"),
                "source_type": icd.get("content_type"),
                "youtube_url": None,
                "video_id": None,
                "lcms_content_id": lcms_id,
                "due_at": cd.get("due_at"),
            }
            if resolve_source and lcms_id:
                cc = _commons_content(lcms_id)
                if cc:
                    url = cc.get("target_url") or cc.get("embed_url")
                    row["source_type"] = cc.get("type") or row["source_type"]
                    row["author"] = cc.get("author")
                    row["youtube_url"] = url
                    row["video_id"] = _youtube_id(url)
                    row["lcms_has_caption"] = cc.get("exists_caption") == "Y"
            out.append(row)
    return out


def get_lecture_transcript(
    course_id: int,
    item_id: int,
    languages: list[str] | None = None,
    timestamps: bool = False,
) -> dict:
    """eTL 영상 강의 1개의 대본(자막)과 메타데이터를 반환한다.

    item_id 는 list_lecture_videos 의 attendance_item_id(또는 Canvas 모듈항목 id)다.
    영상 출처가 유튜브면 유튜브 자막을 가져온다(자동 생성 자막 포함). languages 는 선호
    언어 순서(기본 ['ko','en']), timestamps=True 면 세그먼트별 시작시각도 함께 준다.
    유튜브가 아니거나 자막이 없으면 transcript=None 과 note 로 사유를 알린다.
    """
    languages = languages or ["ko", "en"]
    base, s, xn = _launch(course_id)
    cd = _resolve_item(base, s, xn, course_id, item_id)
    if not cd:
        raise RuntimeError(
            f"영상 항목(item_id={item_id})을 찾지 못했습니다. list_lecture_videos 로 id 를 확인하세요."
        )
    icd = cd.get("item_content_data") or {}
    lcms_id = icd.get("content_id")
    meta: dict[str, Any] = {
        "course_id": course_id,
        "item_id": item_id,
        "title": cd.get("title"),
        "week": cd.get("week_position"),
        "lesson": cd.get("lesson_position"),
        "duration_sec": icd.get("duration"),
        "view_url": icd.get("view_url"),
        "lcms_content_id": lcms_id,
        "source_type": icd.get("content_type"),
    }
    cc = _commons_content(lcms_id) if lcms_id else None
    url = None
    if cc:
        meta["author"] = cc.get("author")
        meta["source_type"] = cc.get("type") or meta["source_type"]
        url = cc.get("target_url") or cc.get("embed_url")
    meta["source_url"] = url

    vid = _youtube_id(url)
    if not vid:
        meta["transcript"] = None
        meta["note"] = (
            "유튜브 영상이 아니거나 URL을 찾지 못했습니다. "
            "네이티브 LCMS 영상이면 yt-dlp+Whisper 폴백이 필요합니다(미구현)."
        )
        return meta
    meta["video_id"] = vid
    meta.update(_youtube_transcript(vid, languages, timestamps))
    return meta


def get_youtube_transcript(
    url: str, languages: list[str] | None = None, timestamps: bool = False
) -> dict:
    """공개 유튜브 영상의 대본(자막)을 반환한다 (교수가 직접 올린 링크 등 범용).

    url 은 유튜브 주소 또는 11자 영상ID. languages 는 선호 언어 순서(기본 ['ko','en']),
    timestamps=True 면 세그먼트별 시작시각도 함께 준다.
    """
    languages = languages or ["ko", "en"]
    vid = _youtube_id(url)
    if not vid:
        raise RuntimeError(f"유튜브 영상 ID를 추출하지 못했습니다: {url}")
    return {"video_id": vid, **_youtube_transcript(vid, languages, timestamps)}
