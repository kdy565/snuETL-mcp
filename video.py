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

유튜브가 아닌 네이티브 LCMS 영상(content_type=mp4)이면 자막이 없으므로, UniPlayer
content.php(XML)에서 실제 MP4(progressive) URL을 해석해 ffmpeg로 오디오만 뽑고
Whisper(STT)로 전사한다. 전사 결과는 store/transcripts 에 캐시한다(재호출 시 즉시 반환).
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

_LCMS_BASE = "https://lcms.snu.ac.kr"
_LCMS_COMMONS = _LCMS_BASE + "/CommonsCore2/v2/contents"
_LCMS_CONTENT_PHP = _LCMS_BASE + "/viewer/ssplayer/uniplayer_support/content.php"
# lcms object storage(MP4) 다운로드는 lcms Referer 가 없으면 403.
_LCMS_HEADERS = {"Referer": "https://lcms.snu.ac.kr/", "User-Agent": "Mozilla/5.0"}
_TRANSCRIPT_DIR = Path(__file__).resolve().parent / "store" / "transcripts"
_DEFAULT_WHISPER_MODEL = os.environ.get(
    "ETL_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"
)
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


# --- 네이티브 LCMS 영상 → ffmpeg + Whisper STT 폴백 ---------------------------


def _lcms_media_url(content_id: str) -> str | None:
    """네이티브 LCMS 영상의 실제 MP4(progressive) URL 을 해석한다.

    UniPlayer content.php(XML)에서 main_media(파일명)와 progressive media_uri
    템플릿([MEDIA_FILE] 치환)을 뽑는다. 이 URL 다운로드 시 lcms Referer 가 필요하다.
    """
    try:
        xml = requests.get(
            _LCMS_CONTENT_PHP, params={"content_id": content_id},
            headers=_LCMS_HEADERS, timeout=20,
        ).text
    except Exception:
        return None
    mm = re.search(r"<main_media\b[^>]*>([^<]+)</main_media>", xml)
    tmpl = re.search(
        r'<media_uri\b[^>]*method="progressive"[^>]*target="all"[^>]*>([^<]+)</media_uri>',
        xml,
    )
    if not (mm and tmpl):
        return None
    return tmpl.group(1).strip().replace("[MEDIA_FILE]", mm.group(1).strip())


def _extract_audio(media_url: str, wav_path: Path) -> None:
    """ffmpeg 로 원격 MP4(lcms Referer 필요)에서 16kHz mono WAV 오디오만 추출한다."""
    import subprocess

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-headers", "Referer: https://lcms.snu.ac.kr/\r\n",
        "-user_agent", "Mozilla/5.0",
        "-i", media_url, "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(wav_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _stt(wav_path: Path, language: str, model: str) -> dict:
    """로컬 WAV 를 음성인식(STT)한다. mlx-whisper(Apple Silicon) 우선, faster-whisper 폴백."""
    try:
        import mlx_whisper
    except ImportError:
        mlx_whisper = None
    if mlx_whisper is not None:
        res = mlx_whisper.transcribe(
            str(wav_path), path_or_hf_repo=model, language=language, verbose=False
        )
        return {"engine": f"mlx-whisper:{model}", "result": res}
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "STT 엔진이 없습니다. 'pip install mlx-whisper'(Apple Silicon) 또는 "
            "'pip install faster-whisper' 로 설치하세요."
        ) from e
    m = WhisperModel("large-v3", device="auto", compute_type="int8")
    segs = list(m.transcribe(str(wav_path), language=language)[0])
    return {
        "engine": "faster-whisper:large-v3",
        "result": {
            "text": " ".join(x.text for x in segs).strip(),
            "segments": [
                {"start": x.start, "end": x.end, "text": x.text} for x in segs
            ],
        },
    }


def _whisper_for_content(
    content_id: str, language: str, model: str, timestamps: bool, force: bool
) -> dict:
    """LCMS content_id 영상을 ffmpeg+Whisper 로 전사한다(store/transcripts 에 캐시)."""
    _TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    safe_model = re.sub(r"[^A-Za-z0-9._-]", "_", model)
    cache = _TRANSCRIPT_DIR / f"{content_id}.{language}.{safe_model}.json"
    from_cache = cache.exists() and not force
    if from_cache:
        data = json.loads(cache.read_text())
    else:
        media_url = _lcms_media_url(content_id)
        if not media_url:
            return {"transcript": None, "note": "LCMS 영상 미디어 URL 해석에 실패했습니다."}
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / f"{content_id}.wav"
            try:
                _extract_audio(media_url, wav)
            except Exception as e:
                return {"transcript": None, "note": f"오디오 추출(ffmpeg) 실패: {e}"}
            try:
                stt = _stt(wav, language, model)
            except RuntimeError as e:
                return {"transcript": None, "note": str(e)}
        res = stt["result"]
        segs = res.get("segments") or []
        data = {
            "engine": stt["engine"],
            "media_url": media_url,
            "language": res.get("language") or language,
            "char_count": len((res.get("text") or "").strip()),
            "segment_count": len(segs),
            "transcript": (res.get("text") or "").strip(),
            "segments": [
                {
                    "start": round(float(x.get("start", 0)), 2),
                    "end": round(float(x.get("end", 0)), 2),
                    "text": x.get("text", ""),
                }
                for x in segs
            ],
        }
        cache.write_text(json.dumps(data, ensure_ascii=False))
    out = {k: v for k, v in data.items() if k != "segments"}
    out["from_cache"] = from_cache
    out["cache_path"] = str(cache)
    if timestamps:
        out["segments"] = data.get("segments")
    return out


_LCMS_EM_RE = re.compile(r"lcms\.snu\.ac\.kr/(?:em|embed|view)/([0-9a-f]{8,})", re.I)


def _lcms_id_from_url(url: str | None) -> str | None:
    """`lcms.snu.ac.kr/em/{id}` 형태의 링크에서 LCMS content_id 를 추출한다."""
    if not url:
        return None
    m = _LCMS_EM_RE.search(url)
    return m.group(1) if m else None


def _lcms_from_module_item(course_id: int, item_id: int) -> tuple[str | None, str | None]:
    """Canvas 모듈 항목(id=item_id)의 외부링크에서 (LCMS content_id, 제목)을 찾는다.

    출결형이 아니라 ExternalUrl 로 LCMS em 링크를 직접 거는 영상(예: 출결 미반영 1강)용.
    """
    try:
        course = get_canvas().get_course(course_id)
        for m in course.get_modules():
            for it in m.get_module_items():
                if getattr(it, "id", None) == item_id:
                    lid = _lcms_id_from_url(getattr(it, "external_url", "") or "")
                    return lid, getattr(it, "title", None)
    except Exception:
        pass
    return None, None


def _lcms_transcript(
    lcms_id: str | None, languages: list[str], timestamps: bool,
    stt: bool, whisper_model: str | None, force: bool, meta: dict,
) -> dict:
    """LCMS content_id 영상의 대본을 만든다: 유튜브 출처면 자막, 네이티브면 Whisper STT."""
    meta["lcms_content_id"] = lcms_id
    cc = _commons_content(lcms_id) if lcms_id else None
    url = None
    if cc:
        meta["author"] = cc.get("author")
        meta["source_type"] = cc.get("type") or meta.get("source_type")
        meta["title"] = meta.get("title") or cc.get("title")
        if meta.get("duration_sec") is None:
            meta["duration_sec"] = cc.get("duration")
        url = cc.get("target_url") or cc.get("embed_url")
    meta["source_url"] = url

    vid = _youtube_id(url)
    if vid:
        meta["video_id"] = vid
        meta["transcribe_method"] = "youtube-caption"
        meta.update(_youtube_transcript(vid, languages, timestamps))
        return meta
    if not stt:
        meta["transcript"] = None
        meta["note"] = "유튜브가 아닌 LCMS 영상입니다. stt=True 로 호출하면 Whisper 로 전사합니다."
        return meta
    if not lcms_id:
        meta["transcript"] = None
        meta["note"] = "LCMS content_id 가 없어 전사할 수 없습니다."
        return meta
    meta["transcribe_method"] = "whisper-stt"
    meta.update(
        _whisper_for_content(
            lcms_id, languages[0], whisper_model or _DEFAULT_WHISPER_MODEL,
            timestamps, force,
        )
    )
    return meta


# --- 공개 함수 ----------------------------------------------------------------


def list_lecture_videos(course_id: int, resolve_source: bool = True) -> list[dict]:
    """강의의 영상 강의(lecture_attendance) 목록을 주차·차시 순으로 반환한다.

    출결형(lecture_attendance)뿐 아니라 모듈에 LCMS em 링크를 직접 건 영상(ExternalUrl,
    예: 출결 미반영 1강)도 함께 모은다. 각 항목의 id(get_lecture_transcript 에 넘김:
    출결형은 attendance_item_id, 직접링크형은 module_item_id)·제목·길이와, resolve_source=True
    면 실제 출처(youtube_url/video_id, source_type)까지 해석한다.
    """
    out: list[dict] = []
    seen: set[str] = set()

    # 1) 출결형(lecture_attendance) — LearningX 런치가 되는 과목만
    try:
        base, s, xn = _launch(course_id)
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
                if lcms_id:
                    seen.add(lcms_id)
                out.append(row)
    except RuntimeError:
        pass  # 이 과목엔 lecture_attendance 외부도구가 없음 → 직접링크형만 수집

    # 2) 모듈의 직접 LCMS em 링크(ExternalUrl/ExternalTool) — 출결형으로 안 잡힌 것만
    try:
        course = get_canvas().get_course(course_id)
        for m in course.get_modules():
            for it in m.get_module_items():
                lid = _lcms_id_from_url(getattr(it, "external_url", "") or "")
                if not lid or lid in seen:
                    continue
                seen.add(lid)
                row = {
                    "attendance_item_id": None,
                    "module_item_id": getattr(it, "id", None),
                    "week": None,
                    "lesson": None,
                    "title": getattr(it, "title", None),
                    "duration_sec": None,
                    "source_type": "lcms",
                    "youtube_url": None,
                    "video_id": None,
                    "lcms_content_id": lid,
                    "due_at": None,
                }
                if resolve_source:
                    cc = _commons_content(lid)
                    if cc:
                        url = cc.get("target_url") or cc.get("embed_url")
                        row["source_type"] = cc.get("type") or row["source_type"]
                        row["title"] = row["title"] or cc.get("title")
                        row["author"] = cc.get("author")
                        row["duration_sec"] = cc.get("duration")
                        row["youtube_url"] = url
                        row["video_id"] = _youtube_id(url)
                        row["lcms_has_caption"] = cc.get("exists_caption") == "Y"
                out.append(row)
    except Exception:
        pass
    return out


def get_lecture_transcript(
    course_id: int,
    item_id: int,
    languages: list[str] | None = None,
    timestamps: bool = False,
    stt: bool = True,
    whisper_model: str | None = None,
    force: bool = False,
) -> dict:
    """eTL 영상 강의 1개의 대본(자막)과 메타데이터를 반환한다.

    item_id 는 list_lecture_videos 의 attendance_item_id(또는 Canvas 모듈항목 id)다.
    영상 출처가 유튜브면 유튜브 자막(자동 생성 포함)을 가져온다. 네이티브 LCMS 영상이면
    실제 MP4 에서 오디오만 뽑아 Whisper(STT)로 전사한다(stt=False 면 전사 생략).
    languages 는 선호 언어 순서(기본 ['ko','en']; STT 는 languages[0] 사용),
    timestamps=True 면 세그먼트별 시각도 함께 준다. whisper_model 로 모델 변경,
    force=True 면 캐시 무시하고 재전사한다. STT 는 수 분이 걸리며 결과는 캐시된다.
    """
    languages = languages or ["ko", "en"]
    meta: dict[str, Any] = {"course_id": course_id, "item_id": item_id}

    # 1) 출결형(lecture_attendance) 우선 — LearningX 런치 + attendance_items 해석
    cd = None
    try:
        base, s, xn = _launch(course_id)
        cd = _resolve_item(base, s, xn, course_id, item_id)
    except RuntimeError:
        cd = None
    if cd:
        icd = cd.get("item_content_data") or {}
        meta.update({
            "title": cd.get("title"),
            "week": cd.get("week_position"),
            "lesson": cd.get("lesson_position"),
            "duration_sec": icd.get("duration"),
            "view_url": icd.get("view_url"),
            "source_type": icd.get("content_type"),
        })
        return _lcms_transcript(
            icd.get("content_id"), languages, timestamps, stt, whisper_model, force, meta
        )

    # 2) 폴백: Canvas 모듈 항목(item_id=module_item_id)의 LCMS em 링크 직접 해석
    lcms_id, title = _lcms_from_module_item(course_id, item_id)
    if not lcms_id:
        raise RuntimeError(
            f"영상 항목(item_id={item_id})을 찾지 못했습니다. list_lecture_videos 로 id 를 확인하세요."
        )
    meta["title"] = title
    return _lcms_transcript(
        lcms_id, languages, timestamps, stt, whisper_model, force, meta
    )


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


def get_lcms_transcript(
    em_url_or_id: str,
    languages: list[str] | None = None,
    timestamps: bool = False,
    stt: bool = True,
    whisper_model: str | None = None,
    force: bool = False,
) -> dict:
    """LCMS 영상(lcms.snu.ac.kr/em/{id} 링크 또는 content_id)의 대본을 반환한다(범용).

    eTL 강의 항목이 아니라 공지·모듈 본문에 박힌 LCMS em 링크를 바로 처리할 때 쓴다.
    유튜브 출처면 자막을, 네이티브 MP4면 ffmpeg+Whisper STT 로 전사한다(stt/force/whisper_model
    동작은 get_lecture_transcript 과 동일). 결과는 store/transcripts 에 캐시된다.
    """
    languages = languages or ["ko", "en"]
    s = (em_url_or_id or "").strip()
    lid = _lcms_id_from_url(s) or (s if re.fullmatch(r"[0-9a-f]{8,}", s) else None)
    if not lid:
        raise RuntimeError(f"LCMS content_id 를 추출하지 못했습니다: {em_url_or_id}")
    return _lcms_transcript(
        lid, languages, timestamps, stt, whisper_model, force,
        {"lcms_em_url": f"{_LCMS_BASE}/em/{lid}"},
    )
