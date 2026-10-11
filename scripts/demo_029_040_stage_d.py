"""阶段 D 离线断点入口：cd 仓库根目录；PYTHONPATH=src GOGO_DATABASE_URL='' .venv/bin/python scripts/demo_029_040_stage_d.py --case all。

无需 MariaDB、Qdrant、Redis 或模型密钥；每个 case 新建隔离 SQLite 和固定 Agent 模型。
可单独运行 --case 029/031/032/033/035/036/037/038/039/040；030、034、041 有独立脚本。
PyCharm：Script path 为本文件，Working directory 为仓库根目录，Interpreter 为 .venv/bin/python，
Parameters 填 --case 035，Environment 填 PYTHONPATH=src;GOGO_DATABASE_URL=。
完整断点表、隐藏前置轮次与练习：docs/契约样例/阶段D-029-041-调试指南.md。
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import json

from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gogo_agent.api import app
from gogo_agent.auth.dependencies import get_auth_service
from gogo_agent.auth.models import UserAccount
from gogo_agent.auth.repository import InMemoryUserAccountRepository
from gogo_agent.auth.security import InMemoryTokenStore, TokenManager
from gogo_agent.auth.service import AuthService
from gogo_agent.chat.dependencies import get_chat_executor, get_chat_history_service
from gogo_agent.chat.executor import ChatAgentExecutor, _FallbackMockModel
from gogo_agent.chat.recommendation import generate_recommendations
from gogo_agent.chat.repository import InMemoryAgentSessionStore, InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.db.base import Base
from gogo_agent.db.models import BookingRecordModel, UserProfileModel
from gogo_agent.intent import (
    ConfidenceLevel, FastMatch, IntentCandidate, IntentCategory, IntentItem,
    IntentPipelineService, IntentRecognizer, IntentResult, MatchStatus,
    RecognitionLayer, RewriteContextBuilder,
)
from gogo_agent.request_context import RequestContext
from gogo_agent.travel.cache import UserPolicyCache
from gogo_agent.travel.conflict import TripSegment, detect_conflicts
from gogo_agent.travel.models import (
    AdminDecisionRequest, BookingCancelRequest, CancelTravelRequest,
    ModifyTravelRequest, PolicyCheckInput, SubmitTravelRequest,
    TravelOrderDraft, TravelOrderPatch, UserProfilePatch,
)
from gogo_agent.travel.repository import BusinessRepository
from gogo_agent.travel.service import BusinessError, TravelBusinessService
from gogo_agent.travel.tools import build_manage_toolkit
import gogo_agent.chat.executor as executor_module


def make_service():
    """每个案例使用独立库；职级由可信种子写入，用户自己不能升级职级。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    service = TravelBusinessService(BusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)))
    with service.repository.transaction() as session:
        session.add_all([UserProfileModel(user_id="u001", level="P6"), UserProfileModel(user_id="u002", level="P5")])
    return engine, service


def submit(service, *, key="demo-submit", destination="上海"):
    """035 之后案例的隐藏准备轮次：用户已显式确认并提交一张单。"""
    return service.submit_order("u001", SubmitTravelRequest(
        destination=destination, departure_city="北京",
        departure_date=date(2026, 11, 10), return_date=date(2026, 11, 12),
        purpose="客户拜访", confirmed=True, idempotency_key=key,
    ))


class PolicyRule:
    """029 离线 L1 识别，仍执行真实 IntentPipelineService。"""

    def match(self, query):
        result = IntentResult(
            intents=[IntentItem(intent=IntentCategory.POLICY_QUERY,
                                confidence=ConfidenceLevel.HIGH, reason="离线政策意图", evidence=[query.question])],
            primary_intent=IntentCategory.POLICY_QUERY, multi_intent=False,
            overall_reason="029 只演示管理路由",
        )
        return FastMatch(
            status=MatchStatus.HIT, result=result, threshold=None,
            candidates=[IntentCandidate(intent=IntentCategory.POLICY_QUERY,
                                        layer=RecognitionLayer.RULE, score=None, reason="离线 L1 命中")],
            reason="离线 L1 命中",
        )


class UnusedRewriter:
    """029 L1 已命中时改写器不应运行。"""

    async def rewrite(self, context):
        raise AssertionError("029 L1 命中时不调用改写器")


class MasterModel(_FallbackMockModel):
    """第一轮调用管理工具，第二轮只汇总真实工具结果。"""

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.call_count += 1
        names = [item["function"]["name"] for item in tools or []]
        print("  GoGo 工具 schema:", names)
        if self.call_count == 1:
            return ChatResponse(content=[ToolCallBlock(
                id="demo-manage", name="itinerary_manage_agent",
                input=json.dumps({"question": "查询上海差旅政策"}, ensure_ascii=False),
            )], is_last=True)
        got = any(isinstance(block, ToolResultBlock) for msg in messages for block in msg.content)
        return ChatResponse(content=[TextBlock(text="GoGo 已取得管理 Agent 的结果" if got else "未取得结果")], is_last=True)


class ManageModel(_FallbackMockModel):
    """029 固定管理回答，用于验证真实 AgentScope 工具委派和状态保存。"""

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        print("  ItineraryManageAgent 工具数:", len(tools or []))
        assert any(item["function"]["name"] == "query_travel_policy" for item in tools or [])
        return ChatResponse(content=[TextBlock(text="请用精确政策工具查询本人职级与上海规则。")], is_last=True)


def case_029(service):
    auth = AuthService(InMemoryUserAccountRepository(), TokenManager(store=InMemoryTokenStore()))
    history = ChatHistoryService(InMemoryChatHistoryRepository())

    @asynccontextmanager
    async def pipeline_factory(history_service):
        model = _FallbackMockModel()
        try:
            yield IntentPipelineService(
                RewriteContextBuilder(history_service), UnusedRewriter(),
                IntentRecognizer(model, rule_matcher=PolicyRule()),
            )
        finally:
            await model.client.close()

    executor = ChatAgentExecutor(history, InMemoryAgentSessionStore(), pipeline_factory=pipeline_factory)
    master = MasterModel(stream=False)
    manage = ManageModel(stream=False)
    executor._build_model = lambda stream=False, role="master": master if role == "master" else manage

    async def no_recommendations(question, answer):
        return []

    executor.recommend_questions = no_recommendations
    previous = dict(app.dependency_overrides)
    original_service = executor_module.get_travel_service
    executor_module.get_travel_service = lambda: service
    app.dependency_overrides[get_auth_service] = lambda: auth
    app.dependency_overrides[get_chat_executor] = lambda: executor
    app.dependency_overrides[get_chat_history_service] = lambda: history
    try:
        response = TestClient(app).post(
            "/api/chat/demo-stage-d-029",
            headers={"Authorization": auth.login("alice", "123456"), "Accept": "application/json"},
            json={"message": "查询上海差旅政策"},
        )
        response.raise_for_status()
        saved = history.list_messages("demo-stage-d-029", "u001")[-1]
        key = executor._state_session_id("demo-stage-d-029", "u001")
        print("  HTTP 回复:", response.json()["content"])
        print("  工具记录:", saved.extra["tool_calls"])
        print("  管理 AgentState 已保存:", executor.session_store.load_agent_state(key, agent_name="ItineraryManageAgent") is not None)
    finally:
        executor_module.get_travel_service = original_service
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def case_030(service):
    print("  阶段 D 表:", ["travel_order", "approval_record", "user_profile", "booking_record", "business_action_audit"])
    try:
        TravelOrderDraft(destination="上海", departure_city="北京", departure_date="2026-11-12",
                         return_date="2026-11-10", purpose="客户拜访")
    except ValueError as exc:
        print("  非法日期:", type(exc).__name__)
    print("  可信资料职级:", service.get_profile("u001").level)


async def case_031(service):
    order = submit(service)
    toolkit = build_manage_toolkit(service, RequestContext(user_id="u002", session_id="demo", request_id="r"))
    schema = (await toolkit.get_tool_schemas())[0]
    tool = await toolkit.get_tool("get_travel_order")
    denied = await tool.call(order_id=order.order_id)
    print("  get_travel_order 参数:", schema["function"]["parameters"])
    print("  Bob 查询 Alice 单:", denied.content[0].text)


def case_032(service):
    alice = service.query_policy("u001", "上海", on_date=date(2026, 10, 7))
    bob = service.query_policy("u002", "上海", on_date=date(2026, 10, 7))
    print("  P6/P5 上海酒店上限:", alice.rule.hotel_limit, bob.rule.hotel_limit)
    for amount in (Decimal("500"), Decimal("500.01"), None):
        result = service.check_policy("u001", "上海", PolicyCheckInput(type="HOTEL", amount=amount),
                                      on_date=date(2026, 10, 7))
        print("  酒店金额", amount, "合规:", result.compliant)


def case_033(service):
    profile = service.update_profile("u001", UserProfilePatch(phone="13800000001", base_city="北京"))
    print("  Alice 常驻地:", profile.base_city, "机票缺项:", service.profile_completeness("u001", "FLIGHT").missing_fields)
    print("  Bob 手机仍为空:", service.get_profile("u002").phone is None)
    try:
        UserProfilePatch(level="P9")
    except ValueError as exc:
        print("  用户自改职级:", type(exc).__name__)


def case_034(service):
    hits = service.search_policy_knowledge("上海酒店住宿标准")
    print("  片段数量:", len(hits), "首条来源/版本:", (hits[0].source, hits[0].version) if hits else None)
    print("  未知问题结果:", service.search_policy_knowledge("量子火箭推进器"))


def case_035(service):
    order = submit(service)
    print("  提交状态/审批:", order.status, bool(order.approval_id))
    changed = service.modify_order("u001", order.order_id, ModifyTravelRequest(
        confirmed=True, idempotency_key="demo-modify", changes=TravelOrderPatch(destination="杭州"),
    ))
    print("  修改后新审批:", changed.approval_id != order.approval_id)
    print("  审批历史:", [item.status for item in service.list_approvals_for_order("u001", order.order_id)])
    cancelled = service.cancel_order("u001", order.order_id, CancelTravelRequest(
        confirmed=True, idempotency_key="demo-cancel",
    ))
    print("  取消并撤审:", cancelled.cancelled, cancelled.approval_cancelled)


def case_036(service):
    candidate = TripSegment(order_id="new", departure_city="北京", destination="上海",
                            start_at=datetime(2026, 11, 10, 9), end_at=datetime(2026, 11, 10, 12))
    overlap = TripSegment(order_id="overlap", departure_city="广州", destination="深圳",
                          start_at=datetime(2026, 11, 10, 10), end_at=datetime(2026, 11, 10, 14))
    exact = TripSegment(order_id="exact", departure_city="杭州", destination="苏州",
                        start_at=datetime(2026, 11, 10, 15), end_at=datetime(2026, 11, 10, 16))
    unknown = TripSegment(order_id="unknown", departure_city="拉萨", destination="成都",
                          start_at=datetime(2026, 11, 10, 17), end_at=datetime(2026, 11, 10, 18))
    minutes = lambda a, b: 180 if {a, b} == {"上海", "杭州"} else None
    report = detect_conflicts(candidate, [overlap, exact, unknown], minutes)
    print("  冲突等级:", [(item.order_id, item.severity) for item in report.conflicts])
    print("  缺交通数据:", report.uncertain)


def case_037(service):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with service.repository.transaction() as session:
        session.add(BookingRecordModel(
            booking_id="bk-demo", user_id="u001", conversation_id=None, travel_order_id=None,
            biz_type="HOTEL", platform="simulated", external_order_no="hotel-ext", status="CONFIRMED",
            external_status=None, payment_status="PAID", title="演示酒店", total_amount=Decimal("400"),
            currency="CNY", contact_name=None, contact_phone=None, start_time=None, end_time=None,
            detail=None, remark=None, booked_at=None, created_at=now, updated_at=now, deleted=0,
        ))
    print("  Bob 可见预订:", len(service.list_bookings("u002")))
    result = service.cancel_booking("u001", "bk-demo", BookingCancelRequest(
        confirmed=True, idempotency_key="demo-booking-cancel",
    ))
    print("  内部取消/供应商取消:", result.cancelled, result.supplier_cancelled, result.message)


def case_038(service):
    order = submit(service)
    admin = UserAccount(user_id="u_001", username="admin", password_hash="test", role="ADMIN")
    normal = UserAccount(user_id="u002", username="bob", password_hash="test", role="USER")
    try:
        service.decide_approval(normal, order.approval_id, AdminDecisionRequest(decision="agree"))
    except BusinessError as exc:
        print("  普通用户审批:", exc.code)
    approved = service.decide_approval(admin, order.approval_id, AdminDecisionRequest(decision="agree"))
    print("  管理员审批/订单:", approved.status, service.get_order("u001", order.order_id).status)
    print("  回调入口固定禁用：POST /api/callback/dingtalk/approval -> HTTP 503")


def case_039(service):
    clock = [0.0]
    cache = UserPolicyCache(ttl_seconds=10, clock=lambda: clock[0])
    calls = [0]

    def load():
        calls[0] += 1
        return calls[0]

    print("  同用户同参两次:", cache.get_or_load("u001", "上海", "P6", "v1", "2026-10-07", load),
          cache.get_or_load("u001", "上海", "P6", "v1", "2026-10-07", load), "loader 次数:", calls[0])
    print("  换用户:", cache.get_or_load("u002", "上海", "P6", "v1", "2026-10-07", load))
    cache.invalidate_user("u001")
    print("  Alice 资料更新失效后:", cache.get_or_load("u001", "上海", "P6", "v1", "2026-10-07", load))
    clock[0] = 11
    print("  TTL 到期后:", cache.get_or_load("u002", "上海", "P6", "v1", "2026-10-07", load))


class RecommendationModel(_FallbackMockModel):
    """040 快模型固定 JSON；不接触 AgentState 或数据库。"""

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        return ChatResponse(content=[TextBlock(text=json.dumps({"questions": [
            "上海酒店上限是多少？", "上海酒店上限是多少?", "如何查询审批进度？", "继续",
        ]}, ensure_ascii=False))], is_last=True)


async def case_040(service):
    model = RecommendationModel(stream=False)
    try:
        result = await generate_recommendations(model, "我想申请差旅", "请先核对出行日期，当前没有创建差旅单。")
        print("  主答复已完成后 recommendations:", result)
        print("  SSE 只在非空时发送 event: suggestions")
    finally:
        await model.client.close()


CASES = {
    "029": case_029, "030": case_030, "031": case_031, "032": case_032,
    "033": case_033, "034": case_034, "035": case_035, "036": case_036,
    "037": case_037, "038": case_038, "039": case_039, "040": case_040,
}


def main():
    parser = argparse.ArgumentParser(description="029-040 差旅管理离线单步演示")
    parser.add_argument("--case", choices=("all", *CASES), default="all")
    selected = parser.parse_args().case
    for number in CASES if selected == "all" else (selected,):
        engine, service = make_service()
        try:
            print(f"\n[{number}]")
            result = CASES[number](service)
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        finally:
            engine.dispose()
    print("\n本脚本使用真实业务服务、SQLite、AgentScope Toolkit；模型和认证数据为离线替身。")


if __name__ == "__main__":
    main()
