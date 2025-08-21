from langgraph.graph import MessagesState
from typing import List, Annotated, Literal, Dict, Any
from operator import add, or_
from pydantic import BaseModel, Field

    
class Task(BaseModel):
    """노드 작업 단위"""
    id: str
    domain: Literal["web", "history"]
    question: str
    status: Literal["pending", "done"] = "pending"

class PlanOut(BaseModel):
    """사용자 다중 질의 메시지를 나눔"""
    tasks: List[Task] 
    reason: str

class WorkerResult(BaseModel):
    task_id: str
    domain: Literal["web", "history"]
    coverage: float = 0.0         # 질문 요구사항 만족하는지
    faithfulness: float = 0.0     # 근거 일치성/할루시
    confidence: float = 0.0       # 종합 신뢰도
    eval_notes: str = ""          # 평가 코멘트
    answer: str
    full_answer: Any = None       # 최종 답변

class JudgeOut(BaseModel):
    """답변 평가"""
    coverage: float = 0.0
    faithfulness: float = 0.0
    confidence: float = 0.0
    notes: str = "" #이유

class MainState(MessagesState):
    num_generation: int = 0 # 실행횟수
    last_user_fingerprint: str = ""# 질문 초기화 중복 방지
    agent_selection_reason: str = ""# node 선택 이유
    tasks: Annotated[Dict[str, Task], or_] = {} # node 작업 목록
    results: Annotated[List[WorkerResult], add] = [] # node 작업 결과
    