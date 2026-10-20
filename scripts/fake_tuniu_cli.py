"""模拟途牛 CLI（纯标准库）：固定样例输出，并可按环境变量模拟各种失败。

只实现 `call <server> <tool> -a <JSON>` 的搜索类工具；任何其他子命令（含下单、取消）都以退出码 102 拒绝。
这是测试替身，不代表真实途牛服务的任何行为或响应字段是否完整。

环境变量：
- TUNIU_API_KEY    缺失或为空时退出码 108（与 SKILL 文档的“未配置 API Key”一致）。
- FAKE_TUNIU_MODE  ok（默认）| fail（退出码 104，stderr 回显 Key 用于验证脱敏）| bad_json | hang（留下孙进程并挂起）
                   | huge（无限输出直到被杀）| vendor_error（退出码 0 但 success=false）| echo_env | write_cwd
- FAKE_TUNIU_PIDFILE  hang 模式下把孙进程 PID 写到该路径，便于测试确认进程树已被杀。
- FAKE_TUNIU_ECHO_DATES=1  机票与火车样例的日期改为请求中的 departureDate（仅供 043 规划测试使用）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

REQUEST_ID = "TN-REQ-20260315-0001"

FLIGHTS = [
    {
        "flightNumber": "MU5101", "airlineCompany": "东方航空", "departureTime": "2026-03-15 06:50",
        "arrivalTime": "2026-03-15 09:05", "departureAirport": "首都国际机场", "arrivalAirport": "虹桥国际机场",
        "basePrice": "1020", "totalTax": "50", "cabinClass": "经济舱", "type": "直飞",
        "queryId": "internal-1", "airComImageUrl": "https://example.invalid/logo.png",
    },
    {
        "flightNumber": "CA1832", "airlineCompany": "中国国航", "departureTime": "2026-03-15 13:20",
        "arrivalTime": "2026-03-15 17:40", "departureAirport": "首都国际机场", "arrivalAirport": "浦东国际机场",
        "basePrice": "880.5", "totalTax": "70.125", "cabinClass": "经济舱", "type": "中转",
    },
]

TRAINS = [
    {
        "trainNum": "G103", "trainType": "direct", "departureTime": "2026-03-15 07:00",
        "arrivalTime": "2026-03-15 11:29", "departStationName": "北京南", "destStationName": "上海虹桥",
        "price": {"edzPrice": "553", "ydzPrice": 933.0, "swzPrice": "1748"},
        "seatAvailable": {"edzNum": "10", "ydzNum": 0, "swzNum": 3},
    },
    {
        "trainNum": "G11", "trainType": "transfer", "departureTime": "2026-03-15 08:00",
        "arrivalTime": "2026-03-15 12:40", "departStationName": "北京南", "destStationName": "上海虹桥",
        "price": {"edzPrice": 576},
    },
    {"trainNum": "D1", "departureTime": "2026-03-15 09:00", "arrivalTime": "2026-03-15 20:00"},
]

HOTELS = [
    {
        "hotelId": "H-1001", "hotelName": "外滩豪华酒店", "brandName": "示例集团", "starName": "豪华型",
        "roomName": "高级大床房", "lowestPrice": 820, "meal": "含双早", "refund": "入住前一日18点前免费取消",
    },
    {
        "hotelId": "H-1002", "hotelName": "人民广场高档酒店", "brandName": "示例连锁", "starName": "高档型",
        "roomName": "商务双床房", "lowestPrice": "560.5", "meal": "无早", "refund": "不可取消",
    },
    {
        "hotelId": "H-1003", "hotelName": "火车站经济酒店", "starName": "经济型", "roomName": "标准间",
        "lowestPrice": 238, "meal": "含早餐", "refund": "",
    },
    {"hotelId": "H-1004", "hotelName": "星级未标注酒店", "roomName": "大床房", "lowestPrice": 400},
]


def _with_date(items: list[dict], day: str) -> list[dict]:
    """把样例时间的日期部分（固定为 2026-03-15）替换为请求日期，其余字段不变。"""
    out = []
    for item in items:
        copy = dict(item)
        for key in ("departureTime", "arrivalTime"):
            if isinstance(copy.get(key), str) and copy[key].startswith("2026-03-15"):
                copy[key] = day + copy[key][len("2026-03-15"):]
        out.append(copy)
    return out


def emit(inner: dict, *, success: bool = True) -> None:
    """按真实 CLI 的外壳输出：result.content[0].text 是业务 JSON 字符串。"""
    body = {
        "success": success,
        "result": {"content": [{"type": "text", "text": json.dumps(inner, ensure_ascii=False)}], "structuredContent": inner},
        "metadata": {"requestId": REQUEST_ID},
    }
    print(json.dumps(body, ensure_ascii=False))


def main(argv: list[str]) -> int:
    mode = os.environ.get("FAKE_TUNIU_MODE", "ok")
    key = os.environ.get("TUNIU_API_KEY", "")
    if mode == "echo_env":
        print(json.dumps({"cwd": os.getcwd(), "home": os.environ.get("HOME"), "env_keys": sorted(os.environ)}))
        return 0
    if mode == "write_cwd":
        with open("leftover.txt", "w", encoding="utf-8") as handle:
            handle.write("residue")
        print(json.dumps({"wrote": os.path.abspath("leftover.txt")}))
        return 0
    if not key:
        print(json.dumps({"success": False, "error": {"type": "AuthError", "message": "未配置 API Key", "code": 108}}))
        return 108
    if mode == "fail":
        sys.stderr.write(f"认证失败：key={key}\n")
        print(json.dumps({"success": False, "error": {"type": "AuthError", "message": f"bad key {key}", "code": 104}}))
        return 104
    if mode == "bad_json":
        print("this is not json <html>")
        return 0
    if mode == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
        pidfile = os.environ.get("FAKE_TUNIU_PIDFILE")
        if pidfile:
            with open(pidfile, "w", encoding="utf-8") as handle:
                handle.write(str(child.pid))
        time.sleep(3600)
        return 0
    if mode == "huge":
        chunk = "x" * 65536
        while True:
            sys.stdout.write(chunk)
            sys.stdout.flush()
    if mode == "vendor_error":
        print(json.dumps({"success": False, "error": {"type": "ToolError", "message": f"供应商拒绝 {key}", "code": 199}}))
        return 0

    if len(argv) < 5 or argv[0] != "call" or argv[3] != "-a":
        print(json.dumps({"success": False, "error": {"type": "UsageError", "message": "仅支持 call <server> <tool> -a <json>", "code": 103}}))
        return 103
    server, tool = argv[1], argv[2]
    payload = json.loads(argv[4]) if argv[4].strip().startswith("{") else {}
    day = payload.get("departureDate")
    if os.environ.get("FAKE_TUNIU_ECHO_DATES") == "1" and day:
        # 测试开关：把固定样例的日期换成请求日期，使往返规划的时间关系可用。
        if (server, tool) == ("flight", "searchLowestPriceFlight"):
            emit({"data": _with_date(FLIGHTS, day), "queryId": "q-flight"})
            return 0
        if (server, tool) == ("train", "searchLowestPriceTrain"):
            emit({"data": _with_date(TRAINS, day), "queryId": "q-train"})
            return 0
    if (server, tool) == ("flight", "searchLowestPriceFlight"):
        emit({"data": FLIGHTS, "queryId": "q-flight"})
    elif (server, tool) == ("train", "searchLowestPriceTrain"):
        emit({"data": TRAINS, "queryId": "q-train"})
    elif (server, tool) == ("hotel", "tuniuHotelSearch"):
        emit({"hotels": HOTELS, "queryId": "q-hotel"})
    else:
        print(json.dumps({"success": False, "error": {"type": "ToolNotFoundError", "message": "工具不存在", "code": 102}}))
        return 102
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
