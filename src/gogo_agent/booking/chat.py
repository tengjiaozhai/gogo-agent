"""网页预订的结构化确认边界：凭据只交给认证客户端，模型不能自行确认。"""
from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent.models import ConfidenceLevel, IntentCategory, IntentItem, IntentResult, QueryInput, RecognitionDecision, RecognitionLayer
from gogo_agent.intent.pipeline import PreparedIntentTurn
from gogo_agent.request_context import RequestContext
from gogo_agent.travel import confirmation

from .service import BookingError, BookingService


class BookingConfirmationView(BaseModel):
    """已保存回复中的待确认预订摘要；token 不进入模型上下文。"""
    model_config = ConfigDict(extra="forbid")
    confirmation_id: str = Field(..., description="本次确认摘要标识，用于关联处理结果")
    token: str = Field(..., repr=False, description="服务端签名确认凭据，仅供本人在同会话后续轮次提交")
    plan_id: str = Field(..., description="本人已审核计划标识")
    content_version: int = Field(..., ge=1, description="摘要对应的计划内容版本")
    proposal_tag: str = Field(..., description="用户选定的代表方案标签")
    leg: Literal["outbound", "inbound", "hotel"] = Field(..., description="本次独立确认的去程、返程或酒店")
    biz_type: str = Field(..., description="预订品类：FLIGHT、TRAIN 或 HOTEL")
    title: str = Field(..., description="来自方案计算的预订标题")
    route: str = Field(..., description="本次预订的路线或酒店所在城市")
    start_time: str = Field(..., description="出发时间或入住日期，来自已审核方案")
    end_time: str = Field(..., description="到达时间或退房日期，来自已审核方案")
    amount: str = Field(..., description="本次确认的金额，精确到分")
    currency: str = Field(..., description="金额币种")
    expires_at: int = Field(..., description="确认凭据失效的 Unix 秒时间戳")
    simulated: Literal[True] = Field(default=True, description="本入口仅使用模拟供应商，不实际出票或扣款")

    def digest_fields(self) -> dict[str, Any]:
        """把授权对象与金额绑定到同一域，不能拿差旅申请确认令牌代替预订确认。"""
        return {"action": "supplier_booking", **self.model_dump(exclude={"token", "confirmation_id", "expires_at"})}


class BookingChatTurn:
    """一次认证请求的预订收集器；最终摘要与结果随助手回复一起保存。"""
    def __init__(self, request: RequestContext, history: ChatHistoryService, service: Callable[[], BookingService]):
        self.request = request
        self.history = history
        self.service = service
        self.pending: list[BookingConfirmationView] = []
        self.confirmed: BookingConfirmationView | None = None
        self.results: list[dict[str, Any]] = []
        self.other_answers: list[str] = []
        self.denials: list[str] = []
        self.advisories: list[str] = []  # 审核警告：不阻止预订，仅随待确认摘要告知用户
        self.query_results: list[dict[str, Any]] = []

    def prepare(self, plan_id: str, proposal_tag: str, leg: str) -> dict[str, Any]:
        """仅取得门禁通过的摘要；不执行供应商写入。"""
        if self.confirmed is not None:
            raise BookingError("CONFIRMATION_SCOPE", "本轮只能执行网页已确认的预订项，其他项目须另行确认")
        summary = self.service().check_ready(self.request.user_id, plan_id, proposal_tag=proposal_tag, leg=leg)
        existing = next((item for item in self.pending if item.plan_id == plan_id and item.proposal_tag == proposal_tag and item.leg == leg), None)
        if existing is None:
            item = BookingConfirmationView(confirmation_id="pending", token="pending", **summary,
                                           expires_at=int(time.time()) + confirmation.DEFAULT_TTL_SECONDS)
            token = confirmation.issue(self.request.user_id, confirmation.draft_digest(self.request.user_id, item.digest_fields()),
                                       session_id=self.request.session_id, request_id=self.request.request_id)
            item = item.model_copy(update={"token": token, "confirmation_id": sha256(token.encode()).hexdigest()[:24]})
            self.pending.append(item)
        return {"success": False, "errorCode": "CONFIRMATION_REQUIRED", "data": summary,
                "message": "门禁通过。请在网页确认卡核对本项金额后点击确认；本轮尚未预订，供应商为模拟环境"}

    def verify_client_confirmation(self, token: str) -> None:
        """只接受同用户同会话已保存回复中的签名摘要；新旧版本或金额不符立即拒绝。"""
        found = None
        for message in reversed(self.history.list_messages(self.request.session_id, self.request.user_id)):
            if message.role != "agent":
                continue
            for raw in (message.extra or {}).get("booking_confirmations", []):
                if raw.get("token") == token:
                    try:
                        found = BookingConfirmationView.model_validate(raw)
                    except ValidationError:
                        raise confirmation.ConfirmationError("CONFIRMATION_STALE", "旧摘要已失效，请重新发起预订并核对完整信息") from None
                    break
            if found is not None:
                break
        if found is None:
            raise confirmation.ConfirmationError("CONFIRMATION_INVALID", "确认摘要不存在、未保存或不属于当前会话，请重新发起预订")
        current = self.service().check_ready(self.request.user_id, found.plan_id, proposal_tag=found.proposal_tag, leg=found.leg)
        current_view = found.model_copy(update=current)
        confirmation.verify(token, self.request.user_id, confirmation.draft_digest(self.request.user_id, current_view.digest_fields()),
                            session_id=self.request.session_id, request_id=self.request.request_id)
        self.confirmed = found

    def prepared_turn(self, message: str) -> PreparedIntentTurn:
        """结构化确认是确定性业务路由，不再让自由文本识别改变授权对象。"""
        assert self.confirmed is not None
        item = IntentItem(intent=IntentCategory.BOOKING, confidence=ConfidenceLevel.HIGH,
                          reason="认证客户端提交了已保存摘要的有效确认凭据", evidence=["结构化预订确认"])
        result = IntentResult(intents=[item], primary_intent=IntentCategory.BOOKING, multi_intent=False,
                              overall_reason="服务端校验通过的结构化确认，只开放对应预订能力")
        decision = RecognitionDecision(result=result, hit_layer=RecognitionLayer.RULE, confidence=ConfidenceLevel.HIGH,
                                       vector_threshold=None, candidates=[], attempted_layers=[RecognitionLayer.RULE], reason=item.reason)
        query = QueryInput(question=message)
        return PreparedIntentTurn(branch="confirmed_booking", original_question=query, effective_question=query,
                                  fast_decision=decision, decision=decision, rewrite=None)

    def execute(self) -> dict[str, Any]:
        """模型无参数执行工具；对象、金额与身份只能来自本轮服务端已验证确认。"""
        if self.confirmed is None:
            raise BookingError("CONFIRMATION_REQUIRED", "请先在网页核对摘要并明确确认")
        if self.results:
            return self.results[-1]
        item = self.confirmed
        try:
            outcome = self.service().book(self.request.user_id, item.plan_id, proposal_tag=item.proposal_tag,
                                          leg=item.leg, confirmed=True, expected_content_version=item.content_version,
                                          conversation_id=self.request.session_id)
            resolution, order_no, booking_id = outcome.resolution, outcome.external_order_no, outcome.booking_id
            detail = outcome.message
        except BookingError as exc:
            resolution, order_no, booking_id, detail = ("UNKNOWN" if exc.code in {"CONCURRENT_WRITE", "BOOKING_RESULT_UNKNOWN"} else "FAILED"), None, None, str(exc)
        data = {"type": "booking_result", "confirmationId": item.confirmation_id,
                "bizType": item.biz_type.lower(), "bizTypeLabel": {"FLIGHT": "机票", "TRAIN": "火车票", "HOTEL": "酒店"}.get(item.biz_type, "预订"),
                "bookingId": booking_id, "orderId": order_no or booking_id or item.confirmation_id,
                "title": item.title, "route": item.route, "startTime": item.start_time, "endTime": item.end_time, "amount": item.amount, "currency": item.currency, "payUrl": None,
                "resolution": resolution, "simulated": True, "detail": detail,
                "statusLabel": {"CONFIRMED": "模拟预订已确认", "FAILED": "预订未完成", "UNKNOWN": "结果未知，待核实"}[resolution]}
        self.results.append(data)
        return data

    @property
    def timeline(self) -> list[dict[str, Any]]:
        return ([{"kind": "booking_confirmation", "data": item.model_dump()} for item in self.pending]
                + [{"kind": "booking", "data": item} for item in self.results])

    def reply_text(self, model_text: str, prepared: PreparedIntentTurn) -> str:
        """预订单一事项的正文由已验证业务事实生成，防止模型把未知结果说成成功。"""
        if self.confirmed is not None and not self.results:
            raise BookingError("BOOKING_NOT_EXECUTED", "本轮确认未取得可用执行结果，请核实原预订或重试同一确认，不要创建新订单")
        result = prepared.decision.result if prepared.decision else None
        has_booking = result is not None and any(i.intent == IntentCategory.BOOKING for i in result.intents)
        if not has_booking:
            return model_text
        if self.results:
            canonical = "\n".join(f"{item['statusLabel']}：{item['title']}，{item['amount']} {item['currency']}。{item['detail']}。这是模拟供应商，不实际出票或扣款。" for item in self.results)
        elif self.pending:
            canonical = "已校验方案与差旅审批，请在下方逐项核对金额并点击确认预订。本轮尚未下单；使用模拟供应商，不实际出票或扣款。"
            if self.advisories:
                canonical += "\n审核建议（不影响预订，可据此调整）：" + "；".join(self.advisories) + "。"
        elif self.query_results:
            labels: dict[str | None, str] = {"CONFIRMED": "模拟预订已确认", "UNKNOWN": "结果待核实", "FAILED": "预订未完成"}
            canonical = "本人预订查询（本轮未重新下单）：\n" + "\n".join(
                f"{row.get('title') or row['booking_id']}：{labels.get(row.get('resolution'), row['status'])}，{row.get('total_amount') or '0'} {row.get('currency') or 'CNY'}，编号 {row.get('external_order_no') or row['booking_id']}"
                for row in self.query_results)
        else:
            canonical = "本轮未下单。" + ("；".join(self.denials) or "请明确本人已审批并已审核的计划，以及要预订的去程、返程或酒店。")
        return "\n\n".join([*self.other_answers, canonical])
