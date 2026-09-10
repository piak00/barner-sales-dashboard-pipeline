# 매출 대시보드 파이프라인

여러 브랜드의 매출·광고 대시보드(Artifact)를 자동으로 새로고침하기 위한 스크립트 모음.
브랜드별로 폴더가 분리돼 있고, 각 폴더는 독립적으로 실행된다.

- 저장소 루트 (`refresh.py` / `template.html`) — 바르너
- [`odentic/`](odentic/) — 오덴틱 ([odentic/README.md](odentic/README.md) 참고)

## 바르너

바르너 매출 대시보드(Artifact)를 매일 자동으로 새로고침하기 위한 스크립트.

- `refresh.py` — 구글시트(링크 공유, 뷰어) 4개 탭을 CSV로 받아 집계하고 `template.html`에 데이터를 채워 `dashboard_output.html`을 생성한다.
- `template.html` — 대시보드 HTML/CSS/JS 템플릿. `__SALES_DATA_JSON__` 자리에 집계 데이터가 채워진다.

## 실행

```bash
python3 refresh.py
```

`dashboard_output.html`이 생성되면, Artifact 도구로 기존 대시보드 URL에 재배포한다.

## 매일 자동 실행

Claude Code 예약 작업(routine)이 이 저장소를 clone해서 `python3 refresh.py`를 실행한 뒤,
결과를 기존 Artifact URL에 재배포한다. 대시보드 디자인/로직을 바꿀 때는 이 저장소의
`template.html`/`refresh.py`만 수정해서 커밋하면, 다음 실행부터 자동 반영된다.
