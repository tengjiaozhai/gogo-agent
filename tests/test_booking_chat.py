"""网页聊天预订的认证、结构化确认、真实SDK委派、SQL保存和模拟供应商边界。"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time

import pytest
from sqlalchemy import select

from scripts.demo_booking_chat import SESSION_ID, QUESTION, BookingDemoModel, create_demo_app
from gogo_agent.booking.service import BookingError
from gogo_agent.db.models import BookingRecordModel
from gogo_agent.planning.review import ApprovedTrip
from gogo_agent.planning.simulated import SIMULATED_POLICY, simulated_assessors
from gogo_agent.request_context import RequestContext
from gogo_agent.booking.chat import BookingChatTurn
from gogo_agent.intent.models import IntentCategory, IntentItem, IntentResult, ConfidenceLevel, QueryInput, RecognitionDecision, RecognitionLayer
from gogo_agent.intent.pipeline import PreparedIntentTurn
from fastapi.testclient import TestClient


@pytest.fixture
def lab_factory():
    apps = []
    def create(mode="ok"):
        app = asyncio.run(create_demo_app(mode))
        apps.append(app)
        client = TestClient(app)
        token = client.post("/api/auth/login", json={"username": "alice", "password": "123456"}).json()["token"]
        return app, client, {"Authorization": token, "Accept": "application/json"}
    yield create
    for app in apps:
        app.state.lab.engine.dispose()
        app.state.lab.directory.cleanup()


def post(client, headers, body, session=SESSION_ID):
    return client.post(f"/api/chat/{session}", headers=headers, json=body)


def prepare(client, headers):
    response = post(client, headers, {"message": QUESTION})
    assert response.status_code == 200, response.text
    return next(item["data"] for item in response.json()["timeline"] if item["kind"] == "booking_confirmation")


def confirm_body(pending):
    return {"message": "确认模拟预订：" + pending["title"], "booking_confirmation_token": pending["token"]}


def booking_result(response):
    assert response.status_code == 200, response.text
    return next(item["data"] for item in response.json()["timeline"] if item["kind"] == "booking")


def events(response):
    result = []
    for chunk in response.text.split("\n\n"):
        lines = chunk.splitlines()
        name = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = "\n".join(line[6:] for line in lines if line.startswith("data: "))
        if name:
            result.append((name, data))
    return result


def test_full_http_chain_uses_booking_agent_confirmation_and_one_supplier_order(lab_factory):
    app, client, headers = lab_factory()
    pending = prepare(client, headers)
    assert pending["simulated"] is True and app.state.lab.supplier.create_calls == 0
    reply = app.state.lab.history.list_messages(SESSION_ID, "u001")[-1]
    assert reply.extra["tool_calls"][-1]["agent_name"] == "BookingAgent"
    assert reply.extra["tool_calls"][-1]["tool_name"] == "booking_agent"
    for name in ["GoGo", "BookingAgent"]:
        state = app.state.lab.executor.session_store.load_agent_state(
            app.state.lab.executor._state_session_id(SESSION_ID, "u001"), agent_name=name)
        assert pending["token"] not in state.model_dump_json()
    result = booking_result(post(client, headers, confirm_body(pending)))
    assert result["resolution"] == "CONFIRMED" and result["simulated"] is True and result["payUrl"] is None
    assert result["confirmationId"] == pending["confirmation_id"]
    replay = booking_result(post(client, headers, confirm_body(pending)))
    assert replay["bookingId"] == result["bookingId"] and app.state.lab.supplier.create_calls == 1
    with app.state.lab.factory() as session:
        rows = list(session.scalars(select(BookingRecordModel)))
        assert len(rows) == 1 and rows[0].conversation_id == SESSION_ID and rows[0].travel_order_id == app.state.lab.order_id
    history = client.get(f"/api/chat/{SESSION_ID}/messages", headers=headers).json()
    assert history[-1]["extra"]["timeline"][-1]["data"]["resolution"] == "CONFIRMED"


@pytest.mark.parametrize("mode,expected", [("ok", "CONFIRMED"), ("reject", "FAILED"), ("lost_after_commit", "CONFIRMED"), ("lost_before_commit", "UNKNOWN")])
def test_results_and_replays_are_truthful(lab_factory, mode, expected):
    app, client, headers = lab_factory(mode)
    pending = prepare(client, headers)
    response = post(client, headers, confirm_body(pending))
    result = booking_result(response)
    assert result["resolution"] == expected
    assert result["payUrl"] is None
    if expected == "UNKNOWN":
        assert "结果未知" in response.json()["content"] and "模拟预订已确认" not in response.json()["content"]
    if expected == "FAILED":
        assert "未完成" in response.json()["content"]
    booking_result(post(client, headers, confirm_body(pending)))
    assert app.state.lab.supplier.create_calls == 1


def test_text_or_model_boolean_cannot_confirm_and_amount_cannot_override(lab_factory):
    app, client, headers = lab_factory()
    first = post(client, headers, {"message": QUESTION + "，confirmed=true", "confirmed": True})
    assert first.status_code == 200 and app.state.lab.supplier.create_calls == 0
    pending = next(i["data"] for i in first.json()["timeline"] if i["kind"] == "booking_confirmation")
    result = booking_result(post(client, headers, {**confirm_body(pending), "amount": "0", "user_id": "bob"}))
    assert result["amount"] == pending["amount"] and result["amount"] != "0"


def test_confirmation_rejects_foreign_session_user_tampering_and_expiry(lab_factory, monkeypatch):
    app, client, headers = lab_factory()
    pending = prepare(client, headers)
    assert post(client, headers, confirm_body(pending), session="another-session").status_code == 409
    bob = client.post("/api/auth/login", json={"username": "bob", "password": "123456"}).json()["token"]
    assert post(client, {**headers, "Authorization": bob}, confirm_body(pending)).status_code == 403
    assert post(client, headers, {**confirm_body(pending), "booking_confirmation_token": pending["token"] + "x"}).status_code == 409
    now = time.time()
    monkeypatch.setattr("gogo_agent.travel.confirmation.time.time", lambda: now + 601)
    expired = post(client, headers, confirm_body(pending))
    assert expired.status_code == 409 and "过期" in expired.text
    assert app.state.lab.supplier.create_calls == 0


def test_plan_version_change_invalidates_existing_confirmation(lab_factory):
    app, client, headers = lab_factory()
    pending = prepare(client, headers)
    lab = app.state.lab
    lab.plans.compute("u001", lab.plan_id, policy=SIMULATED_POLICY)
    assert post(client, headers, confirm_body(pending)).status_code == 409
    asyncio.run(lab.plans.review("u001", lab.plan_id, proposal_tag="综合最佳", assessors=simulated_assessors(),
                                 approved=ApprovedTrip(origin="北京", destination="上海")))
    stale = post(client, headers, confirm_body(pending))
    assert stale.status_code == 409 and "变化" in stale.text and lab.supplier.create_calls == 0


def test_model_cannot_call_execution_tool_without_structured_confirmation(lab_factory, monkeypatch):
    from agentscope.message import ToolCallBlock
    from agentscope.model import ChatResponse
    app, client, headers = lab_factory()
    class ForgedModel(BookingDemoModel):
        async def _call_api(self, *args, **kwargs):
            if self.role == "booking" and self.call_count == 0:
                self.call_count += 1
                return ChatResponse(content=[ToolCallBlock(id="forged", name="execute_confirmed_booking", input="{}")], is_last=True)
            return await super()._call_api(*args, **kwargs)
    executor = app.state.lab.executor
    monkeypatch.setattr(executor, "_build_model", lambda stream=False, role="master": ForgedModel(role, executor.plan_id, stream=stream))
    response = post(client, headers, {"message": QUESTION})
    assert response.status_code == 200 and "本轮未下单" in response.json()["content"]
    assert app.state.lab.supplier.create_calls == 0


def test_sse_only_delivers_confirmation_and_result_after_reply_saved(lab_factory):
    app, client, headers = lab_factory()
    sse_headers = {**headers, "Accept": "text/event-stream"}
    first = events(post(client, sse_headers, {"message": QUESTION}))
    names = [name for name, _ in first]
    assert names.index("booking_confirmation") < names.index("message_id")
    pending = json.loads(next(data for name, data in first if name == "booking_confirmation"))
    result_events = events(post(client, sse_headers, confirm_body(pending)))
    names = [name for name, _ in result_events]
    assert names.index("booking_result") < names.index("message_id")
    assert json.loads(next(data for name, data in result_events if name == "booking_result"))["resolution"] == "CONFIRMED"
    assert app.state.lab.supplier.create_calls == 1


def test_reply_persistence_failure_leaks_no_confirmation(lab_factory, monkeypatch):
    app, client, headers = lab_factory()
    def fail(*args, **kwargs):
        raise RuntimeError("isolated state write failure")
    monkeypatch.setattr(app.state.lab.executor.session_store, "save_agent_state", fail)
    output = events(post(client, {**headers, "Accept": "text/event-stream"}, {"message": QUESTION}))
    assert "error" in [name for name, _ in output]
    assert not any(name in {"booking_confirmation", "message_id"} for name, _ in output)
    assert len(app.state.lab.history.list_messages(SESSION_ID, "u001")) == 3
    assert app.state.lab.supplier.create_calls == 0


def test_order_survives_reply_failure_and_same_confirmation_recovers_without_new_order(lab_factory, monkeypatch):
    app, client, headers = lab_factory()
    pending = prepare(client, headers)
    store = app.state.lab.executor.session_store
    original = store.save_agent_state
    def fail(*args, **kwargs):
        raise RuntimeError("isolated reply failure")
    monkeypatch.setattr(store, "save_agent_state", fail)
    output = events(post(client, {**headers, "Accept": "text/event-stream"}, confirm_body(pending)))
    assert "error" in [name for name, _ in output] and not any(name == "message_id" for name, _ in output)
    assert app.state.lab.supplier.create_calls == 1
    monkeypatch.setattr(store, "save_agent_state", original)
    assert booking_result(post(client, headers, confirm_body(pending)))["resolution"] == "CONFIRMED"
    assert app.state.lab.supplier.create_calls == 1


def test_unknown_order_blocks_new_plan_version_for_same_leg(lab_factory):
    app, client, headers = lab_factory("lost_before_commit")
    pending = prepare(client, headers)
    assert booking_result(post(client, headers, confirm_body(pending)))["resolution"] == "UNKNOWN"
    lab = app.state.lab
    lab.plans.compute("u001", lab.plan_id, policy=SIMULATED_POLICY)
    asyncio.run(lab.plans.review("u001", lab.plan_id, proposal_tag="综合最佳", assessors=simulated_assessors(),
                                 approved=ApprovedTrip(origin="北京", destination="上海")))
    again = post(client, headers, {"message": QUESTION})
    assert again.status_code == 200 and "不能换方案重复下单" in again.json()["content"]
    assert not again.json().get("timeline") and lab.supplier.create_calls == 1
    with pytest.raises(BookingError) as blocked:
        lab.bookings.book("u001", lab.plan_id, proposal_tag="综合最佳", leg="hotel", confirmed=True)
    assert blocked.value.code == "BOOKING_RESULT_UNKNOWN"


def test_concurrent_identical_confirmations_create_one_supplier_order(lab_factory):
    app, client, headers = lab_factory()
    pending = prepare(client, headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(client, headers, confirm_body(pending)), range(2)))
    assert any(response.status_code == 200 for response in responses)
    assert app.state.lab.supplier.create_calls == 1 and len(app.state.lab.supplier.orders) == 1


def test_mixed_intent_cannot_turn_pending_booking_into_model_claimed_success(lab_factory):
    app, client, headers = lab_factory()
    history, lab = app.state.lab.history, app.state.lab
    request = RequestContext(user_id="u001", session_id=SESSION_ID, request_id="mixed")
    turn = BookingChatTurn(request, history, lambda: lab.bookings)
    turn.prepare(lab.plan_id, "综合最佳", "hotel")
    turn.other_answers.append("政策查询：按已验证的政策结果")
    items = [IntentItem(intent=category, confidence=ConfidenceLevel.HIGH, reason="固定混合意图", evidence=["查询政策并预订"])
             for category in [IntentCategory.POLICY_QUERY, IntentCategory.BOOKING]]
    result = IntentResult(intents=items, primary_intent=IntentCategory.BOOKING, multi_intent=True, overall_reason="固定回归")
    decision = RecognitionDecision(result=result, hit_layer=RecognitionLayer.RULE, confidence=ConfidenceLevel.HIGH,
        vector_threshold=None, candidates=[], attempted_layers=[RecognitionLayer.RULE], reason="fixed")
    query = QueryInput(question="查询政策并预订")
    prepared = PreparedIntentTurn(branch="fast", original_question=query, effective_question=query, fast_decision=decision, decision=decision, rewrite=None)
    text = turn.reply_text("已预订成功", prepared)
    assert "政策查询" in text and "尚未下单" in text and "已预订成功" not in text


def call(toolkit, name, **kwargs):
    tool = asyncio.run(toolkit.get_tool(name))
    return json.loads(asyncio.run(tool.call(**kwargs)).content[0].text)


def test_chat_planning_can_bind_approved_order_review_and_become_bookable(lab_factory):
    from gogo_agent.planning.tools import build_plan_toolkit
    from gogo_agent.planning.simulated import simulated_provider
    app, _, _ = lab_factory()
    lab = app.state.lab
    ctx = RequestContext(user_id="u001", session_id="new-planning-chat", request_id="p-request")
    toolkit = build_plan_toolkit(lab.plans, ctx, search_provider=simulated_provider, assessors=simulated_assessors(),
                                 travel_provider=lambda: lab.bookings.travel)
    approved = call(toolkit, "list_approved_orders")
    assert approved["data"]["orders"][0]["order_id"] == lab.order_id
    data = call(toolkit, "create_plan", origin="北京", destination="上海", departure_date="2026-11-10", return_date="2026-11-12",
                order_id=lab.order_id)
    assert data["success"] is True
    pid = data["data"]["plan_id"]
    assert lab.plans.get("u001", pid).session_id == ctx.session_id
    call(toolkit, "search_candidates", plan_id=pid)
    call(toolkit, "compute_plan", plan_id=pid, policy=SIMULATED_POLICY.model_dump())
    reviewed = call(toolkit, "review_plan", plan_id=pid, proposal_tag="综合最佳")
    assert reviewed["data"]["verdict"] == "pass"
    assert lab.bookings.check_ready("u001", pid, proposal_tag="综合最佳", leg="hotel")["start_time"] == "2026-11-10"
    assert any(plan.plan_id == pid for plan in lab.plans.list_for_session("u001", ctx.session_id))
    assert lab.plans.list_for_session("u002", ctx.session_id) == []


def test_order_binding_rejects_other_user_and_mismatched_dates(lab_factory):
    from gogo_agent.planning.tools import build_plan_toolkit
    from gogo_agent.planning.simulated import simulated_provider
    app, _, _ = lab_factory()
    lab = app.state.lab
    def toolkit(user):
        return build_plan_toolkit(lab.plans, RequestContext(user_id=user, session_id="bind", request_id="r"),
            search_provider=simulated_provider, assessors=simulated_assessors(), travel_provider=lambda: lab.bookings.travel)
    payload = dict(origin="北京", destination="上海", departure_date="2026-11-10", return_date="2026-11-12", order_id=lab.order_id)
    assert call(toolkit("u002"), "create_plan", **payload)["success"] is False
    mismatch = call(toolkit("u001"), "create_plan", **{**payload, "return_date": "2026-11-13"})
    assert mismatch["errorCode"] == "ORDER_PLAN_MISMATCH"
    created = call(toolkit("u001"), "create_plan", **{**payload, "order_id": ""})["data"]["plan_id"]
    bound = call(toolkit("u001"), "bind_travel_order", plan_id=created, order_id=lab.order_id)
    assert bound["data"]["order_id"] == lab.order_id and not bound["data"]["review_current"]


def test_pending_card_contains_dates_and_same_version_other_tag_cannot_book_twice(lab_factory):
    app, client, headers = lab_factory()
    pending = prepare(client, headers)
    assert pending["route"] == "上海" and pending["start_time"] == "2026-11-10" and pending["end_time"] == "2026-11-12"
    booking_result(post(client, headers, confirm_body(pending)))
    with pytest.raises(BookingError) as duplicate:
        app.state.lab.bookings.check_ready("u001", app.state.lab.plan_id, proposal_tag="价格最低", leg="hotel")
    # 价格最低是另一套代表方案，需先审核；无论哪一层拒绝，供应商不会再次调用。
    assert duplicate.value.code in {"REVIEW_REQUIRED", "BOOKING_ALREADY_CONFIRMED"}
    assert app.state.lab.supplier.create_calls == 1


def test_booking_query_uses_own_local_records_without_new_supplier_calls(lab_factory):
    app, client, headers = lab_factory()
    pending = prepare(client, headers)
    booking_result(post(client, headers, confirm_body(pending)))
    response = post(client, headers, {"message": "查询我的预订记录"})
    assert response.status_code == 200 and "SUP000001" in response.json()["content"]
    assert app.state.lab.supplier.create_calls == 1
    bob = client.post("/api/auth/login", json={"username": "bob", "password": "123456"}).json()["token"]
    response = post(client, {**headers, "Authorization": bob}, {"message": "查询我的预订记录"}, session="bob-query")
    assert response.status_code == 200 and "暂无" in response.json()["content"] and "SUP000001" not in response.json()["content"]


def test_session_plan_filter_happens_before_recent_limit(lab_factory):
    from scripts.demo_booking_chat import CONDITIONS
    app, _, _ = lab_factory()
    lab = app.state.lab
    for i in range(25):
        lab.plans.create("u001", CONDITIONS, session_id=f"other-session-{i}")
    visible = lab.plans.list_for_session("u001", SESSION_ID)
    assert [plan.plan_id for plan in visible] == [lab.plan_id]
    assert lab.plans.list_for_session("u002", SESSION_ID) == []


@pytest.mark.parametrize("mode,states", [
    ("ok", ["PENDING_SUPPLIER", "CONFIRMED"]),
    ("reject", ["PENDING_SUPPLIER", "FAILED"]),
    ("lost_before_commit", ["PENDING_SUPPLIER", "UNKNOWN"]),
    ("lost_after_commit", ["PENDING_SUPPLIER", "UNKNOWN", "CONFIRMED"]),
])
def test_booking_transitions_are_audited_atomically_and_replays_do_not_duplicate(lab_factory, mode, states):
    from gogo_agent.db.models import BusinessActionAuditModel
    app, client, headers = lab_factory(mode)
    pending = prepare(client, headers)
    result = booking_result(post(client, headers, confirm_body(pending)))
    def rows():
        with app.state.lab.factory() as session:
            return list(session.scalars(select(BusinessActionAuditModel)
                .where(BusinessActionAuditModel.action == "supplier_booking").order_by(BusinessActionAuditModel.created_at)))
    first = rows()
    assert [json.loads(row.result_json)["resolution"] for row in first] == states
    assert all(json.loads(row.result_json)["booking_id"] == result["bookingId"] for row in first)
    assert all(row.user_id == "u001" and pending["token"] not in row.result_json for row in first)
    booking_result(post(client, headers, confirm_body(pending)))
    assert len(rows()) == len(first) and app.state.lab.supplier.create_calls == 1


def test_booking_audit_failure_rolls_back_pending_before_supplier_call(lab_factory, monkeypatch):
    app, client, headers = lab_factory()
    pending = prepare(client, headers)
    def unavailable(*args, **kwargs):
        raise RuntimeError("isolated audit failure")
    monkeypatch.setattr(app.state.lab.bookings.travel.repository, "audit", unavailable)
    response = post(client, headers, confirm_body(pending))
    assert response.status_code == 503
    assert app.state.lab.supplier.create_calls == 0
    with app.state.lab.factory() as session:
        assert list(session.scalars(select(BookingRecordModel))) == []


def test_terminal_audit_failure_returns_unknown_and_recovers_by_lookup_only(lab_factory, monkeypatch):
    from gogo_agent.db.models import BusinessActionAuditModel
    app, client, headers = lab_factory()
    pending = prepare(client, headers)
    repo = app.state.lab.bookings.travel.repository
    original = repo.audit
    def terminal_failure(session, user_id, action, key):
        if action == "supplier_booking" and key.endswith(":CONFIRMED"):
            raise RuntimeError("isolated terminal audit failure")
        return original(session, user_id, action, key)
    monkeypatch.setattr(repo, "audit", terminal_failure)
    result = booking_result(post(client, headers, confirm_body(pending)))
    assert result["resolution"] == "UNKNOWN" and "审计保存未完成" in result["detail"]
    assert booking_result(post(client, headers, confirm_body(pending)))["resolution"] == "UNKNOWN"
    assert app.state.lab.supplier.create_calls == 1
    with app.state.lab.factory() as session:
        row = session.scalar(select(BookingRecordModel))
        assert row.status == "CREATED"  # 终态与其审计一起回滚，待处理证据仍在。
    monkeypatch.setattr(repo, "audit", original)
    assert booking_result(post(client, headers, confirm_body(pending)))["resolution"] == "CONFIRMED"
    assert app.state.lab.supplier.create_calls == 1
    with app.state.lab.factory() as session:
        audit = list(session.scalars(select(BusinessActionAuditModel).where(BusinessActionAuditModel.action == "supplier_booking")))
        assert {json.loads(row.result_json)["resolution"] for row in audit} == {"PENDING_SUPPLIER", "CONFIRMED"}


def test_read_only_reconciliation_works_after_plan_change_without_confirmation(lab_factory):
    from decimal import Decimal
    from gogo_agent.booking.supplier import SupplierOrder
    app, client, headers = lab_factory("lost_before_commit")
    pending = prepare(client, headers)
    first = booking_result(post(client, headers, confirm_body(pending)))
    assert first["resolution"] == "UNKNOWN"
    lab = app.state.lab
    lab.plans.compute("u001", lab.plan_id, policy=SIMULATED_POLICY)  # 原确认已失效，但读取原单仍合法。
    lab.supplier.orders[first["bookingId"]] = SupplierOrder(request_id=first["bookingId"], order_no="SUP-LATE",
        status="CONFIRMED", amount=Decimal(pending["amount"]), currency="CNY")
    response = post(client, headers, {"message": f"查询我的预订记录 {first['bookingId']}（核实结果）"})
    assert response.status_code == 200 and "模拟预订已确认" in response.json()["content"]
    assert "SUP-LATE" in response.json()["content"] and lab.supplier.create_calls == 1
    with pytest.raises(BookingError) as foreign:
        lab.bookings.check_result("u002", first["bookingId"])
    assert foreign.value.code == "BOOKING_NOT_FOUND"


@pytest.mark.parametrize("mode,failed_state", [("reject", "FAILED"), ("lost_after_commit", "UNKNOWN")])
def test_rejection_or_unknown_audit_failure_still_reports_unknown(lab_factory, monkeypatch, mode, failed_state):
    app, client, headers = lab_factory(mode)
    pending = prepare(client, headers)
    repo = app.state.lab.bookings.travel.repository
    original = repo.audit
    def fail_state(session, user_id, action, key):
        if action == "supplier_booking" and key.endswith(":" + failed_state):
            raise RuntimeError("isolated state audit failure")
        return original(session, user_id, action, key)
    monkeypatch.setattr(repo, "audit", fail_state)
    result = booking_result(post(client, headers, confirm_body(pending)))
    assert result["resolution"] == "UNKNOWN" and "审计保存未完成" in result["detail"]
    assert app.state.lab.supplier.create_calls == 1
    monkeypatch.setattr(repo, "audit", original)
    response = post(client, headers, {"message": f"查询我的预订记录 {result['bookingId']}（核实结果）"})
    assert response.status_code == 200 and app.state.lab.supplier.create_calls == 1
    if mode == "lost_after_commit":
        assert "模拟预订已确认" in response.json()["content"]
