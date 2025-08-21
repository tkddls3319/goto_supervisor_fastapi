# CUDA_VISIBLE_DEVICES=1 uvicorn api_supervisor_gateway:app --host 0.0.0.0 --port 9508 
import sys
import os
import re
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from supervisor_graph import SupervisorGraph 
from typing import AsyncGenerator
from langchain_core.messages import HumanMessage, ToolMessage, AIMessage
from web_packet_manager import QueryReq
import uuid
import time
from collections import defaultdict
from fastapi.middleware.cors import CORSMiddleware

supervisor = SupervisorGraph()

app = FastAPI()

#cors 등록 선택 사항(front 에서 바로 접근하려면)
origins = ["http://localhost"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET","POST"],
    allow_headers=["Authorization", "Content-Type"]
    )

@app.middleware("http")
async def add_request_id_middleware(request: Request, call_next):
    start_time = time.time()
    response = None
    try:
        print(
            "[API][MIDDLE/REQ] %s %s | IP: %s ",
            request.method,
            request.url.path,
            request.client.host,
        )
        response = await call_next(request)
        return response

    finally:
        process_time = round(time.time() - start_time, 4)
        print.info(
            "[API][MIDDLE/RESP] %s %s | Status: %s | Duration: %.4fs",
            request.method,
            request.url.path,
            response.status_code if response else "500",
            process_time,
        )

@app.post("/generateStream")
async def stream_response(req: QueryReq, request: Request):
    request_id = str(uuid.uuid4())
    print("[API][REQ] %s %s | IP: %s | req: %s", request.method, request.url.path, request.client.host, req.model_dump())

    async def astream_generator() -> AsyncGenerator[str, None]:

        messages = []
        config = {
            'configurable': {
                'thread_id':  request_id
            }
        }
        for chat in req.query:
            if chat['role']=='user':
                messages.append(HumanMessage(content=chat['content']))
            elif chat['role']=='ai':
                messages.append(AIMessage(content=chat['content']))

        inputs = {
            "messages": messages, 
        }

        FINAL = "[final_answer_node/final_answer_node]"
        WORKER_WHITELIST = {
            "[web_research_node/agent]",
            "[history_resarch_node/history_resarch_node]"
        }

        def is_worker(pretty: str) -> bool:
            return pretty in WORKER_WHITELIST

        def graph_label_from_meta(meta: dict) -> str:
            ns = meta.get("langgraph_checkpoint_ns") or meta.get("checkpoint_ns") or ""
            left = ns.split("|")[0] if "|" in ns else ns
            return left.split(":")[0] if ":" in left else ""

        def instance_id_from_meta(meta: dict, pretty: str) -> str:
            """node 키값 생성"""
            ns_full = meta.get("langgraph_checkpoint_ns") or meta.get("checkpoint_ns")
            if ns_full:
                return f"{ns_full}"

        # 상태
        active_id = None                 # 현재 실시간 출력 중인 인스턴스
        final_started = False            # FINAL 활성화 여부 (시작되면 워커 무시)
        final_seen = False               # FINAL 토큰이 실제로 들어왔는지
        final_id = None                  # FINAL 인스턴스 id (뒤늦게 와도 셋)

        inflight_by_type = defaultdict(set)  # 워커가 작업 끝났는지 유무{worker_type_pretty: set(instance_id)}
        seen_worker_types = set()            # 한 번이라도 등장한 워커 타입

        buffers = defaultdict(list)      # 인스턴스별 버퍼
        order_ids = []                   # 노드 나온 순서
        idrole = {}                     # "worker"/"final"

        def _flush(iid: str, newline: bool = False) -> str:
            if buffers[iid]:
                s = "".join(buffers[iid])
                buffers[iid].clear()
            else:
                s = ""
            if newline:
                s += "\n--------------------------------\n"
            return s

        def _activate(iid: str) -> str:
            nonlocal active_id
            active_id = iid
            return _flush(iid, newline=True)

        def _all_workers_quiet() -> bool:
            """워커 타입들 작업 있는지 확인"""
            for t in seen_worker_types:
                if inflight_by_type[t]:
                    return False
            return True

        def _drain_worker_buffers() -> str:
            """final node 실행전 남은 worker중 남은 버퍼가 있는 지 있으면 출력"""
            out = []
            for iid in order_ids:
                if idrole.get(iid) != "worker":
                    continue
                _activate(iid)
            return "".join(out)

        def _try_start_final_now() -> str:
            """final node 실행해도 되는지 확인로직"""
            nonlocal final_started
            out = ""
            if final_started or not final_seen or final_id is None:
                return out
            if not _all_workers_quiet():
                return out
            out += _drain_worker_buffers()
            out += _activate(final_id)
            final_started = True
            return out

        try:
            async for event in supervisor.graph.astream_events(inputs, config, version="v2"):
                meta = event.get("metadata") or {}
                node = meta.get("langgraph_node", "") or ""
                label = graph_label_from_meta(meta) or "(unknown)"
                pretty = f"[{label}/{node}]"
                etype = event.get("event")

                if pretty == FINAL:
                    role = "final"
                elif is_worker(pretty):
                    role = "worker"
                else:
                    continue

                iid = instance_id_from_meta(meta, pretty)
                idrole.setdefault(iid, role)
                if iid not in order_ids:
                    order_ids.append(iid)

                if role == "final":
                    final_id = final_id or iid

                # --- 토큰 스트림 ---
                if etype == "on_chat_model_stream":
                    data = event.get("data", {})
                    chunk = data.get("chunk")
                    if chunk is None:
                        continue
                    piece = getattr(chunk, "content", None) or getattr(chunk, "delta", None)
                    if piece is None:
                        continue
                    piece = str(piece)

                    if role == "final":
                        final_seen = True
                        # 조건 만족 시 즉시 FINAL 시작
                        to_emit = _try_start_final_now()
                        if to_emit:
                            yield to_emit
                        if final_started and active_id == final_id:
                            # 이미 FINAL이 활성화되었다면 바로 흘려보냄
                            yield piece
                        else:
                            # FINAL 토큰은 왔지만 아직 워커가 진행 중 적재만만
                            buffers[iid].append(piece)
                        continue

                    # --- 워커 처리 ---
                    seen_worker_types.add(pretty)
                    inflight_by_type[pretty].add(iid)

                    if final_started:
                        # FINAL 시작 후엔 워커 토큰 무시 (순서 보장)
                        continue

                    if active_id in (None, iid):
                        if active_id is None:
                            pre = _activate(iid)  # 기존 버퍼 방출이 있으면 먼저 내보냄
                            if pre:
                                yield pre
                        yield piece
                    else:
                        buffers[iid].append(piece)

                # --- 스트림 종료 ---
                elif etype == "on_chat_model_end":
                    if role == "final":
                        if active_id == iid:
                            yield "\n"
                        continue

                    # 워커 종료 
                    if iid in inflight_by_type[pretty]:
                        inflight_by_type[pretty].remove(iid)

                    if final_started:
                        continue

                    if active_id == iid:
                        yield "\n"
                        active_id = None

                    # node가 끝날 때 마다 final node 실행해도 되는지 체크
                    to_emit = _try_start_final_now()
                    if to_emit:
                        yield to_emit

            print(
                "[API][RES] %s %s | IP: %s | res: %s ",
                request.method, request.url.path, request.client.host,
                supervisor.graph.get_state(config=config).values
            )
        finally:
            supervisor.graph.checkpointer.delete_thread(config["configurable"]["thread_id"])

    return StreamingResponse(astream_generator(), media_type="text/plain")
