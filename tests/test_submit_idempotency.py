"""差旅提交的幂等边界：同一次确认的重试只生效一次；取消后的新确认生成新单；已失效的订单不得报告为已提交。"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gogo_agent.db.base import Base
from gogo_agent.db.models import UserProfileModel
from gogo_agent.request_context import RequestContext
from gogo_agent.travel.models import CancelTravelRequest
from gogo_agent.travel.repository import BusinessRepository
from gogo_agent.travel.service import TravelBusinessService
from gogo_agent.travel.tools import build_manage_toolkit

ARGS = {
    "destination": "广州", "departure_city": "北京",
    "departure_date": "2026-12-10", "return_date": "2026-12-12", "purpose": "客户拜访",
}


@pytest.fixture
def service():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    business = TravelBusinessService(BusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)))
    with business.repository.transaction() as session:
        session.add(UserProfileModel(user_id="u001", level="P6"))
    yield business
    engine.dispose()


async def call_submit(service, request_id, **extra):
    toolkit = build_manage_toolkit(service, RequestContext(user_id="u001", session_id="s-idem", request_id=request_id))
    tool = await toolkit.get_tool("submit_travel_approval")
    return json.loads((await tool.call(**ARGS, **extra)).content[0].text)


async def issue_token(service, request_id):
    pending = await call_submit(service, request_id)
    assert pending["errorCode"] == "CONFIRMATION_REQUIRED", pending
    return pending["data"]["confirmation_token"]


def cancel(service, order_id, key):
    return service.cancel_order("u001", order_id, CancelTravelRequest(confirmed=True, idempotency_key=key))


@pytest.mark.asyncio
async def test_same_confirmation_retried_creates_one_order(service):
    token = await issue_token(service, "r1")
    first = await call_submit(service, "r2", confirmation_token=token)
    retry = await call_submit(service, "r3", confirmation_token=token)
    assert first["success"] is True, first
    assert retry["success"] is True, retry
    assert retry["data"]["order_id"] == first["data"]["order_id"]
    assert len(service.list_orders("u001")) == 1


@pytest.mark.asyncio
async def test_new_confirmation_after_cancel_creates_new_order(service):
    t1 = await issue_token(service, "r1")
    first = await call_submit(service, "r2", confirmation_token=t1)
    old_order = first["data"]["order_id"]
    cancel(service, old_order, "cancel-1")
    t2 = await issue_token(service, "r3")
    second = await call_submit(service, "r4", confirmation_token=t2)
    assert second["success"] is True, second
    assert second["data"]["order_id"] != old_order
    assert second["data"]["status"] == "SUBMITTED"


@pytest.mark.asyncio
async def test_old_token_for_cancelled_order_is_not_reported_as_submitted(service):
    t1 = await issue_token(service, "r1")
    first = await call_submit(service, "r2", confirmation_token=t1)
    cancel(service, first["data"]["order_id"], "cancel-2")
    again = await call_submit(service, "r5", confirmation_token=t1)
    assert again["success"] is False, again
    assert again["errorCode"] == "ORDER_NOT_ACTIVE"
    assert "CANCELLED" in again["message"]
