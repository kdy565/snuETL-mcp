# snuETL-mcp — MCP 서버 설계

## 0. 목표

- **주 목표 1:** 과제/시험 일정을 캘린더에 자동 추가
- **주 목표 2:** 강의록(강의자료)을 자동으로 정리
- **부가:** 과제 제출 등 쓰기 기능도 MCP 도구로 보유 (당장 안 써도 유지)

## 1. 설계 원칙

1. **단일 책임:** 이 MCP는 *"eTL(Canvas)과 대화하는 레이어"* 만 담당한다.
   캘린더 적재·노트 요약 같은 상위 목표는 이 MCP의 도구를 **조합**해서 달성한다
   (Claude 오케스트레이션 또는 별도 스크립트/스케줄러).
2. **읽기/쓰기 분리:** 쓰기(제출·메시지·캘린더 생성) 도구는 환경변수
   `ETL_ENABLE_WRITE=1` 일 때만 등록 → 실수 방지.
3. **LLM 친화 출력:** canvasapi 객체는 평범한 dict 로 변환, 불필요한 필드 제거.
4. **정규화 레이어:** 일정/자료는 Canvas raw 가 아니라 공용 스키마로 변환해
   캘린더·파일 적재가 쉽도록 한다 (Transform 단계).
5. **캐싱(후속):** 반복 호출·rate limit 대비 SQLite 캐시 레이어를 끼울 수 있게
   client 와 transform 을 분리해 둔다.

## 2. 모듈 구조

```
snuETL-mcp/
├── .env                     # ETL_BASE_URL, ETL_TOKEN, ETL_ENABLE_WRITE
├── server.py                # FastMCP 진입점 — 도구/리소스/프롬프트 등록만
├── etl/
│   ├── client.py            # Canvas 인증 + raw 조회 (현 canvas_client.py 이전)
│   ├── transform.py         # raw -> 정규화 dict (schedule event, material 등)
│   ├── schedule.py          # 일정 추출 + .ics 내보내기 (목표 1)
│   ├── materials.py         # 자료 목록/다운로드/정리 (목표 2)
│   └── write.py             # 제출/메시지/캘린더 생성 (쓰기, 기본 비활성)
└── cache/                   # (후속) SQLite 캐시
```

> 지금은 `canvas_client.py` 하나에 들어있음 → 위 구조로 점진 분리.

## 3. 도구(Tool) 카탈로그

### A. 조회 (Read) — 이미 일부 구현됨
| 도구 | 설명 | 상태 |
|------|------|------|
| `whoami` | 토큰 소유자 | ✅ |
| `list_courses(active_only)` | 수강 강의 | ✅ |
| `list_assignments(course_id)` | 과제+마감 | ✅ |
| `get_upcoming` | 마감임박 To-Do | ✅ |
| `list_announcements(course_id)` | 공지 | ✅ |
| `get_grades(active_only)` | 성적 요약 | ✅ |
| `list_files(course_id)` | 파일 목록 | ✅ |
| `get_syllabus(course_id)` | 강의계획서 | ➕ 추가 |
| `list_modules(course_id)` | 주차별 구조 | ➕ 추가 |
| `list_lecture_videos(course_id)` | 영상 강의 목록(+유튜브 출처 해석) | ✅ |
| `get_lecture_transcript(course_id, item_id)` | 영상 강의 대본(유튜브 자막) | ✅ |
| `get_youtube_transcript(url)` | 공개 유튜브 링크 대본(범용) | ✅ |
| `list_quizzes(course_id)` | 퀴즈/시험 | ➕ 추가 |
| `get_submissions(course_id)` | 내 점수·피드백 | ➕ 추가 |
| `list_messages` | Inbox 대화 | ➕ 추가 |

### B. 일정 (Schedule) — 목표 1 지원
| 도구 | 설명 |
|------|------|
| `get_schedule_events(start, end, kinds)` | 과제 마감·퀴즈·캘린더 이벤트를 **정규화 스키마**로 통합 반환 |
| `export_ics(dest_path, start, end)` | 위 일정을 `.ics` 파일로 내보내기 (캘린더 구독/가져오기용) |

정규화 일정 스키마:
```json
{ "uid": "etl-assignment-12345",
  "type": "assignment|quiz|exam|event",
  "course": "2025-2 생물학 (001)",
  "title": "...", "start": "ISO8601", "end": "ISO8601|null",
  "due": "ISO8601|null", "url": "https://myetl.snu.ac.kr/..." }
```
> 캘린더 **적재**(Google Calendar 등)는 이 MCP 밖에서: ① Claude 가
> `get_schedule_events` 결과를 Google Calendar MCP 로 넣거나, ② `export_ics`
> 산출물을 캘린더에 구독. 둘 다 이 MCP 의 책임 밖(=느슨한 결합).

### C. 강의록 (Materials) — 목표 2 지원
| 도구 | 설명 |
|------|------|
| `list_materials(course_id)` | 파일 + 모듈 항목을 주차/모듈 기준으로 묶어 반환 |
| `download_material(file_id, dest)` | 단일 파일 다운로드 |
| `download_course_materials(course_id, dest_dir)` | 강의 전체 자료를 모듈/주차 폴더로 정리 다운로드 |
| `get_file_text(file_id)` | PDF/문서 텍스트 추출 → LLM 요약·정리 입력용 |

### D. 쓰기 (Write) — `ETL_ENABLE_WRITE=1` 일 때만
| 도구 | 설명 |
|------|------|
| `submit_assignment(course_id, assignment_id, ...)` | 과제 제출(파일/텍스트/URL) |
| `post_discussion / reply_discussion` | 토론 작성 |
| `send_message(recipients, body)` | 메시지 발송 |
| `create_calendar_event(...)` | 개인 캘린더 이벤트 생성 |

## 4. 리소스 & 프롬프트 (MCP 확장 프리미티브)

- **Resources** (읽기 전용 URI 노출 — 앱/LLM이 첨부처럼 사용):
  - `etl://courses`
  - `etl://course/{id}/syllabus`
  - `etl://course/{id}/file/{file_id}`
- **Prompts** (재사용 프롬프트 템플릿):
  - `summarize_lecture(course_id, file_id)` — 강의록 요약/정리
  - `weekly_plan(start, end)` — 주간 과제·시험 계획표

## 5. 공통 사항(cross-cutting)

- **인증:** `.env` 의 `ETL_BASE_URL`, `ETL_TOKEN` (개인 액세스 토큰).
- **페이지네이션:** canvasapi `PaginatedList` 자동 순회 (per_page 조정).
- **에러:** 403/404 를 사람이 읽을 메시지로 래핑 (예: "이 강의는 퀴즈 미사용").
- **권한 스캔 결과(2026-06-02):** 학생 토큰으로 조회 대부분 200,
  관리자(analytics/accounts)만 차단. pages/quizzes 는 강의별 사용 여부에 따라 404.

## 6. 단계 계획

- **현재:** Phase 1 읽기 7도구 완료.
- **Phase 1.5:** 조회 도구 보강(syllabus/modules/quizzes/submissions/messages) + 모듈 구조 분리.
- **Phase 2 (목표 1):** `get_schedule_events` + `export_ics` → 캘린더 연동.
- **Phase 3 (목표 2):** materials 다운로드/정리 + `get_file_text` + 요약 프롬프트.
- **Phase 4:** 쓰기 도구(제출 등) `ETL_ENABLE_WRITE` 게이트.
- **Phase 5:** SQLite 캐시 + 스케줄 자동 동기화.
- **Phase 6:** LearningX 고유(진도율/PIN 출석) 역분석.
