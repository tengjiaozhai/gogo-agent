"""网页聊天的模拟预订装配；真实供应商未接入，不根据模型参数切换提供方。"""
from gogo_agent.planning.dependencies import get_plan_service
from gogo_agent.travel.dependencies import get_travel_service

from .service import BookingService
from .supplier import SimulatedSupplier

_supplier = SimulatedSupplier()


def get_booking_service() -> BookingService:
    """复用同一模拟供应商与权威业务仓储；供应商订单只在当前单进程生命周期内保留。"""
    return BookingService(get_travel_service(), get_plan_service(), _supplier)
