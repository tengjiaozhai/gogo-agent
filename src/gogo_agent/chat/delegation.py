"""记录本轮子 Agent 委派及显式重试关系，不触发自动重试。"""

import json
from collections.abc import Awaitable, Callable
from uuid import uuid4


class DelegationAttempts:
    """用服务端编号关联重试；原始问题只留在本轮内存，重试复用原参数。"""

    def __init__(self, records: list[dict[str, str]], trace_id: str):
        self.records = records
        self.trace_id = trace_id
        self._attempts: dict[str, tuple[dict[str, str], str]] = {}

    async def run(
        self, tool_name: str, agent_name: str, question: str, retry_of: str,
        invoke: Callable[[str], Awaitable[str]],
    ) -> str:
        """执行一次委派；只有引用本轮同工具失败编号的调用才属于原任务的重试。"""
        attempt_id = uuid4().hex
        record = {
            "attempt_id": attempt_id, "task_id": attempt_id,
            "tool_name": tool_name, "agent_name": agent_name,
            "status": "failed", "trace_id": self.trace_id,
        }
        self.records.append(record)
        try:
            if retry_of:
                previous = self._attempts.get(retry_of)
                if (previous is None or previous[0]["tool_name"] != tool_name
                        or previous[0]["status"] != "failed" or question.strip()):
                    raise ValueError("重试须引用本轮同工具的失败编号，且 question 必须留空")
                previous_record, question = previous
                record.update(task_id=previous_record["task_id"], retry_of=retry_of)
            elif not question.strip():
                raise ValueError("首次委派必须提供 question；重试请只填写 retry_of")
            self._attempts[attempt_id] = (record, question)
            answer = await invoke(question)
            record["status"] = "completed"
            return answer
        except BaseException as exc:
            record["error_type"] = type(exc).__name__
            # 取消和进程退出必须继续向上传播，不能伪装成可重试的工具结果。
            if not isinstance(exc, Exception):
                raise
            payload = {
                "success": False, "errorCode": "DELEGATION_FAILED",
                "errorType": record["error_type"],
                "message": "子智能体委派未完成；如需重试，只填写 retry_of，question 留空，服务端将复用原参数。",
            }
            if attempt_id in self._attempts:
                payload["retry_of"] = attempt_id
            else:
                payload["message"] = "委派参数无效：首次调用须提供 question；重试须引用本轮同工具的失败编号且 question 留空。"
            return json.dumps(payload, ensure_ascii=False)
