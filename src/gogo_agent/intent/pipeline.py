"""017 原句快筛、条件改写和完整识别的单一应用入口。"""

from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import RewriteContextBuilder
from .models import QueryInput, RecognitionDecision, RewriteResult
from .service import IntentRecognizer, QueryRewriter


class PreparedIntentTurn(BaseModel):
    """已保存的本轮问题经过快筛和必要改写后的可调度结果。"""

    model_config = ConfigDict(extra="forbid")

    branch: Literal["fast", "rewritten", "needs_context"] = Field(
        ..., description="本轮实际走过的快速命中、改写识别或待补上下文分支"
    )
    original_question: QueryInput = Field(..., description="业务历史中已保存的用户原问题")
    effective_question: QueryInput | None = Field(
        ..., description="交给当前主 Agent 的独立问题；待补上下文时为空"
    )
    fast_decision: RecognitionDecision = Field(..., description="原问题的 L0/L1/L2 快速识别记录")
    rewrite: RewriteResult | None = Field(..., description="未快速命中时唯一一次问题改写结果")
    decision: RecognitionDecision | None = Field(
        ..., description="最终意图识别结果；待补上下文时为空"
    )

    @model_validator(mode="after")
    def validate_branch(self) -> Self:
        """防止把未完成改写或空识别结果交给主 Agent。"""
        if self.branch == "fast":
            if self.fast_decision.result is None or self.rewrite is not None:
                raise ValueError("快速分支必须命中且不能调用改写")
            if self.effective_question != self.original_question or self.decision != self.fast_decision:
                raise ValueError("快速分支必须保留原问题和快速识别结果")
        elif self.branch == "rewritten":
            if self.fast_decision.result is not None or self.rewrite is None:
                raise ValueError("改写分支只用于快速未命中")
            if self.rewrite.rewritten_question is None or self.decision is None:
                raise ValueError("完整识别前必须取得可用的独立问题")
            if self.effective_question is None or self.effective_question.question != self.rewrite.rewritten_question:
                raise ValueError("调度问题必须来自本次改写")
            if self.decision.result is None:
                raise ValueError("完整识别不得交付空意图")
        else:
            if (
                self.fast_decision.result is not None
                or self.rewrite is None
                or self.rewrite.rewritten_question is not None
                or self.effective_question is not None
                or self.decision is not None
            ):
                raise ValueError("待补上下文时不得识别或调度")
        return self


class IntentPipelineService:
    """从已保存消息构造可信历史，按 Java 新请求顺序准备主 Agent 输入。"""

    def __init__(
        self,
        context_builder: RewriteContextBuilder,
        rewriter: QueryRewriter,
        recognizer: IntentRecognizer,
    ) -> None:
        self._context_builder = context_builder
        self._rewriter = rewriter
        self._recognizer = recognizer

    async def prepare(
        self,
        session_id: str,
        user_id: str,
        *,
        current_message_id: str,
        reference_date: date | None = None,
    ) -> PreparedIntentTurn:
        """先原句快筛；未命中时只改写一次，再用完整模式识别。"""
        context = self._context_builder.build_rewrite_context(
            session_id, user_id,
            current_message_id=current_message_id,
            reference_date=reference_date,
        )
        fast = await self._recognizer.recognize(context.query, fast_only=True)
        if fast.result is not None:
            return PreparedIntentTurn(
                branch="fast",
                original_question=context.query,
                effective_question=context.query,
                fast_decision=fast,
                rewrite=None,
                decision=fast,
            )

        rewrite = await self._rewriter.rewrite(context)
        if rewrite.rewritten_question is None:
            return PreparedIntentTurn(
                branch="needs_context",
                original_question=context.query,
                effective_question=None,
                fast_decision=fast,
                rewrite=rewrite,
                decision=None,
            )

        effective = QueryInput(question=rewrite.rewritten_question)
        decision = await self._recognizer.recognize(effective)
        return PreparedIntentTurn(
            branch="rewritten",
            original_question=context.query,
            effective_question=effective,
            fast_decision=fast,
            rewrite=rewrite,
            decision=decision,
        )
