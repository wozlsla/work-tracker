# WorkTracker 1.2

WorkTracker는 코드 구조, Git 활동, 변경 스냅샷과 위험 신호를 하나의 근거 모델로 연결하는 로컬 프로젝트 인텔리전스 도구입니다. 스캔 결과의 정본은 `report.json`이며, 대시보드·Markdown·CSV·Mermaid는 같은 데이터에서 생성됩니다.

## 핵심 기능

- Unreal C++ 클래스, 상속, 컴포넌트, 인터페이스, 입력, 델리게이트, 모듈 의존성 추출
- Git 커밋, 작업자 매핑, 파일·라인 변화, 작업 트리 추적
- Activity 커밋을 클릭한 뒤 원하는 시점에 OpenAI 분석을 실행해 요약, 호출·네트워크 흐름, 컴포넌트별 변경, 영향 범위, 잠재 위험과 검증 제안을 확인하는 PR형 구현 분석
- 도입 전에 수집된 기존 커밋은 외부 API 없이 Codex 기준 분석으로 보강하고, 이후 새 커밋만 사용자 요청 시 OpenAI API로 분석
- Git diff의 입력 바인딩, 함수 선언·구현, Server RPC, Replication, OnRep, 이벤트를 구조화하고 커밋 카드에 브랜치 문맥 표시
- 이전 스냅샷 대비 추가·수정·삭제 파일 비교 및 최대 300회 실행 이력 유지
- 담당 경계 침범, 바이너리 에셋, 공개 API, 대형 변경, 순환 의존, 결합도 핫스팟 탐지
- Tailwind Colors의 공식 OKLCH 스케일과 VS Code Material Theme의 Ocean/Palenight 색상, 조합 가능한 Standard/Material 3 표면 스타일
- Git 커밋과 별개로 두 스캔 사이의 미추적·생성 파일까지 비교하는 Snapshots, 다중 선택/정렬·위치 자동 저장 관계 그래프와 휠 확대 가능한 시스템 맵
- 전체 Git 이력을 15개 단위로 탐색하는 Previous/Next Activity 페이지네이션과 필터별 자동 첫 페이지 복귀
- JSON, Markdown, CSV, Mermaid로 손쉬운 후처리와 외부 도구 연동
- 표준 라이브러리만 사용하여 별도 Python 패키지 설치 불필요

## 빠른 시작

```powershell
cd C:\UnrealPrj\work-tracker
python -m work_tracker scan --project WarZ=C:\UnrealPrj\WarZ
python -m work_tracker serve
```

`scan`은 기본적으로 현재 브랜치의 initial commit부터 전체 커밋 이력을 수집합니다. 최근 구간만 필요할 때 `--days 14`, 명시적인 기간이 필요할 때 `--since`/`--until`, 성능을 위해 개수를 제한할 때 `--max-commits`를 추가합니다.

커밋별 AI 분석을 사용하려면 루트의 `.env`에 키를 입력합니다. 서버는 버튼을 누를 때마다 이 파일을 다시 읽으므로 키를 추가한 뒤 재시작할 필요가 없습니다.

```dotenv
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.6-terra
OPENAI_REASONING_EFFORT=medium
```

그다음 Activity에서 커밋을 펼치고 `OpenAI로 분석`을 누릅니다. 분석 결과는 해당 커밋 해시에 저장되며 재스캔 후에도 유지됩니다. amend로 커밋 해시가 바뀌면 새 커밋은 다시 분석 대기 상태가 됩니다. 다른 설정 파일을 쓰려면 `serve --env-file <path>`를 사용하세요.

이미 수집된 기준 이력을 한 번만 외부 API 없이 채우려면 `scan`에 `--backfill-existing-reviews`를 추가합니다. 결과에는 `Codex · 기존 이력 보강`으로 출처가 표시됩니다. 이 옵션을 사용한 시점의 커밋 해시만 저장되므로, 이후 생성되는 커밋은 평소처럼 `OpenAI로 분석` 버튼을 기다립니다.

기존 설정을 사용해 여러 프로젝트를 분석하려면:

```powershell
python -m work_tracker scan --config-dir examples
```

작업 중 자동 갱신:

```powershell
python -m work_tracker watch --project WarZ=C:\UnrealPrj\WarZ --interval 5
```

`watch`는 소스 파일뿐 아니라 Git의 HEAD, 브랜치 ref, index와 fetch 상태도 감지합니다. 새 커밋이나 push 관련 로컬 ref가 바뀌면 기간을 다시 계산합니다. 기존 이력 보강과 수동 OpenAI 분석은 같은 커밋 해시에 한해 유지합니다.

파이프라인 자체 검증:

```powershell
python -m work_tracker self-test
```

## 출력 구조

```text
output/
  index.html                 # 프로젝트 포트폴리오
  portfolio.json
  projects/<project>/
    index.html               # 통합 대시보드
    report.json              # 완전한 정규화 데이터
    state.json               # 다음 비교의 기준 스냅샷
    summary.md               # 공유용 브리핑
    activity.csv             # 커밋/작업자 분석
    risks.csv                # 위험과 근거/후속 행동
    architecture.mmd         # Mermaid 구조 그래프
    history.jsonl            # 실행별 요약 이력(최대 300회)
    snapshots/<run-id>.json
```

## 설정

`--config-dir`은 다음 파일을 선택적으로 읽습니다.

- `repos.yaml`: 프로젝트 이름과 로컬 경로
- `team_members.yaml`: Git author/email을 팀원과 역할에 매핑
- `areas.json`: 파일 경로 기반 팀 영역 분류
- `ownership.json`: 저장소별 담당 경로와 담당자
- `schedule.yaml`: 마일스톤과 목표
- `project_plan.md`: 프로젝트 목표 원문

기존 `examples/` 설정 형식을 그대로 지원합니다.

## 안전 기본값

- `.git`, `Binaries`, `DerivedDataCache`, `Intermediate`, `Saved`, 출력 폴더를 재귀 분석하지 않습니다.
- 심볼릭 링크와 Windows reparse point를 따라가지 않아 프로젝트 루트 밖 파일을 수집하지 않습니다.
- 자격 증명 가능성이 있는 이름과 키/인증서 파일은 내용과 경로를 보고서에 넣지 않습니다.
- 파일 수, 해시 크기, 텍스트 크기, 관계 수, Git 시간·출력에 상한을 둡니다.
- Git은 셸 없이 실행하며 네트워크 인증 프롬프트와 선택적 잠금을 비활성화합니다.
- 스캔과 대시보드 열람만으로는 소스 내용을 외부 서비스로 전송하지 않습니다. Activity의 OpenAI 분석 버튼을 직접 누른 경우에만 선택한 커밋의 메시지, 변경 파일 메타데이터와 텍스트 diff를 OpenAI API로 보냅니다. 바이너리 파일 내용과 API 키는 보내지 않습니다.
- OpenAI API 키는 서버 프로세스가 `.env`에서 읽으며 HTML, `report.json`, 브라우저 JavaScript에 포함하지 않습니다. `.env`는 Git에서 제외됩니다.
- 웹 서버는 기본적으로 `127.0.0.1`에만 바인딩하고 디렉터리 목록과 비산출물 파일 제공을 차단합니다. 원격 바인딩은 `--allow-remote`를 명시해야 합니다.
- HTML은 외부 리소스를 요청하지 않으며 CSP, `nosniff`, `no-referrer` 정책을 사용합니다.

## 주요 옵션

```powershell
python -m work_tracker scan -p C:\UnrealPrj\WarZ --since 2026-07-01 --until 2026-07-15
python -m work_tracker scan -p WarZ=C:\UnrealPrj\WarZ --no-include-working-tree --max-files 150000
python -m work_tracker scan -p WarZ=C:\UnrealPrj\WarZ --backfill-existing-reviews
python -m work_tracker serve --port 8765
```
