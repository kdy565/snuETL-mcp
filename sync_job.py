#!/usr/bin/env python
"""자동 동기화 잡 — cron/launchd 등 스케줄러가 주기 실행한다.

store.sync_all() 을 돌리고 결과를 store/sync.log 에 JSON 한 줄로 누적 기록한다.
성공 0 / 실패 1 로 종료한다.

수동 실행:  .venv/bin/python sync_job.py
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import store

LOG = Path(__file__).resolve().parent / "store" / "sync.log"


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        result = store.sync_all()
        line = {"ts": ts, "ok": True, "result": result}
        code = 0
    except Exception as e:
        line = {"ts": ts, "ok": False, "error": str(e), "trace": traceback.format_exc()}
        code = 1

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in line.items() if k != "trace"}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
