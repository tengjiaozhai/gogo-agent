"""网页预订离线练习：真实 HTTP/SDK/确认签名/SQL，固定模型与模拟供应商；不调用外部服务。

仓库根目录：uv sync --frozen
运行：PYTHONPATH=src .venv/bin/python scripts/demo_booking_chat.py
页面后端：同命令加 --serve --port 8006
前端：cd frontend && GOGO_API_PROXY_TARGET=http://127.0.0.1:8006 npm run dev -- --host 127.0.0.1 --port 5178 --strictPort
页面：http://127.0.0.1:5178 ，alice / 123456，打开“模拟预订练习”，发送“请预订这个方案的酒店”。
--scenario ok|reject|lost_after_commit|lost_before_commit；每次启动先在隔离库预置审批通过且已审核的方案。
详细断点、准备轮次和边界：docs/契约样例/预订Agent-网页完整链路调试.md
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import date
import json
import re
from types import SimpleNamespace
from tempfile import TemporaryDirectory

from agentscope.message import TextBlock, ToolCallBlock, ThinkingBlock, DataBlock
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatResponse
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gogo_agent.auth.dependencies import get_auth_service
from gogo_agent.auth.models import UserAccount
from gogo_agent.auth.repository import InMemoryUserAccountRepository
from gogo_agent.auth.router import router as auth_router
from gogo_agent.auth.security import InMemoryTokenStore, TokenManager
from gogo_agent.auth.service import AuthService
from gogo_agent.booking.service import BookingService
from gogo_agent.booking.supplier import SimulatedSupplier, SupplierCancelGateway
from gogo_agent.chat.config import ChatAgentSettings
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.repository import SQLAgentSessionStore, SQLChatHistoryRepository
from gogo_agent.chat.router import router as chat_router
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.db.base import Base
from gogo_agent.db.models import UserProfileModel
from gogo_agent.intent import IntentPipelineService, IntentRecognizer, RewriteContextBuilder
from gogo_agent.intent.rules import IntentRuleMatcher
from gogo_agent.intent.service import QueryRewriter
from gogo_agent.planning.models import PlanConditions
from gogo_agent.planning.review import ApprovedTrip
from gogo_agent.planning.service import PlanService
from gogo_agent.planning.simulated import SIMULATED_POLICY, simulated_assessors, simulated_search
from gogo_agent.planning.store import PlanRepository
from gogo_agent.travel.dependencies import get_travel_service
from gogo_agent.travel.models import AdminDecisionRequest, SubmitTravelRequest
from gogo_agent.travel.repository import BusinessRepository
from gogo_agent.travel.router import travel_router
from gogo_agent.travel.service import TravelBusinessService

SESSION_ID = "booking-demo"
QUESTION = "请预订这个方案的酒店"
TODAY = date(2026, 10, 25)
CONDITIONS = PlanConditions(origin="北京", destination="上海", departure_date=date(2026, 11, 10), return_date=date(2026, 11, 12))


class RuleOnlyRewriter(QueryRewriter):
    """离线练习仅使用真实规则，不接受会触发外部改写的模糊输入。"""
    async def rewrite(self, context):
        raise ValueError("离线练习请发送明确的预订请求，如：" + QUESTION)


@asynccontextmanager
async def pipeline(history):
    model = _FallbackMockModel()
    try:
        yield IntentPipelineService(RewriteContextBuilder(history), RuleOnlyRewriter(model),
                                    IntentRecognizer(model, rule_matcher=IntentRuleMatcher()))
    finally:
        await model.client.close()


class BookingDemoModel(_FallbackMockModel):
    """只替换模型 API；实际 AgentScope 负责主子委派、工具和保存。"""
    def __init__(self, role, plan_id, *, stream=False, delay: float = 0):
        super().__init__(stream=stream)
        self.role, self.plan_id, self.delay = role, plan_id, delay

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.call_count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        content: list[TextBlock | ToolCallBlock | ThinkingBlock | DataBlock]
        if self.call_count == 1 and self.role == "master":
            content = [ToolCallBlock(id="booking-delegate", name="booking_agent", input=json.dumps({"question": next((m.get_text_content() or QUESTION for m in reversed(messages) if m.role == "user"), QUESTION)}))]
        elif self.call_count == 1 and self.role == "booking":
            names = {item["function"]["name"] for item in (tools or [])}
            question = next((m.get_text_content() or "" for m in reversed(messages) if m.role == "user"), "")
            booking_match = re.search(r"bk_[a-f0-9]{32}", question)
            name = "check_booking_result" if booking_match and "核实" in question else "execute_confirmed_booking" if "execute_confirmed_booking" in names else "query_bookings" if "查询" in question else "prepare_booking"
            args = {} if name in {"execute_confirmed_booking", "query_bookings"} else {"plan_id": self.plan_id, "proposal_tag": "综合最佳", "leg": "hotel"}
            if name == "check_booking_result":
                assert booking_match is not None
                args = {"booking_id": booking_match.group()}
            content = [ToolCallBlock(id="booking-action", name=name, input=json.dumps(args))]
        else:
            # 故意给出不可信的成功文字，真实结果仍由服务端业务回执决定。
            content = [TextBlock(text="模型的汇总文字不能作为订单事实。")]
        response = ChatResponse(content=content, is_last=not self.stream)
        if not self.stream:
            return response
        async def chunks():
            yield response
        return chunks()


class BookingDemoExecutor(ChatAgentExecutor):
    """只替换模型与供应商装配；身份、工具、持久化和确认使用正式执行器。"""
    def __init__(self, history, state, bookings, plan_id, delay: float = 0):
        super().__init__(history, state, pipeline_factory=pipeline, settings=ChatAgentSettings(),
                         booking_service_factory=lambda: bookings)
        self.plan_id, self.delay = plan_id, delay

    def _build_model(self, stream=False, *, role="master"):
        return BookingDemoModel(role, self.plan_id, stream=stream, delay=self.delay)

    def _input_middlewares(self, request, trace) -> list[MiddlewareBase]:
        from gogo_agent.context.input import ModelInputContext
        return [ModelInputContext(request), trace] if trace is not None else [ModelInputContext(request)]

    async def recommend_questions(self, question, answer):
        return []


async def create_demo_app(scenario="ok", delay: float = 0):
    """一次性文件 SQLite；计划、业务记录、聊天历史与 AgentState 使用同一事务工厂。"""
    directory = TemporaryDirectory(prefix="gogo-booking-chat-")
    engine = create_engine(f"sqlite+pysqlite:///{directory.name}/lab.sqlite3", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    supplier = SimulatedSupplier(modes=[scenario])
    travel = TravelBusinessService(BusinessRepository(factory), booking_gateway=SupplierCancelGateway(supplier))
    with factory() as session, session.begin():
        session.add(UserProfileModel(user_id="u001", level="P6"))
    order = travel.submit_order("u001", SubmitTravelRequest(destination="上海", departure_city="北京",
        departure_date=CONDITIONS.departure_date, return_date=CONDITIONS.return_date,
        purpose="离线预订练习", confirmed=True, idempotency_key="booking-demo-order"))
    assert order.approval_id is not None
    travel.decide_approval(UserAccount(user_id="u_001", username="admin", password_hash="unused", role="ADMIN"),
                           order.approval_id, AdminDecisionRequest(decision="agree"))
    plans = PlanService(PlanRepository(factory), today=lambda: TODAY)
    plan = plans.create("u001", CONDITIONS, order_id=order.order_id, session_id=SESSION_ID)
    plans.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
    plans.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
    await plans.review("u001", plan.plan_id, proposal_tag="综合最佳", assessors=simulated_assessors(),
                       approved=ApprovedTrip(origin="北京", destination="上海"))
    bookings = BookingService(travel, plans, supplier, sleep=lambda _: None)
    history = ChatHistoryService(SQLChatHistoryRepository(factory))
    history.save_user_message(SESSION_ID, "u001", "为已审批的北京到上海差旅规划方案")
    history.save_assistant_message(SESSION_ID, "u001", "模拟预订练习：已预置本人审批通过、方案审核通过的计划。发送“请预订这个方案的酒店”开始。")
    history.update_title(SESSION_ID, "u001", "模拟预订练习")
    executor = BookingDemoExecutor(history, SQLAgentSessionStore(factory), bookings, plan.plan_id, delay)
    auth = AuthService(InMemoryUserAccountRepository(load_default_seeds=True), TokenManager(store=InMemoryTokenStore()))
    app = FastAPI(title="GoGo 模拟预订聊天练习")
    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(travel_router)
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    app.dependency_overrides[get_travel_service] = lambda: travel
    app.state.lab = SimpleNamespace(engine=engine, directory=directory, scenario=scenario, supplier=supplier, bookings=bookings, plans=plans,
                                    history=history, executor=executor, factory=factory, plan_id=plan.plan_id, order_id=order.order_id)
    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "offline-simulated-booking"}
    return app


def exercise(app):
    client = TestClient(app)
    token = client.post("/api/auth/login", json={"username": "alice", "password": "123456"}).json()["token"]
    headers = {"Authorization": token, "Accept": "application/json"}
    first = client.post(f"/api/chat/{SESSION_ID}", headers=headers, json={"message": QUESTION})
    assert first.status_code == 200, first.text
    pending = next(item["data"] for item in first.json()["timeline"] if item["kind"] == "booking_confirmation")
    assert app.state.lab.supplier.create_calls == 0
    body = {"message": "确认预订（模拟）：" + pending["title"], "booking_confirmation_token": pending["token"]}
    second = client.post(f"/api/chat/{SESSION_ID}", headers=headers, json=body)
    assert second.status_code == 200, second.text
    result = next(item["data"] for item in second.json()["timeline"] if item["kind"] == "booking")
    replay = client.post(f"/api/chat/{SESSION_ID}", headers=headers, json=body)
    assert replay.status_code == 200, replay.text
    assert app.state.lab.supplier.create_calls == 1
    print(json.dumps({"scenario": app.state.lab.scenario, "pending_before_confirmation_calls": 0,
        "resolution": result["resolution"], "replay_did_not_create_again": True,
        "supplier_create_calls": app.state.lab.supplier.create_calls,
        "history_has_result": any((m.extra or {}).get("booking_results") for m in app.state.lab.history.list_messages(SESSION_ID, "u001"))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8006)
    parser.add_argument("--scenario", choices=["ok", "reject", "lost_after_commit", "lost_before_commit"], default="ok")
    args = parser.parse_args()
    application = asyncio.run(create_demo_app(args.scenario, delay=0.04 if args.serve else 0))
    if args.serve:
        import uvicorn
        uvicorn.run(application, host="127.0.0.1", port=args.port)
    else:
        try:
            exercise(application)
        finally:
            application.state.lab.engine.dispose()
            application.state.lab.directory.cleanup()
