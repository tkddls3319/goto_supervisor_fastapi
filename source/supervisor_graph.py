from __future__ import annotations

from typing import List, Dict, Any, Optional, Literal
from textwrap import dedent
import re
import uuid
from typing import List
from langgraph.types import Command
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import AnyMessage, HumanMessage, AIMessage, SystemMessage
from langchain_core.prompts import PromptTemplate
from typing import Union
from state_manager import MainState, PlanOut, WorkerResult, JudgeOut, Task
from langchain_ollama import ChatOllama
from web_graph import WebGraph, WEB_PROMPT
from zoneinfo import ZoneInfo
from datetime import datetime
class SupervisorGraph:

    def __init__(self):
        self.llm = ChatOllama(model="모델이름 or 모델경로", temperature=0.5)
        self.web_research_graph = WebGraph(self.llm).create_agent()

        self.nodes = {
            "supervisor_node": self.supervisor_node,
            "web_research_node": self.web_research_node,
            "history_research_node": self.history_research_node,
            "final_answer_node": self.final_answer_node,
        }
        self.graph = self.graph_build()
        self.KST = ZoneInfo("Asia/Seoul") # 웹에서 날짜에 사용

        # 정확도 평가 기준
        self.MIN_COVERAGE = 0.3
        self.MIN_FAITHFUL = 0.3
        self.MIN_CONFIDENCE = 0.3

    def graph_build(self):
        graph_builder = StateGraph(MainState)
        for name, node in self.nodes.items():
            graph_builder.add_node(name, node)
        graph_builder.add_edge(START, "supervisor_node")
        graph_builder.add_edge("final_answer_node", END)
        return graph_builder.compile(checkpointer=MemorySaver())

    # ---------------[Util]---------------

    def _fingerprint_user(self, messages: List[AnyMessage]) -> str:
        """최근 사용자 질문 해쉬로 변경 (질문 다시 하는거 방지)"""
        last_h = self._get_last_human_message(messages)
        if not last_h:
            return ""
        text = re.sub(r"\s+", " ", last_h.content).strip().lower()
        return f"{len(text)}:{hash(text)}"

    def _get_last_human_message(self, messages: List[AnyMessage]) -> Optional[HumanMessage]:
        return next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)

    # ---------------[Planner & Judge]---------------

    def _plan_tasks(self, messages: List[AnyMessage], history: List[AnyMessage])  -> Union[PlanOut]:
        planner_prompt = PromptTemplate.from_template(dedent("""
[분해 원칙]
1) 같은 주제/대상(node)라도 요청 행위가 다르면 반드시 별도 task로 분리한다.
   - 예: "찾아줘" vs "요약해줘" vs "비교해줘" vs "번역해줘" vs "정리해줘" 등은 각각 독립 task.
2) 한 문장에 접속사/구분자(그리고, 또, 및, vs, 비교, 요약, 번역, 분석, 정리, 검증, 추천, 변경, 생성, 업데이트, 삭제, ? ; · • ①② 등)가 있으면 각 절(클로즈)마다 task를 만든다.
3) domain은 요청 성격에 따라 지정:
   - "web": 공개 지식/뉴스/과학/일반 웹 검색
   - "history": 아래 [대화 이력]만으로 답변이 가능한 경우. 외부 검색/사내 문서 탐색을 하지 않는다.                                                          
   - 같은 domain이라도 합치지 말 것.
4) history 선택 기준:
   - [대화 이력]에 현재 질문을 해결하는 데 필요한 사실/정의/수치/링크/지시/사용자 선호가 *명시*되어 있으면 domain="history"로 지정한다.
   - [대화 이력]이 불충분하면 적절한 다른 domain을 고른다. 이때 task에 "왜 history가 부족한지"를 한 문장으로 간단히 설명하는 메모를 포함해도 된다(스키마가 허용하면).
5) 각 task.question에는 그 하위 질문만 한국어로 간결하게 넣는다(불필요한 맥락/중복 제거). 지시 대상이 생략되면 앞 문맥을 해석해 지시어를 보완한다.
6) 출력은 PlanOut 스키마를 따르며, 원문의 질문들이 2개 이상이면 항상 2개 이상 task를 내라. 

[대화 이력]:     
{history}                                                                                                                 

최근 메시지:
{messages}
        """))

        chain = planner_prompt | self.llm.with_structured_output(PlanOut)

        return chain.invoke({"messages": messages, "history":history})

    async def _ajudge_result (self, question: str, answer: str) -> JudgeOut:
        judge_prompt = PromptTemplate.from_template(dedent("""
        다음 Q/A의 품질을 0~1 사이로 평가하라.
        - coverage: 질문의 요구사항을 얼마나 충족했는가
        - faithfulness: 제시한 근거/인용과 내용이 얼마나 일치하는가(환각 낮을수록 높음)
        - confidence: 종합 신뢰도(숫자, 근거 수/명확성 반영)
        - notes: 평가 이유 짧은 설명
        Question: {question}
        Answer: {answer}
        """))
        chain = judge_prompt | self.llm.with_structured_output(JudgeOut)
        try:
            return await chain.ainvoke({"question": question, "answer": answer})
        except Exception as e:
            return JudgeOut(coverage=0.5, faithfulness=0.5, confidence=0.5, notes=f"judge timeout/fail: {e}")

    # ---------------[Worker Nodes]---------------

    async def web_research_node(self, state: MainState) -> Command[Literal["supervisor_node"]]:
        pending = next(
            (t for t in state.get("tasks", {}).values() if t.domain == "web" and t.status == "pending"),
            None,
        )
        if not pending:
            return Command(goto="supervisor_node")

        date = datetime.now(self.KST).strftime("%Y-%m-%d")
        try:
            result = await self.web_research_graph.ainvoke(
                {"messages": [SystemMessage(content=WEB_PROMPT.format(TODAY_KST=date)),
                            HumanMessage(content=pending.question)]},
                recursion_limit = 2
            )
        except Exception as e:
           result = {"messages": [AIMessage(content="탐색 동안 적절한 답변을 만들지 못 했습니다.")]}
        answer_text = result["messages"][-1].content

        judge = await self._ajudge_result(pending.question, answer_text)

        wr = WorkerResult(
            task_id=pending.id,
            domain="web",
            answer=answer_text,
            full_answer=result,
            coverage=judge.coverage,
            faithfulness=judge.faithfulness,
            confidence=judge.confidence,
            eval_notes=judge.notes,
        )
        updated_task = pending.model_copy(update={"status": "done"})  
        return Command(
            update={
                "messages": [AIMessage(content=answer_text, name="web_research_node")],
                "results": [wr],
                "tasks": {updated_task.id: updated_task},  
            },
            goto="supervisor_node"
        )
    
    async def history_research_node(self, state: MainState) -> Command[Literal["supervisor_node"]]:
        # 1) pending history task 선택
        pending = next(
            (t for t in state.get("tasks", {}).values()
            if t.domain == "history" and t.status == "pending"),
            None,
        )
        if not pending:
            return Command(goto="supervisor_node")

        msgs: List[AnyMessage] = state.get("messages", [])
        history_text = msgs[:-1]

        # 3) 프롬프트 구성(History만 사용)
        prompt = PromptTemplate.from_template(dedent("""
        당신은 아래 [History]만을 근거로 현재 질문에 답하는 어시스턴트입니다.

        규칙:
        1) 근거 제한: 외부 지식/추측 금지. 오직 [History] 내용만 사용.
        2) 충분성 판단: [History]에 정답을 구성할 필수 사실/정의/수치/링크/지시가 '명시'돼 있으면 간결하고 정확하게 답하라.
        3) 우선순위: [History]에서 서로 모순되면 '최신 메시지'를 우선으로 판단한다.
        4) 간결성: 불필요한 반복/장황함 금지. 필요한 최소 맥락만 재인용.
        5) 형식: 항목이 여럿이면 번호목록(1., 2., …)으로 정리한다.

        [History]
        {history}

        [Question]
        {question}
        """))

        chain = prompt | self.llm
        result = await chain.ainvoke({"history": history_text, "question": pending.question})

        # 4) 품질 평가 & 결과 기록
        judge = await self._ajudge_result(pending.question, result.content)

        wr = WorkerResult(
            task_id=pending.id,
            domain="history",
            answer=result.content,
            coverage=judge.coverage,
            faithfulness=judge.faithfulness,
            confidence=judge.confidence,
            eval_notes=judge.notes,
        )

        updated_task = pending.model_copy(update={"status": "done"})

        return Command(
            goto="supervisor_node",
            update={
                "messages": [AIMessage(content=result.content, name="history_research_node")],
                "results": [wr],
                "tasks": {updated_task.id: updated_task},
            },
        )

    # ---------------[Supervisor Node]---------------
    def supervisor_node(self, state: MainState) -> Command[List[Literal["web_research_node", "history_research_node", "final_answer_node"]]]:
        
        step = state.get("num_generation", 0)
        is_generationMax = step >= 3

        if is_generationMax:
            return Command(goto=["final_answer_node"], update = {"num_generation": step +1})

        # 1) 새로운 질문인지 확인하고 멀티질문 나누기
        fp = self._fingerprint_user(state["messages"])
        need_replan = (fp != state.get("last_user_fingerprint")) or (not state.get("tasks"))
        if need_replan:
            plan = self._plan_tasks( self._get_last_human_message(state["messages"]), history= state["messages"][:-1])
            print(f"plan : {plan}")
            planned_tasks: Dict[str, Task] = {}

            reason = ""
            for t in plan.tasks:
                tid = str(uuid.uuid4())[:8]
                planned_tasks[tid] = Task(
                    id=tid,
                    domain=t.domain,
                    question=t.question,
                    status="pending",
                )
                reason = plan.reason

            updates = {
                "tasks": planned_tasks,  
                "agent_selection_reason":reason,
                "last_user_fingerprint": fp,
            }
        else:
            updates = {}

        # 2) 미완료 task 확인
        tasks: Dict[str, Task] = {**state.get("tasks", {}), **updates.get("tasks", {})}
        pending_web = [t for t in tasks.values() if t.domain == "web" and t.status == "pending"]
        pending_history = [t for t in tasks.values() if t.domain == "history" and t.status == "pending"]

        print(f"pending task = web: {pending_web}, history: {pending_history}")
        # 3) 모든 task가 끝났다면 답변 품질 확인 후 종료
        if not pending_web and not pending_history:

            weak_results = [r for r in state.get("results", []) if (r.coverage < self.MIN_COVERAGE) + (r.faithfulness < self.MIN_FAITHFUL) + (r.confidence < self.MIN_CONFIDENCE) >= 2]
            task_patches: Dict[str, Task] = {}
            nxt: List[str] = []
            for r in weak_results:
                t = tasks.get(r.task_id)
                if not t:
                    continue
                # done인 애들 pending으로 전환
                if t.status != "pending":
                    new_t = t.model_copy(update={"status": "pending"})
                    task_patches[t.id] = new_t

                    if t.domain == "web":
                        nxt.append("web_research_node")
                    elif t.domain == "history":
                        nxt.append("history_research_node")

            updates["num_generation"] = step + 1

            if task_patches:
                updates["tasks"] = {**updates.get("tasks", {}), **task_patches}
                print(f"정보부족 task 재실행 : {updates}")
                return Command(goto=nxt, update=updates)

            if not nxt or is_generationMax:
                print(f"모든 답변 충족 : final_answer_node")
                return Command(goto=["final_answer_node"], update=updates)

        # 4) 아직 남은 일이 있으면 실행 ( 같은 node있으면 하나씩 )
        nxt: List[str] = []
        if pending_web:
            nxt.append("web_research_node")
        if pending_history:
            nxt.append("history_research_node")
            

        updates.update({ "num_generation": step + 1 })
        return Command(goto=nxt, update=updates)
    
    def final_answer_node(self, state: MainState):

        last_h = self._get_last_human_message(state.get("messages", []))
        results = state.get("results", []) or []
        answer = []
        for r in results:
            answer.append(AIMessage(content=f"confidence: {r.confidence} answer: {r.answer}" ))

        prompt = PromptTemplate.from_template(dedent("""
        당신은 여러 워커의 중간 답변만을 근거로 '최종 요약'을 작성하는 편집자입니다.

        규칙:
        - 제공된 중간 답변 내 정보만 사용(새로운 사실/추정 추가 금지).
        - 중복은 통합하고, 상충 시 confidence가 더 높은 정보를 우선.
        - 근거가 약한/모호한 내용은 제외.
        - 간결하고 명확한 한국어로 작성.

        출력 형식(반드시 준수):
        - 2–3문장으로 결론 요약
        핵심 포인트:
        - 3–5개 불릿으로 주요 근거/사실 정리

        [사용자 질문]
        {question}

        [중간 답변 목록]
        {snippets}

        위 정보만으로 위 형식을 지켜 최종 요약을 작성하세요.
        """))

        llm_out = (prompt | self.llm).invoke({"question": last_h, "snippets": answer})
    
        return Command(
        goto=END,
        update={"messages": [AIMessage(content=llm_out.content, name="final_answer")]}
        )