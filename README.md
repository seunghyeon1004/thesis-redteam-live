# 가설 레드팀 · 라이브 검산 데모

KIC AI 에이전트 공모전 출품작(개인 참가 백승현)의 검산 코어입니다. SEC EDGAR 원공시를 실시간으로 받아 투자 가설의 사실(자사주 매입·순자사주 환원·기말 주식수, 순이익·영업현금흐름)을 코드로 검산합니다.

- LLM과 가격 데이터를 쓰지 않습니다. 파이썬 표준 라이브러리만 사용합니다.
- 기준일까지 공개된 공시만 사용합니다(시점 고정).
- 투자 권유가 아닙니다.

## Report submission gate (gate/)
A review report for a holding cannot be submitted while a broken premise from the sealed season is unresolved.
- `gate/ledger.py`: append-only hash-chained JSONL ledger. No update or delete path; editing or removing a line breaks `verify()`.
- `gate/service.py`: attach broken premises, resolve (investigate, or dismiss with a required reason and actor), submit. Rejected submits are recorded too.
- `gate/server.py`: mock submission API on 127.0.0.1 only. `submit` returns 409 with the unresolved findings; PUT/PATCH/DELETE return 405.
- `gate/scenario.py`: recorded end-to-end run on Celestica (real break) and QSR (false alarm still blocks until dismissed). `gate/ANCHOR.json` holds the ledger head hash; the commit that adds it fixes that hash in time.
- `python -m pytest gate/test_gate.py` (8 tests). Not connected to any real approval system.
