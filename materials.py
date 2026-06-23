"""강의 파일(강의록) 다운로드 — 서울대 의예과 폴더 구조에 맞춰 저장.

저장 위치:  <base>/<학기 폴더>/<과목명>/  (Canvas 내부 폴더 구조 보존)
  - base 기본값: ETL_DOWNLOAD_DIR 또는 SNU_BASE.
  - 과목명: eTL 강의명에서 앞 학기코드(2026-1)와 뒤 분반((064))을 제거.
  - 학기 폴더: 이미 존재하는 폴더가 있으면 그대로 재사용, 없으면 eTL 학기코드를
    사용자 폴더 규칙(시간순 번호)으로 변환해 생성.
이미 받은 파일(같은 크기)은 건너뛰어 증분 다운로드한다.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Optional

import canvas_client as cc

# 다운로드 기본 경로 fallback. 실제 경로는 .env 의 ETL_DOWNLOAD_DIR 로 지정한다.
SNU_BASE = os.path.expanduser(os.environ.get("ETL_DOWNLOAD_DIR", "") or "./downloads")

# eTL 학기코드 -> (사용자 폴더 번호, 라벨). eTL: 1=1학기,2=2학기,3=여름,4=겨울.
# 사용자 폴더는 시간순 번호: 1학기=1, 여름=2, 2학기=3, 겨울=4.
_TERM_MAP = {
    1: (1, "1학기"),
    2: (3, "2학기"),
    3: (2, "여름계절학기"),
    4: (4, "겨울계절학기"),
}

# eTL 강의명(접두/분반 제거 후) -> 표시용 과목명 별칭(폴더·일정·캘린더 공용).
# eTL 이 영어이거나 원하는 이름과 다를 때 여기에 추가하면 다운로드 폴더명과
# 캘린더 이벤트 과목명에 동일하게 적용된다. 키는 공백 무시·NFC 로 비교한다.
_NAME_ALIASES = {
    "Basics of Deep Learning": "딥러닝의 기초",
    "Introduction to Machine Learning": "기계학습 개론",
    "Advanced English: Exploring Film": "고급영어 영화",
}


def _safe(name: Optional[str]) -> str:
    return re.sub(r'[/\\:*?"<>|]', "_", (name or "untitled")).strip() or "untitled"


def _norm(s: Optional[str]) -> str:
    """폴더명 매칭용 정규화 (Unicode NFC + 공백 제거).

    macOS 는 한글 파일명을 NFD 로 저장하므로 NFC 로 통일해 비교한다.
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", s or ""))


def clean_course_name(name: Optional[str]) -> str:
    """eTL 강의명에서 앞 학기코드와 뒤 분반 번호를 제거한다."""
    n = re.sub(r"^\d{4}-\d+\s+", "", name or "")
    n = re.sub(r"\s*\(\d+\)\s*$", "", n)
    return n.strip()


def display_course_name(name: Optional[str]) -> str:
    """표시용 과목명을 일관되게 만든다(폴더·일정·캘린더 공용 단일 진입점).

    학기코드·분반을 떼고(clean_course_name), 영어명 등은 _NAME_ALIASES 로
    한국어(또는 사전 설정명)로 치환한다. 새 과목명을 통일하려면 _NAME_ALIASES 에
    한 줄 추가하면 다운로드 폴더와 캘린더 양쪽에 동시에 반영된다.
    """
    clean = clean_course_name(name)
    return {_norm(k): v for k, v in _NAME_ALIASES.items()}.get(_norm(clean), clean)


def _term_folder(course_name: str) -> Optional[str]:
    """eTL 강의명 접두(2025-2 등)를 사용자 학기 폴더명으로 변환한다."""
    m = re.match(r"^(\d{4})-(\d+)", course_name or "")
    if not m:
        return None
    year, code = m.group(1), int(m.group(2))
    num, label = _TERM_MAP.get(code, (code, f"{code}학기"))
    return f"{year}-{num} {label}"


def resolve_course_dir(course_name: str, base: str) -> tuple[Path, str]:
    """과목의 저장 폴더를 결정한다. (경로, 'existing'|'computed') 반환.

    base 아래에 같은 과목명 폴더가 이미 있으면 그것을 재사용한다.
    """
    base_p = Path(base).expanduser()
    clean = display_course_name(course_name)  # 학기코드·분반 제거 + 별칭 치환(공용)
    target = _norm(clean)
    tf = _term_folder(course_name)

    matches = []
    if base_p.exists():
        for term in base_p.iterdir():
            if not term.is_dir():
                continue
            for cdir in term.iterdir():
                if cdir.is_dir() and _norm(cdir.name) == target:
                    matches.append(cdir)

    if len(matches) == 1:
        return matches[0], "existing"
    if matches:  # 여러 개면 학기 폴더로 구분 (NFC 정규화 비교)
        tf_n = unicodedata.normalize("NFC", tf) if tf else None
        for m in matches:
            if tf_n and unicodedata.normalize("NFC", m.parent.name) == tf_n:
                return m, "existing"
    # 신규: 계산된 학기 폴더 사용
    if tf:
        return base_p / tf / _safe(clean), "computed"
    return base_p / _safe(clean), "computed"


# 영상 등 받기 싫을 수 있는 큰 미디어 확장자 (skip_video=True 시 제외 대상)
VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".ogv",
}


def _ext(name: str) -> str:
    """파일명에서 소문자 확장자(점 포함)를 반환한다. 없으면 ''."""
    return Path(name).suffix.lower()


def _norm_exts(exts: Optional[list[str]]) -> Optional[set[str]]:
    """확장자 목록을 정규화한다('mp4'·'.MP4' -> '.mp4'). None 이면 None."""
    if exts is None:
        return None
    out = set()
    for e in exts:
        e = (e or "").strip().lower()
        if not e:
            continue
        out.add(e if e.startswith(".") else "." + e)
    return out


def _passes_filter(
    f,
    target: Path,
    *,
    include_ext: Optional[set[str]],
    exclude_ext: Optional[set[str]],
    max_size_mb: Optional[float],
    skip_video: bool,
    only_ids: Optional[set[int]],
    exclude_ids: Optional[set[int]],
) -> bool:
    """파일이 싱크 대상에 포함되는지 판정한다(취사선택 필터).

    only_ids 가 주어지면 그 목록만이 유일한 기준이다(명시 선택 우선).
    그 외에는 exclude_ids → skip_video → include/exclude_ext → max_size 순으로 거른다.
    """
    fid = getattr(f, "id", None)
    if only_ids is not None:
        return fid in only_ids
    if exclude_ids and fid in exclude_ids:
        return False
    ext = _ext(target.name)
    if skip_video and ext in VIDEO_EXTS:
        return False
    if include_ext is not None and ext not in include_ext:
        return False
    if exclude_ext and ext in exclude_ext:
        return False
    if max_size_mb is not None:
        size = getattr(f, "size", None)
        if size is not None and size > max_size_mb * 1024 * 1024:
            return False
    return True


def _file_info(f, target: Path, root: Path, selected: bool) -> dict:
    """미리보기/리포트용 파일 메타데이터."""
    size = getattr(f, "size", None)
    try:
        rel = str(target.relative_to(root))
    except ValueError:
        rel = target.name
    return {
        "id": getattr(f, "id", None),
        "name": target.name,
        "rel_path": rel,
        "ext": _ext(target.name),
        "size_mb": round(size / 1024 / 1024, 1) if size else None,
        "selected": selected,
    }


def _course_targets(course_id: int, base: Optional[str]):
    """(course, course_name, root, how, [(file, target_path), ...]) 를 계산한다.

    다운로드와 정리(organize)가 동일한 대상 경로 계산을 공유하도록 분리.
    """
    canvas = cc.get_canvas()
    course = canvas.get_course(course_id)
    course_name = getattr(course, "name", str(course_id))
    base = base or os.environ.get("ETL_DOWNLOAD_DIR") or SNU_BASE
    root, how = resolve_course_dir(course_name, base)

    folder_map: dict[int, str] = {}
    try:
        for fol in course.get_folders():
            folder_map[fol.id] = getattr(fol, "full_name", "") or ""
    except Exception:
        pass

    targets = []
    seen_ids = set()
    for f in course.get_files():
        seen_ids.add(f.id)
        disp = _safe(getattr(f, "display_name", None) or getattr(f, "filename", f"file_{f.id}"))
        full = folder_map.get(getattr(f, "folder_id", None), "")
        rel = re.sub(r"^course files/?", "", full)
        target = (root / rel / disp) if rel else (root / disp)
        targets.append((f, target))

    # 과제 첨부파일(파일 보관함에 없는 것)도 포함 — 과제 설명의 /files/<id> 링크
    try:
        for a in course.get_assignments():
            desc = getattr(a, "description", "") or ""
            for fid in dict.fromkeys(re.findall(r"/files/(\d+)", desc)):
                fid = int(fid)
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                try:
                    f = course.get_file(fid)
                except Exception:
                    continue
                disp = _safe(
                    getattr(f, "display_name", None) or getattr(f, "filename", f"file_{fid}")
                )
                targets.append((f, root / disp))
    except Exception:
        pass

    return course, course_name, root, how, targets


def download_course_files(
    course_id: int,
    dest_dir: Optional[str] = None,
    overwrite: bool = False,
    dry_run: bool = False,
    skip_video: bool = False,
    include_ext: Optional[list[str]] = None,
    exclude_ext: Optional[list[str]] = None,
    max_size_mb: Optional[float] = None,
    only_file_ids: Optional[list[int]] = None,
    exclude_file_ids: Optional[list[int]] = None,
) -> dict:
    """강의 자료를 의예과 폴더 구조(<base>/<학기>/<과목명>/)에 맞춰 다운로드한다.

    dest_dir 로 base 를 바꿀 수 있다(생략 시 ETL_DOWNLOAD_DIR 또는 SNU_BASE).
    같은 크기 파일이 이미 있으면 건너뛴다(증분).

    취사선택(어떤 파일을 받을지) 필터:
      - skip_video: 영상 확장자(mp4·mov 등)를 제외.
      - include_ext: 이 확장자만 받는다(예 ['pdf','pptx']). '.' 유무 무관.
      - exclude_ext: 이 확장자는 제외.
      - max_size_mb: 이 용량(MB)을 넘는 파일은 제외(영상 등 대용량 차단에 유용).
      - only_file_ids: 이 파일 id 만 받는다(콕 집어 선택, 다른 필터 무시).
      - exclude_file_ids: 이 파일 id 는 제외.
    파일 id·크기는 dry_run=True 나 list_files 로 미리 확인할 수 있다.

    dry_run=True 면 다운로드 없이 결정된 경로와 파일 목록(필터 적용 결과 selected
    표시)을 반환하므로, 무엇을 받을지 사용자에게 보여주고 고르게 할 수 있다.
    """
    course, course_name, root, how, targets = _course_targets(course_id, dest_dir)

    inc = _norm_exts(include_ext)
    exc = _norm_exts(exclude_ext)
    only_ids = set(only_file_ids) if only_file_ids is not None else None
    excl_ids = set(exclude_file_ids) if exclude_file_ids else None

    selected, excluded = [], []
    for f, target in targets:
        ok = _passes_filter(
            f, target, include_ext=inc, exclude_ext=exc, max_size_mb=max_size_mb,
            skip_video=skip_video, only_ids=only_ids, exclude_ids=excl_ids,
        )
        (selected if ok else excluded).append((f, target))

    if dry_run:
        files = [_file_info(f, t, root, True) for f, t in selected]
        files += [_file_info(f, t, root, False) for f, t in excluded]
        return {
            "course": course_name, "resolved_dir": str(root), "match": how,
            "selected": len(selected), "excluded": len(excluded),
            "files": files,
        }

    downloaded, skipped, errors = [], [], []
    for f, target in selected:
        target.parent.mkdir(parents=True, exist_ok=True)
        size = getattr(f, "size", None)
        if target.exists() and not overwrite and (
            size is None or target.stat().st_size == size
        ):
            skipped.append(str(target))
            continue
        try:
            f.download(str(target))
            downloaded.append(str(target))
        except Exception as e:
            errors.append({"file": target.name, "error": str(e)})

    return {
        "course": course_name, "resolved_dir": str(root), "match": how,
        "downloaded": len(downloaded), "skipped": len(skipped),
        "excluded_by_filter": len(excluded), "errors": errors,
    }


# --- 정리 (Organize) — readETL 이 받은 파일만 분류 이동 -----------------------

# 과제물로 분류할 파일명 패턴 (그 외는 강의록)
_ASSIGN_PAT = re.compile(
    r"(hw[\s_-]?\d*|homework|assign|과제|숙제|problem[\s_-]?set|pset|template|제출|submit)",
    re.I,
)


def classify_lecture_or_assignment(name: str) -> str:
    """파일명을 '강의록' 또는 '과제물' 로 분류한다(규칙 기반)."""
    return "과제물" if _ASSIGN_PAT.search(name or "") else "강의록"


def organize_course_files(
    course_id: int,
    dest_dir: Optional[str] = None,
    mapping: Optional[dict] = None,
    dry_run: bool = False,
) -> dict:
    """readETL 이 받은 강의 파일만 '강의록'/'과제물' 하위 폴더로 분류 이동한다.

    Canvas 파일 목록으로 대상 경로를 재계산하므로 사용자의 다른 작업물(코드·venv 등)은
    절대 건드리지 않는다. mapping={파일명: 카테고리} 로 LLM 판단 결과를 덮어쓸 수 있다.
    dry_run=True 면 이동 계획만 반환한다. 이미 분류 폴더 안에 있는 파일은 건너뛴다.
    """
    mapping = mapping or {}
    _, course_name, root, _, targets = _course_targets(course_id, dest_dir)
    cats = {"강의록", "과제물"}

    planned, moved, skipped, missing = [], [], [], []
    for f, target in targets:
        if not target.exists():
            missing.append(str(target))
            continue
        # 이미 분류 폴더(강의록/과제물) 안이면 건너뜀
        if any(p.name in cats for p in target.parents):
            skipped.append(str(target))
            continue
        category = mapping.get(target.name) or classify_lecture_or_assignment(target.name)
        if category not in cats:
            category = "강의록"
        dest = root / category / target.name
        # 충돌 시 숫자 접미사
        if dest.exists() and dest.resolve() != target.resolve():
            i = 2
            while (root / category / f"{dest.stem} ({i}){dest.suffix}").exists():
                i += 1
            dest = root / category / f"{dest.stem} ({i}){dest.suffix}"
        planned.append({"from": str(target), "to": str(dest), "category": category})
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.rename(dest)
                moved.append(str(dest))
            except Exception as e:
                missing.append(f"{target} (move failed: {e})")

    return {
        "course": course_name, "root": str(root), "dry_run": dry_run,
        "lecture": sum(1 for p in planned if p["category"] == "강의록"),
        "assignment": sum(1 for p in planned if p["category"] == "과제물"),
        "skipped_already_organized": len(skipped),
        "missing_not_downloaded": len(missing),
        "plan": planned,
    }
