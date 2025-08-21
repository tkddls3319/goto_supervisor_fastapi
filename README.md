# SupervisorGraph — Command Goto 기반 병렬 에이전트 오케스트레이션

> **요약**  
> Supervisor를 **Command 패턴**으로 구현하고, 고정 Edge 대신 **command `goto`** 로 LLM이 런타임에 노드를 직접 선택·스케줄합니다. 팬아웃(병렬 실행)→팬인(수퍼바이저 합류) 구조에서 품질 점검(Judge)과 재시도 정책을 통해 안정적인 최종 결과를 생성합니다. 병렬 스트리밍을 위한 알고리즘 개발발

---

## 목차

- [배경](#배경)
- [Edge vs Command Goto](#edge-vs-command-goto)
- [SupervisorGraph 워크플로우](#supervisorgraph-워크플로우)
  - [목표](#목표)
  - [상위 아키텍처](#상위-아키텍처)
  - [도메인 워커 정의](#도메인-워커-정의)
  - [재시도 정책](#재시도-정책)
- [상태 스키마와 병합 규칙](#상태-스키마와-병합-규칙)
- [실행 수명주기 (End-to-End)](#실행-수명주기-end-to-end)ㅊ
  - [3.1 진입점: supervisor_node](#31-진입점-supervisor_node)
  - [3.2 워커: web_research_node](#32-워커-web_research_node)
  - [3.3 워커: history_resarch_node](#33-워커-history_resarch_node)
  - [3.4 감독(재시도): supervisor_node](#34-감독재시도-supervisor_node)
  - [3.5 최종 편집자: final_answer_node](#35-최종-편집자-final_answer_node)
- [품질 평가(Judge)](#품질-평가judge)
- [확장 포인트](#확장-포인트)
- [설치 & 실행 예시](#설치--실행-예시)
  - [서버 실행](#서버-실행)
- [병렬 스트리밍 출력 병합 알고리즘](#병렬-스트리밍-출력-병합-알고리즘-langgraph-astream_events-기반)

---

## 배경

아이디어는 이렇다. 과거에 **JobQueue를 Command 패턴**으로 구현한 경험을 바탕으로, Supervisor node를 Command 패턴으로 만들면 좋겠다고 판단했다. 고정 **edge 그래프**보다 **유연한 command `goto`** 를 사용해 **LLM이 직접 노드를 선택**하도록 하여 더 에이전트에 가까운 Supervisor를 목표로 했다.

하지만 실제 구현에서는 **command goto로 팬아웃(병렬 실행) 후 다시 하나의 supervisor로 팬인**하는 구조에서 **무한 루프**가 발생할 수 있었다. 병렬 노드들을 `async` 로 만들지 않아 **`await`하지 않았고 호출이 즉시 리턴**되어 **완료 증거가 상태에 남지 않았기 때문**이다. 그 결과 실질적 진척이 없는데도 supervisor가 다시 스케줄을 돌렸다. 또한 병렬 실행 특성상 공유 state를 각 노드가 일반적인 방법으로 개별 업데이트하면, **병합 충돌**이나 **덮어쓰기** 같은 문제가 발생했다.

---

## Edge vs Command Goto

**Edge 방식**

- 흐름이 **빌드 타임에 고정**
- 조건 분기·재시도·우선순위 조정이 **번거로움**

**Command Goto 방식**

- **런타임에 다중 노드 선택(list)**, **리트라이**, **동적 경로**가 자연스러움
- 플래너/감독 **LLM이 직접 라우팅** 결정을 내림

---

## SupervisorGraph 워크플로우

### 목표

Command `goto` 패턴을 사용하여 **각 노드를 병렬 처리**하여 속도를 높이는 Supervisor를 만든다.  
하나의 사용자 질문을 **작업 단위(task)** 로 분해 → **도메인별 워커(web / history)** 처리 → **품질 평가(judge)** → 부족하면 **재실행** → **최종 편집/요약**으로 마무리.

### 상위 아키텍처

1. **Supervisor (계획·오케스트레이션)**

- 질문 분해(Plan) → **작업 큐 생성**
- 각 워커에게 라우팅
- 품질 평가 결과로 **재시도 여부 결정**
- 최종 노드 호출

2. **Workers (실행 노드)**

- `web_research_node`: **웹 검색 전용 그래프** 호출
- `history_resarch_node`: **대화 이력만**으로 답변

3. **Judge (품질 평가)**

- `coverage / faithfulness / confidence` 스코어링

4. **Finalizer (편집자)**

- 중간 답변들을 기반으로 **2–3문장 요약 + 핵심 불릿** 생성

### 도메인 워커 정의

- **web**: 공개 웹에서 **최신·검증 정보** 수집 _(DuckDuckGo + 동적 날짜 KST 주입)_
- **history**: **오직 대화 이력**만 근거로 답변 _(외부 지식 금지)_
- **3회 이내 재시도**: 품질 임계치 미달 시 관련 task만 자동 재실행

### 재시도 정책

- 각 작업은 **최대 3회** 재시도
- 임계치 미달(아래 Judge 기준) 시 해당 task만 **pending**으로 되돌려 **부분 재실행**

---

## 상태 스키마와 병합 규칙

- `MainState`는 `MessagesState`를 **상속**한다.
- `MessagesState` 자체가 `messages`를 **누적하는 리듀서(`add_messages`)** 를 보유한다.
- `tasks`에서는 **`or_` 리듀서**를 사용하여 **병렬 처리에서도 안전**하게 데이터 업데이트(머지)한다.

---

## 실행 수명주기 (End-to-End)

### 3.1 진입점: `supervisor_node`

- **재계획 필요 여부** 판단
  - 최근 사용자 메시지 → `_fingerprint_user(...)` 로 **해시 생성**
  - 이전 해시와 다르거나, 아직 `tasks`가 비어 있으면 **플래닝 수행**
- **플래닝 `_plan_tasks(...)`**
  - **입력**: 최근 메시지(질문), 이전 대화 이력
  - **출력**: `PlanOut(tasks=[...], reason=...)`
  - **분해 규칙(요약)**
    - 문장 내 **접속사/구분자** 기준으로 세분화
    - 동일 주제라도 **행위가 다르면 별도 task** (찾기 vs 요약 vs 비교 등)
- **작업 큐 생성**
  - 각 task에 **uuid 부여**, 초기 `status="pending"` 저장
- **다음 노드 결정**
  - `pending_web` / `pending_history` 유무로 각 워커 노드를 스케줄

### 3.2 워커: `web_research_node`

- **대상**: `domain="web"` + `pending` 상태 task 중 하나
- **동작**
  - **KST 기준 날짜 문자열 생성** → `WEB_PROMPT(오늘날짜)`에 주입
  - **WebGraph**(사전에 생성된 웹 에이전트 그래프)에 메시지 전달
    - `SystemMessage(content=WEB_PROMPT(오늘날짜))`
    - `HumanMessage(content=task.question)`
    - `recursion_limit = 2`
  - 응답 텍스트 추출 → **Judge 호출**
  - `WorkerResult` 작성(`coverage/faithfulness/confidence/notes/answer`)  
    → `results`에 추가, 해당 `task.status="done"`으로 패치

### 3.3 워커: `history_resarch_node`

- **대상**: `domain="history"` + `pending` 상태 task 중 하나
- **동작**
  - 현재 질문 **이전까지의 메시지**를 **History**로 간주
  - 전용 프롬프트: **외부 지식 금지**, **최신 메시지 우선**, **간결성**
  - 답변 생성 → **Judge 호출** → `WorkerResult` 누적, `task` 완료

### 3.4 감독(재시도): `supervisor_node`

- 두 워커가 모두 끝났는지 검사
- 결과 **품질 확인**
  - `weak_results` 정의
    - `coverage < MIN_COVERAGE`
    - `faithfulness < MIN_FAITHFUL`
    - `confidence < MIN_CONFIDENCE`
    - 위 **3개 중 2개 이상** 위반 시 해당 task를 **pending**으로 되돌리고 **다시 스케줄**
- **재시도 상한**: `num_generation >= 3` 이면 **강제 종료 → `final_answer_node`**

### 3.5 최종 편집자: `final_answer_node`

- **입력**: `results` 리스트(여러 워커의 중간 답변)
- **출력**: 완성된 최종 응답(요약)

---

## 품질 평가(Judge)

#### 지표 & 임계치

```python
MIN_COVERAGE   = 0.5
MIN_FAITHFUL   = 0.5
MIN_CONFIDENCE = 0.5

def is_weak(result) -> bool:
    fails = int(result["coverage"]    < MIN_COVERAGE)           + int(result["faithfulness"]< MIN_FAITHFUL)           + int(result["confidence"]  < MIN_CONFIDENCE)
    return fails >= 2
```

---

## 병렬 처리 이슈 & 대응

- **무한 루프(팬아웃→팬인)**
  - **원인**: 병렬 노드를 `async` 로 만들지 않아 호출 즉시 리턴 → **완료 상태에 남지 않음** → Supervisor가 무한한 루프
  - **해결**: 워커를 **async/await** 로 보장하고, **완료/진척 상태를 원자적으로 기록**
- **공유 상태 충돌**
  - **원인**: 병렬 노드의 개별 쓰기가 서로 **덮어쓰기/충돌**
  - **해결**: `or_` 리듀서·append-only 패턴 등 **머지 규칙** 도입

---

## 확장 포인트

- 새 도메인 추가(예: 내부 문서, 코드 실행 등)
- `Task.domain`의 `Literal[...]` 확장
- 해당 워커 노드 구현 및 `self.nodes`에 등록
- `supervisor_node` 라우팅 로직에 **스케줄링 규칙 추가**
- `_plan_tasks` 프롬프트의 **도메인 분기 규칙 업데이트**

---

### 서버 실행

```bash
uvicorn api_supervisor_gateway:app --host 0.0.0.0 --port 9999
```

---

## 병렬 스트리밍 출력 병합 알고리즘 (LangGraph `astream_events` 기반)

> **문제**: 병렬로 실행되는 여러 노드(워커/파이널)에서 **실시간 토큰 스트림**이 동시에 도착하면, 단일 텍스트 UI에서 문장이 섞여 가독성이 떨어지게됩니다.  
> **해결**: 스트림 이벤트를 **역할 기반으로 분류**하고, **단일 활성 원칙**(한 번에 한 인스턴스만 화면에 직접 출력)으로 최종 답변이 연구 로그 중간에 끼어들지 않도록 **결정적 순서**로 병합합니다.

### 구성 상수

```python
FINAL = "[final_answer_node/final_answer_node]"
WORKER_WHITELIST = {
    "[web_research_node/agent]",
    "[history_resarch_node/history_resarch_node]",
}
```

### 핵심 상태 변수

```text
active_id          : 현재 화면에 직접 출력 중인 인스턴스 (노드 실행 인스턴스 키)
final_started      : 최종 노드 출력이 시작됐는지 여부
final_seen         : 최종 노드 토큰을 받기 시작했는지
final_id           : 최종 노드 인스턴스 식별자 (지연 시작을 위해 기억)
inflight_by_type   : {워커 타입 → 진행 중인 인스턴스 id 집합}
seen_worker_types  : 관측된 워커 타입 집합 (조용함 판단에 사용)
buffers            : {인스턴스 id → 토큰 버퍼(리스트)}
order_ids          : 인스턴스 등장 순서 기록 (버퍼 드레인 순서 보장)
idrole             : {인스턴스 id → "worker" | "final"}
```

### 핵심 함수 (개념적 의사코드)

```python
def _flush(iid, newline=False):
    s = "".join(buffers[iid]); buffers[iid].clear()
    return (s + ("\n--------------------------------\n" if newline else "")) if s or newline else ""

def _activate(iid):
    # 화면의 '말할 차례'를 iid로 넘기고, 해당 인스턴스 버퍼를 먼저 비웁니다.
    nonlocal active_id
    active_id = iid
    return _flush(iid, newline=True)

def _all_workers_quiet():
    # 관측된 모든 워커 타입에 대해 진행 중 인스턴스가 없는지 확인합니다.
    return all(len(inflight_by_type[t]) == 0 for t in seen_worker_types)

def _drain_worker_buffers():
    # FINAL 시작 전, 남은 워커 버퍼를 등장 순서대로 비워 연결합니다.
    out = []
    for iid in order_ids:
        if idrole.get(iid) == "worker":
            out.append(_activate(iid))
    return "".join(out)

def _try_start_final_now():
    # FINAL 토큰을 본 뒤, 모든 워커의 작업이 끝나면 FINAL을 활성화합니다.
    nonlocal final_started
    if final_started or not final_seen or final_id is None:
        return ""
    if not _all_workers_quiet():
        return ""
    out = _drain_worker_buffers()
    out += _activate(final_id)
    final_started = True
    return out
```

### 이벤트 처리 흐름

#### `on_chat_model_stream` (토큰 도착시)

1. 메타데이터에서 `pretty = "[{label}/{node}]"`를 만들고 **역할 분류**

   - `pretty == FINAL` → `role="final"`
   - `pretty ∈ WORKER_WHITELIST` → `role="worker"`
   - 그 외 → 무시(로그/툴 등 기타 이벤트)

2. **FINAL 토큰**

   - `final_seen = True` 로 표식 → `_try_start_final_now()` 호출
   - 조건 충족 시: 남은 워커 버퍼 드레인 → **FINAL 활성화** → 이후 토큰 **즉시 출력**
   - 미충족 시: FINAL 토큰을 **버퍼에 적재**(워커가 끝날 때까지 대기)

3. **WORKER 토큰**
   - `seen_worker_types.add(pretty)` + `inflight_by_type[pretty].add(iid)`
   - `final_started`가 이미 True면 **무시** (FINAL 이후 연구 로그가 끼어들지 않게)
   - `active_id`가 `None` 또는 동일 `iid`이면:
     - `active_id is None`일 때 `pre = _activate(iid)`로 **구분선+버퍼 선방출**
     - 이어서 **토큰 바로 출력**
   - 그 외(다른 워커가 활성 상태): **버퍼에 적재**

#### `on_chat_model_end` (노드 종료시)

- **FINAL 종료**: 현재 활성이라면 개행 한 번 출력 후 종료
- **WORKER 종료**:
  - `inflight_by_type[pretty].remove(iid)` 로 **진행 중 집합에서 제거**
  - `final_started` 전이라면, 현재 활성 워커라면 개행 후 비활성화
  - 매 종료 시점마다 `_try_start_final_now()`를 호출해 **조건 충족 즉시 FINAL 시작**

### 출력 예시

```text
--------------------------------
[web_research_node/agent] ...워커1 토큰...
(워커1 종료)

--------------------------------
[history_resarch_node/history_resarch_node] ...워커2 토큰...
(워커2 종료)

--------------------------------
[final_answer_node/final_answer_node] ...버퍼되었던 앞부분 + 이후 실시간 토큰...
(파이널 종료)
```
