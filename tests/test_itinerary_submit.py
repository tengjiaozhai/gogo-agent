"""差旅提交携带用户手工行程时，服务端校验、生成计划并关联差旅单的验收。"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gogo_agent.db.base import Base
from gogo_agent.db.models import UserProfileModel
from gogo_agent.planning.itinerary import ItineraryBinder
from gogo_agent.planning.service import PlanService
from gogo_agent.planning.store import create_plan_repository
from gogo_agent.request_context import RequestContext
from gogo_agent.travel.repository import BusinessRepository
from gogo_agent.travel.service import TravelBusinessService
from gogo_agent.travel.tools import build_manage_toolkit

SUBMIT_ARGS = {
    "destination": "广州", "departure_city": "北京",
    "departure_date": "2026-12-10", "return_date": "2026-12-12", "purpose": "客户拜访",
}


def itinerary(
    outbound_price: float = 1000.0, outbound_departure: str = "2026-12-10T16:45",
    outbound_arrival: str = "2026-12-10T19:50",
) -> str:
    """与用户贴出的摘要一致：去程 1000 元，酒店 2 晚各 273 元，返程 1020 元，合计 2566 元。"""
    return json.dumps({
        "outbound": {
            "type": "flight", "carrier": "东航", "code": "MU6309",
            "departure_time": outbound_departure, "arrival_time": outbound_arrival, "price": outbound_price,
        },
        "hotel": {
            "name": "维也纳酒店·广州惠福西北京路步行街店", "check_in": "2026-12-10", "check_out": "2026-12-12",
            "price_per_night": 273.0, "breakfast_included": True,
        },
        "inbound": {
            "type": "flight", "carrier": "深航", "code": "ZH9147",
            "departure_time": "2026-12-12T07:30", "arrival_time": "2026-12-12T10:20", "price": 1020.0,
        },
    }, ensure_ascii=False)


def ctx(session_id: str, request_id: str) -> RequestContext:
    return RequestContext(user_id="u001", session_id=session_id, request_id=request_id)


@pytest.fixture
def service():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    business = TravelBusinessService(BusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)))
    with business.repository.transaction() as session:
        session.add(UserProfileModel(user_id="u001", level="P6"))
    yield business
    engine.dispose()


@pytest.fixture
def plans():
    return PlanService(create_plan_repository())


async def submit(service, plans, session_id, request_id, **extra):
    toolkit = build_manage_toolkit(service, ctx(session_id, request_id), itinerary=ItineraryBinder(plans))
    tool = await toolkit.get_tool("submit_travel_approval")
    return json.loads((await tool.call(**SUBMIT_ARGS, **extra)).content[0].text)


@pytest.mark.asyncio
async def test_itinerary_is_bound_to_plan_after_confirmation(service, plans):
    pending = await submit(service, plans, "s1", "r1", itinerary_json=itinerary())
    assert pending["errorCode"] == "CONFIRMATION_REQUIRED"
    assert pending["data"]["itinerary"]["hotel"]["name"] == "维也纳酒店·广州惠福西北京路步行街店"
    assert service.list_orders("u001") == []
    assert plans.list_for_session("u001", "s1") == []

    done = await submit(service, plans, "s1", "r2", itinerary_json=itinerary(),
                        confirmation_token=pending["data"]["confirmation_token"])
    assert done["success"] is True, done
    order_id, plan_id = done["data"]["order_id"], done["data"]["plan_id"]

    plan = plans.get("u001", plan_id)
    assert plan.order_id == order_id
    assert plan.candidates.source == "user_provided"
    assert plan.candidates.hotels[0].nights == 2
    assert plan.result is not None and plan.result.proposals
    assert len(service.list_orders("u001")) == 1


@pytest.mark.asyncio
async def test_itinerary_date_mismatch_is_rejected_before_any_write(service, plans):
    result = await submit(service, plans, "s1", "r1", itinerary_json=itinerary(
        outbound_departure="2026-12-11T16:45", outbound_arrival="2026-12-11T19:50",
    ))
    assert result["errorCode"] == "ITINERARY_DATE_MISMATCH"
    assert "2026-12-10" in result["message"] and "2026-12-11" in result["message"]
    assert service.list_orders("u001") == []
    assert plans.list_for_session("u001", "s1") == []


@pytest.mark.asyncio
async def test_malformed_itinerary_names_the_bad_field(service, plans):
    bad = json.loads(itinerary())
    bad["outbound"]["price"] = -5
    result = await submit(service, plans, "s1", "r1", itinerary_json=json.dumps(bad))
    assert result["errorCode"] == "ITINERARY_INVALID"
    assert "outbound.price" in result["message"]
    assert service.list_orders("u001") == []


@pytest.mark.asyncio
async def test_changed_itinerary_invalidates_confirmation_token(service, plans):
    pending = await submit(service, plans, "s1", "r1", itinerary_json=itinerary(outbound_price=1000.0))
    token = pending["data"]["confirmation_token"]
    result = await submit(service, plans, "s1", "r2", itinerary_json=itinerary(outbound_price=1100.0),
                          confirmation_token=token)
    assert result["errorCode"] == "CONFIRMATION_STALE"
    assert service.list_orders("u001") == []


@pytest.mark.asyncio
async def test_submit_without_itinerary_is_unchanged(service, plans):
    pending = await submit(service, plans, "s1", "r1")
    done = await submit(service, plans, "s1", "r2", confirmation_token=pending["data"]["confirmation_token"])
    assert done["success"] is True, done
    assert "plan_id" not in done["data"]
    assert plans.list_for_session("u001", "s1") == []
    assert len(service.list_orders("u001")) == 1
