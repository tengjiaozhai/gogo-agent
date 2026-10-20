"""061–063 供应商边界：下单与取消的唯一外部适配接口，以及模拟供应商。

真实供应商（途牛 CLI 的下单命令等）未在本仓库接入；这里的模拟供应商只用于练习与测试，
它会记录调用次数与订单，并可按脚本模拟“被拒绝”“响应丢失但订单已生成”等结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from gogo_agent.travel.models import BookingView


class SupplierRejected(Exception):
    """供应商明确拒绝，本地可以安全地记为失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SupplierUnknownOutcome(Exception):
    """超时、连接断开或响应丢失：供应商可能已经生成订单，本地不能宣称成功也不能盲目重试。"""


class SupplierOrder(BaseModel):
    """供应商确认的订单事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(..., description="供应商侧记录的业务请求 ID，即本地预订标识")
    order_no: str = Field(..., description="供应商订单号")
    status: str = Field(..., description="供应商侧订单状态，如 CONFIRMED 或 CANCELLED")
    amount: Decimal = Field(..., description="供应商确认的金额，元")
    currency: str = Field(..., description="金额币种")


class SupplierGateway(Protocol):
    """供应商适配接口：创建、查询与取消必须以请求 ID 去重。"""

    def create_order(self, request_id: str, payload: dict[str, Any]) -> SupplierOrder:
        """创建订单；同一 request_id 重复到达时必须返回同一订单，不得生成第二单。"""
        ...

    def lookup(self, request_id: str) -> SupplierOrder | None:
        """按请求 ID 查询供应商侧结果，用于解决未知结果。"""
        ...

    def cancel(self, order_no: str, reason: str | None) -> bool:
        """取消供应商订单；已取消的订单再次取消仍返回 True。"""
        ...


@dataclass
class SimulatedSupplier:
    """模拟供应商：modes 依次决定每次 create_order 的行为，耗尽后全部按 ok 处理。

    - ok：生成订单并返回
    - reject：不生成订单，抛出 SupplierRejected
    - lost_after_commit：生成订单，但响应丢失（SupplierUnknownOutcome）
    - lost_before_commit：未生成订单，响应丢失（SupplierUnknownOutcome）
    """

    modes: list[str] = field(default_factory=list)
    orders: dict[str, SupplierOrder] = field(default_factory=dict)
    create_calls: int = 0
    cancel_calls: int = 0
    _sequence: int = 0

    def _next_mode(self) -> str:
        if self.modes:
            return self.modes.pop(0)
        return "ok"

    def create_order(self, request_id: str, payload: dict[str, Any]) -> SupplierOrder:
        self.create_calls += 1
        existing = self.orders.get(request_id)
        if existing is not None:
            return existing
        mode = self._next_mode()
        if mode == "reject":
            raise SupplierRejected("SOLD_OUT", "模拟供应商：库存不足")
        if mode == "lost_before_commit":
            raise SupplierUnknownOutcome("模拟供应商：响应丢失，订单未生成")
        self._sequence += 1
        order = SupplierOrder(
            request_id=request_id,
            order_no=f"SUP{self._sequence:06d}",
            status="CONFIRMED",
            amount=Decimal(str(payload["amount"])),
            currency=str(payload.get("currency", "CNY")),
        )
        self.orders[request_id] = order
        if mode == "lost_after_commit":
            raise SupplierUnknownOutcome("模拟供应商：订单已生成，但响应丢失")
        return order

    def lookup(self, request_id: str) -> SupplierOrder | None:
        return self.orders.get(request_id)

    def cancel(self, order_no: str, reason: str | None) -> bool:
        self.cancel_calls += 1
        for request_id, order in list(self.orders.items()):
            if order.order_no == order_no:
                self.orders[request_id] = SupplierOrder(
                    request_id=order.request_id, order_no=order.order_no, status="CANCELLED",
                    amount=order.amount, currency=order.currency,
                )
                return True
        raise SupplierRejected("ORDER_NOT_FOUND", "模拟供应商：订单不存在")


class SupplierCancelGateway:
    """把供应商取消接入阶段 D 的取消服务；未知结果映射为未取消，本地状态保持不变。"""

    def __init__(self, supplier: SupplierGateway) -> None:
        self.supplier = supplier

    def cancel(self, booking: BookingView, reason: str | None) -> bool:
        if not booking.external_order_no:
            return False
        try:
            return self.supplier.cancel(booking.external_order_no, reason)
        except SupplierRejected:
            return False
        except SupplierUnknownOutcome:
            return False
