# 개미탕 지수

개미들이 얼마나 삶아지고 있는지 보여주는 지수. 매일 두 번 자동으로 갱신됩니다.

- `index.html` 홈페이지
- `scripts/update.py` 데이터 수집과 지수 계산 (설치할 것 없음)
- `data/latest.json` 홈페이지가 읽는 최신 값
- `data/status.json` 출처별 수집 성공 여부
- `workflow.txt` 자동 실행 설정 (GitHub에 `.github/workflows/update.yml` 로 붙여넣기)

## 올리는 순서
1. github.com 에서 새 저장소(Public)를 만든다. 이름 예: `gaemitang`
2. 저장소 화면의 "uploading an existing file"을 눌러 `index.html`, `README.md`, `data`, `scripts` 를 끌어다 놓고 Commit
3. Settings → Pages → Source 를 "GitHub Actions" 로 바꾼다
4. Actions 탭 → "set up a workflow yourself" → 파일 이름을 `update.yml` 로, 내용은 `workflow.txt` 전체를 붙여넣고 Commit
5. 1~2분 뒤 Actions 탭에 초록 체크가 뜨면 `https://아이디.github.io/gaemitang/` 에서 확인

가중치와 만점 기준은 `scripts/update.py` 맨 위 설정에서 숫자만 바꾸면 됩니다.
