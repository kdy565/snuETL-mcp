# readETL

SNU eTL(Canvas LMS)을 MCP 도구로 노출하는 Python MCP 서버. 강의·과제·공지·성적을
조회하고, 마감일/공지를 로컬에 **누적 저장**하며, 강의 자료를 의예과 폴더 구조에 맞춰
다운로드·정리한다.

## 구성

| 파일 | 역할 |
|------|------|
| `server.py` | FastMCP 진입점 — 도구 등록 |
| `canvas_client.py` | Canvas 인증 + raw 조회 → dict 변환 |
| `store.py` | SQLite 누적 저장소 (courses/announcements/assignments + change_history) |
| `materials.py` | 강의 자료 다운로드(폴더 구조 매핑) + 강의록/과제물 정리 |
| `sync_job.py` | 자동 동기화 잡 (cron 실행, 결과를 `store/sync.log`에 누적) |
| `DESIGN.md` | 설계 문서 |

## 설정

`.env` (git 제외):

```
ETL_BASE_URL=https://myetl.snu.ac.kr
ETL_TOKEN=<개인 액세스 토큰>
ETL_DOWNLOAD_DIR=/Users/.../의예과   # 강의 자료 저장 기본 경로
```

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 실행

```bash
.venv/bin/python server.py        # MCP 서버 (stdio)
.venv/bin/mcp dev server.py       # MCP Inspector 디버깅
.venv/bin/python sync_job.py      # 동기화 1회 수동 실행
```

MCP 클라이언트 등록:

```bash
claude mcp add snu-etl -- /abs/path/.venv/bin/python /abs/path/server.py
```

## 도구 (v0.1, 18개)

- **조회**: `whoami`, `list_courses`, `list_assignments`, `get_upcoming`,
  `list_announcements`, `get_grades`, `list_files`
- **누적 저장**: `sync_all`, `sync_courses`, `sync_announcements`, `sync_assignments`,
  `get_stored_courses`, `get_stored_announcements`, `get_stored_assignments`,
  `get_change_history`
- **강의 자료**: `download_course_files`(과제 첨부 포함), `organize_course_files`

## 자동 실행

`sync_job.py`를 cron으로 매시 실행하여 공지·마감 변경을 자동 누적한다:

```
0 * * * * /abs/.venv/bin/python /abs/sync_job.py >> /abs/store/cron.out 2>&1 # readETL-sync
```
