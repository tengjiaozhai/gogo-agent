"""模拟天气 MCP 服务（stdio，基于 mcp 包的 FastMCP）：固定输出，并可按环境变量模拟故障。

这是测试替身，不是真实云市场天气服务；真实服务的工具名、入参和返回字段需另行核对。

工具：
- forecast(city, start_date, end_date)：返回固定的每日预报；入参声明只有这三项，用于验证客户端不额外发送隐私字段。
- air_quality(city)：用来演示工具白名单（调用方只启用 forecast 时它不可见、不可调用）。

环境变量 FAKE_WEATHER_MODE：
- normal（默认）
- hang_start   启动前先睡眠（FAKE_WEATHER_HANG_S 秒，默认 8），复现连接超时
- hang_call    forecast 一直不返回，复现调用超时
- crash_call   forecast 被调用时进程直接退出，复现中途断连
- tool_error   forecast 抛异常，MCP 返回 isError
- bad_payload  forecast 返回非 JSON 文本
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

MODE = os.environ.get("FAKE_WEATHER_MODE", "normal")
if MODE == "hang_start":
    # 只睡有限时间：握手超时后 AgentScope 2.0.8 可能不会清理子进程，睡满即自行退出，避免测试遗留进程。
    time.sleep(float(os.environ.get("FAKE_WEATHER_HANG_S", "8")))

server = FastMCP("fake-weather", log_level="WARNING")


@server.tool()
def forecast(city: str, start_date: str, end_date: str) -> str:
    """查询城市在日期范围内的天气预报（日期格式 YYYY-MM-DD）。"""
    if MODE == "hang_call":
        time.sleep(3600)
    if MODE == "crash_call":
        os._exit(3)
    if MODE == "tool_error":
        raise ValueError("模拟的上游服务错误")
    if MODE == "bad_payload":
        return "<html>not json</html>"
    first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
    days = []
    current = first
    while current <= last:
        offset = (current - first).days
        days.append({
            "date": current.isoformat(),
            "condition": ("晴", "多云", "小雨")[offset % 3],
            "max_c": 22 - offset,
            "min_c": 14 - offset,
        })
        current += timedelta(days=1)
    return json.dumps(
        {
            "city": city,
            "source": "fake-weather-mcp",
            "days": days,
        },
        ensure_ascii=False,
    )


@server.tool()
def air_quality(city: str) -> str:
    """查询城市空气质量（仅用于演示工具白名单）。"""
    return json.dumps({"city": city, "aqi": 42}, ensure_ascii=False)


if __name__ == "__main__":
    server.run(transport="stdio")
    sys.exit(0)
