"""046/047/048/049/058 外部能力测试：全部使用替身（假途牛 CLI、假天气 MCP 服务、模拟传输），不触达真实服务。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from agentscope.mcp import StdioMCPConfig
from pydantic import ValidationError

from gogo_agent.integrations import weather_mcp
from gogo_agent.integrations.catalog import (
    CATALOG, AccessMode, CatalogEntry, Decision, validate_catalog,
)
from gogo_agent.integrations.destination_live import (
    DestinationLiveClient, LiveStatus, render_live_result, simulated_transport,
)
from gogo_agent.integrations.skill_runner import (
    SkillError, SkillErrorCode, SkillRunner, user_dir_name,
)
from gogo_agent.integrations.tuniu import (
    TuniuCliAdapter, normalize_flights, normalize_hotels, normalize_trains, to_candidate_set, tuniu_policy,
)
from gogo_agent.integrations.weather_mcp import (
    WeatherMcpError, WeatherMcpErrorCode, WeatherMcpService,
)
from gogo_agent.planning.models import TransportCandidate

ROOT = Path(__file__).resolve().parents[1]
FAKE_TUNIU = str(ROOT / "scripts" / "fake_tuniu_cli.py")
FAKE_WEATHER = str(ROOT / "scripts" / "fake_weather_mcp_server.py")
KEY = "sk-test-SECRET-9999"
MODE_ENV = ("FAKE_TUNIU_MODE", "FAKE_TUNIU_PIDFILE")
SEARCH = ["call", "flight", "searchLowestPriceFlight", "-a", "{}"]
NOW = datetime(2026, 10, 11, 13, 48, tzinfo=UTC)  # 北京时间 2026-10-11 21:48


def make_runner(base_dir: Path, **updates) -> SkillRunner:
    policy = tuniu_policy(executable=sys.executable, fixed_args=[FAKE_TUNIU], extra_env_names=MODE_ENV)
    return SkillRunner(policy.model_copy(update=updates), base_dir=base_dir)


def mode_env(mode: str, **extra: str) -> dict[str, str]:
    return {"TUNIU_API_KEY": KEY, "FAKE_TUNIU_MODE": mode, **extra}


def make_adapter(base_dir: Path, mode: str = "ok", timeout_s: float = 5.0) -> TuniuCliAdapter:
    return TuniuCliAdapter(
        executable=sys.executable, fixed_args=[FAKE_TUNIU], base_dir=base_dir, timeout_s=timeout_s,
        extra_env={"FAKE_TUNIU_MODE": mode},
    )


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ───────────────────────────── 058 安全运行器 ─────────────────────────────


@pytest.mark.asyncio
async def test_058_isolated_dir_env_whitelist_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("PARENT_ONLY_SECRET", "must-not-reach-child")
    result = await make_runner(tmp_path).run_json(
        user_id="alice@example.com", args=SEARCH, env=mode_env("echo_env"), secrets=[KEY],
    )
    workdir = Path(result.workdir)
    assert workdir.parent.name == user_dir_name("alice@example.com")
    assert "alice" not in result.workdir.replace(str(tmp_path), "")
    assert result.data["cwd"].endswith(workdir.name) and result.data["home"] == result.workdir
    child_env = set(result.data["env_keys"])
    assert "PARENT_ONLY_SECRET" not in child_env
    assert {"HOME", "TMPDIR", "TUNIU_API_KEY", "FAKE_TUNIU_MODE"} <= child_env
    assert list(tmp_path.iterdir()) == []  # 运行目录与空的用户目录都已清理


@pytest.mark.asyncio
async def test_058_two_users_get_different_dirs_and_files_do_not_survive(tmp_path):
    runner = make_runner(tmp_path)
    first = await runner.run_json(user_id="u-1", args=SEARCH, env=mode_env("write_cwd"))
    second = await runner.run_json(user_id="u-2", args=SEARCH, env=mode_env("write_cwd"))
    assert Path(first.workdir).parent.name != Path(second.workdir).parent.name
    assert first.data["wrote"].startswith(first.workdir)  # 子进程只写到自己的 cwd
    assert not Path(first.data["wrote"]).exists() and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_058_timeout_kills_process_tree(tmp_path):
    pidfile = tmp_path / "child.pid"
    started = time.monotonic()
    with pytest.raises(SkillError) as raised:
        await make_runner(tmp_path / "w", timeout_s=1.0).run_json(
            user_id="u", args=SEARCH, env=mode_env("hang", FAKE_TUNIU_PIDFILE=str(pidfile)),
        )
    assert raised.value.code is SkillErrorCode.TIMEOUT
    assert time.monotonic() - started < 6
    grandchild = int(pidfile.read_text())
    for _ in range(40):  # 给内核回收留一点时间
        if not pid_alive(grandchild):
            break
        await asyncio.sleep(0.05)
    assert not pid_alive(grandchild)


@pytest.mark.asyncio
async def test_058_huge_output_aborts_instead_of_reading_everything(tmp_path):
    started = time.monotonic()
    with pytest.raises(SkillError) as raised:
        # 假 CLI 会无限输出：若先读完再截断，这里会一直跑到 30 秒超时
        await make_runner(tmp_path, max_stdout_bytes=4096, timeout_s=30).run_json(
            user_id="u", args=SEARCH, env=mode_env("huge"),
        )
    assert raised.value.code is SkillErrorCode.OUTPUT_TOO_LARGE
    assert time.monotonic() - started < 10
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_058_invalid_json_and_nonzero_exit_mapping(tmp_path):
    runner = make_runner(tmp_path)
    with pytest.raises(SkillError) as bad:
        await runner.run_json(user_id="u", args=SEARCH, env=mode_env("bad_json"))
    assert bad.value.code is SkillErrorCode.INVALID_JSON
    with pytest.raises(SkillError) as failed:
        await runner.run_json(user_id="u", args=SEARCH, env=mode_env("fail"), secrets=[KEY])
    assert failed.value.code is SkillErrorCode.NONZERO_EXIT
    assert failed.value.exit_code == 104 and "认证失败" in failed.value.message


@pytest.mark.asyncio
async def test_058_start_failed_for_missing_executable(tmp_path):
    runner = SkillRunner(
        tuniu_policy(executable="/nonexistent/tuniu-binary"), base_dir=tmp_path,
    )
    with pytest.raises(SkillError) as raised:
        await runner.run_json(user_id="u", args=SEARCH, env={"TUNIU_API_KEY": KEY})
    assert raised.value.code is SkillErrorCode.START_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    ["call", "flight", "searchLowestPriceFlight; rm -rf /"],
    ["call", "flight", "searchLowestPriceFlight && curl evil"],
    ["call", "flight", "$(whoami)"],
    ["call", "visa", "check"],
    ["call", "flight", "saveOrder", "-a", "{}"],
    ["call", "flight", "cancelOrder", "-a", "{}"],
    ["call", "train", "bookTrain", "-a", "{}"],
    ["call", "hotel", "tuniuHotelCreateOrder", "-a", "{}"],
    ["call", "flight", "searchLowestPriceFlight", "saveOrder"],
])
async def test_058_command_not_allowed(tmp_path, args):
    with pytest.raises(SkillError) as raised:
        await make_runner(tmp_path).run_json(user_id="u", args=args, env=mode_env("ok"))
    assert raised.value.code is SkillErrorCode.COMMAND_NOT_ALLOWED
    assert list(tmp_path.iterdir()) == []  # 校验在创建目录、启动进程之前


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    "call flight searchLowestPriceFlight -a {}",
    [],
    SEARCH + ["a"] * 10,
    SEARCH[:4] + ["x" * 5000],
    SEARCH[:4] + ["a\x00b"],
    SEARCH[:4] + ["/etc/passwd"],
    SEARCH[:4] + ["../../secret"],
    SEARCH[:4] + [123],
])
async def test_058_invalid_argument(tmp_path, args):
    with pytest.raises(SkillError) as raised:
        await make_runner(tmp_path).run_json(user_id="u", args=args)
    assert raised.value.code is SkillErrorCode.INVALID_ARGUMENT
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_058_env_and_user_validation(tmp_path):
    runner = make_runner(tmp_path)
    for env in ({"LD_PRELOAD": "x"}, {"HOME": "/tmp/evil"}, {"TUNIU_API_KEY": 5}):
        with pytest.raises(SkillError) as raised:
            await runner.run_json(user_id="u", args=SEARCH, env=env)
        assert raised.value.code is SkillErrorCode.INVALID_ARGUMENT
    with pytest.raises(SkillError) as blank:
        await runner.run_json(user_id="  ", args=SEARCH)
    assert blank.value.code is SkillErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_058_secret_masked_in_error_stderr_and_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SkillError) as raised:
        await make_runner(tmp_path).run_json(user_id="u", args=SEARCH, env=mode_env("fail"), secrets=[KEY])
    error = raised.value
    assert KEY not in str(error) and KEY not in error.message and KEY not in (error.stderr or "")
    assert "***" in (error.stderr or "")
    assert KEY not in caplog.text and caplog.text  # 有日志，但没有密钥


# ───────────────────────────── 049 途牛适配器 ─────────────────────────────


async def search_all(adapter: TuniuCliAdapter):
    common = {"user_id": "alice", "api_key": KEY}
    flights = await adapter.search_flights(
        departure_city="北京", arrival_city="上海", departure_date=date(2026, 3, 15), **common)
    trains = await adapter.search_trains(
        departure_city="北京", arrival_city="上海", departure_date=date(2026, 3, 15), **common)
    hotels = await adapter.search_hotels(
        city="上海", check_in=date(2026, 3, 15), check_out=date(2026, 3, 17), **common)
    return flights, trains, hotels


@pytest.mark.asyncio
async def test_049_fixed_samples_are_normalized(tmp_path):
    flights, trains, hotels = await search_all(make_adapter(tmp_path))
    mu, ca = flights.transports
    assert (mu.code, mu.carrier, mu.price, mu.is_direct, mu.refund_policy) == ("MU5101", "东方航空", 1070.0, True, "不可退")
    assert (mu.origin, mu.destination) == ("北京", "上海")  # 城市名取自请求，而非机场名
    assert ca.price == 950.63 and ca.is_direct is False  # 880.5+70.125=950.625，半数向上而非银行家舍入
    assert [(t.code, t.cabin_class, t.price) for t in trains.transports] == [
        ("G103", "二等座", 553.0), ("G103", "商务座", 1748.0), ("G11", "二等座", 576.0),
    ]  # G103 一等座余票 0 被跳过；G11 无余票字段则保留；D1 无价格被跳过
    assert trains.transports[0].is_direct is True and trains.transports[2].is_direct is False
    assert trains.transports[0].refund_policy == "可退"
    stars = {h.name: h.star_rating for h in hotels.hotels}
    assert stars == {"外滩豪华酒店": 5, "人民广场高档酒店": 4, "火车站经济酒店": 2, "星级未标注酒店": 3}
    assert all(h.nights == 2 and h.distance_to_dest_km == 0 for h in hotels.hotels)
    breakfast = {h.name: h.breakfast_included for h in hotels.hotels}
    assert breakfast["火车站经济酒店"] is True and breakfast["外滩豪华酒店"] is False  # 与 Java 一致：只认“早餐”二字
    assert hotels.hotels[1].price_per_night == 560.5  # 字符串价格也能解析


@pytest.mark.asyncio
async def test_049_candidate_set_numbering_is_stable_and_request_id_kept_apart(tmp_path):
    flights, trains, hotels = await search_all(make_adapter(tmp_path))
    first = to_candidate_set([hotels, trains, flights], captured_at=NOW)
    second = to_candidate_set([flights, hotels, trains], captured_at=NOW)
    assert first == second
    assert first.source == "tuniu_cli" and first.captured_at == NOW
    assert [t.id for t in first.transports] == ["F1", "F2", "T3", "T4", "T5"]  # 机票与火车共用交通计数器
    assert [h.id for h in first.hotels] == ["H1", "H2", "H3", "H4"]
    assert flights.vendor_request_id == "TN-REQ-20260315-0001"
    assert "vendor_request_id" not in TransportCandidate.model_fields  # 请求 ID 不进候选模型
    assert "TN-REQ" not in first.model_dump_json()


def test_049_normalizers_edge_cases():
    assert normalize_flights(None, "北京", "上海") == []
    assert normalize_flights([{"departureTime": "2026-03-15 08:00", "arrivalTime": "2026-03-15 10:00",
                               "basePrice": "abc", "totalTax": None}], "北京", "上海")[0].price == 0.0
    seats = {"price": {"edzPrice": "100", "ydzPrice": "  ", "swzPrice": "300"}, "seatAvailable": {"edzNum": "0", "swzNum": "x"},
             "departureTime": "2026-03-15 08:00", "arrivalTime": "2026-03-15 10:00", "trainNum": "G1"}
    result = normalize_trains([seats], "北京", "上海")
    assert [(t.cabin_class, t.price) for t in result] == [("商务座", 300.0)]  # 余票“0”跳过，空白价格跳过，无法解析的余票保留
    assert normalize_hotels([{"hotelName": "A"}, {"starName": "x"}, "bad"], 3)[0].star_rating == 3
    assert [h.id for h in normalize_hotels([{"hotelName": "A"}, {"starName": "x"}, {"hotelName": "B"}], 1)] == ["H1", "H2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [
    ("fail", SkillErrorCode.NONZERO_EXIT),
    ("bad_json", SkillErrorCode.INVALID_JSON),
    ("vendor_error", SkillErrorCode.VENDOR_ERROR),
])
async def test_049_failures_map_to_errors_without_secret(tmp_path, caplog, mode, code):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SkillError) as raised:
        await make_adapter(tmp_path, mode).search_flights(
            user_id="alice", api_key=KEY, departure_city="北京", arrival_city="上海", departure_date=date(2026, 3, 15))
    assert raised.value.code is code
    assert KEY not in str(raised.value) and KEY not in (raised.value.stderr or "") and KEY not in caplog.text


@pytest.mark.asyncio
async def test_049_timeout_and_missing_key_and_bad_input(tmp_path):
    with pytest.raises(SkillError) as slow:
        await make_adapter(tmp_path, "hang", timeout_s=1.0).search_trains(
            user_id="alice", api_key=KEY, departure_city="北京", arrival_city="上海", departure_date=date(2026, 3, 15))
    assert slow.value.code is SkillErrorCode.TIMEOUT
    adapter = make_adapter(tmp_path)
    with pytest.raises(SkillError) as no_key:
        await adapter.search_flights(user_id="alice", api_key=" ", departure_city="北京", arrival_city="上海",
                                     departure_date=date(2026, 3, 15))
    assert no_key.value.code is SkillErrorCode.INVALID_ARGUMENT
    with pytest.raises(SkillError):  # 退房不晚于入住
        await adapter.search_hotels(user_id="alice", api_key=KEY, city="上海", check_in=date(2026, 3, 15),
                                    check_out=date(2026, 3, 15))
    with pytest.raises(SkillError):  # 城市名含控制字符
        await adapter.search_flights(user_id="alice", api_key=KEY, departure_city="北\n京", arrival_city="上海",
                                     departure_date=date(2026, 3, 15))
    assert list(tmp_path.iterdir()) == []  # 以上都没有启动进程


@pytest.mark.asyncio
async def test_049_adapter_exposes_search_only(tmp_path):
    adapter = make_adapter(tmp_path)
    assert not any(hasattr(adapter, name) for name in ("save_order", "cancel_order", "book_train", "create_order"))
    policy = tuniu_policy()
    assert {command[2] for command in policy.allowed_commands} == {
        "searchLowestPriceFlight", "searchLowestPriceTrain", "tuniuHotelSearch",
    }
    assert {"saveOrder", "cancelOrder", "bookTrain", "tuniuHotelCreateOrder"} <= policy.denied_tokens


# ───────────────────────────── 046 实时查询 ─────────────────────────────


def live_client(mode: str, **kwargs) -> DestinationLiveClient:
    return DestinationLiveClient(simulated_transport(mode), now=lambda: NOW, **kwargs)


@pytest.mark.asyncio
async def test_046_normal_result_carries_source_time_and_validity():
    result = await live_client("normal").query_weather("杭州", date(2026, 10, 12))
    assert result.status is LiveStatus.OK and len(result.items) == 1
    item = result.items[0]
    assert item.source == "wttr.in" and item.fetched_at == NOW
    assert item.valid_until - item.fetched_at == timedelta(hours=3)
    assert "小雨" in item.text and "70%" in item.text
    text = render_live_result(result)
    assert "查到的数据" in text and "来源：wttr.in" in text and "抓取：2026-10-11 21:48" in text
    assert "未验证信息" in text and "有效至：2026-10-12 00:48" in text


@pytest.mark.asyncio
async def test_046_today_or_no_date_includes_current_and_all_days():
    today = await live_client("normal").query_weather("杭州", date(2026, 10, 11))
    assert [i.label for i in today.items] == ["实况", "2026-10-11 预报", "2026-10-12 预报", "2026-10-13 预报"]
    undated = await live_client("normal").query_weather("杭州")
    assert len(undated.items) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,status", [
    ("timeout", LiveStatus.TIMEOUT), ("no_result", LiveStatus.NO_RESULT), ("unavailable", LiveStatus.UNAVAILABLE),
])
async def test_046_failures_never_render_certain_facts(mode, status):
    result = await live_client(mode, weather_timeout_s=0.2).query_weather("杭州")
    assert result.status is status and result.items == []
    text = render_live_result(result)
    assert "查到的数据：无" in text and "未验证信息" in text
    assert "°C" not in text and "最高" not in text and "降雨" not in text.replace("降雨概率", "")
    assert "SECRET-IN-URL" not in result.model_dump_json() and "example.invalid" not in text


@pytest.mark.asyncio
async def test_046_beyond_window_does_not_call_transport_and_signals_mcp():
    calls = []

    async def transport(kind, params):
        calls.append(kind)
        return {}

    client = DestinationLiveClient(transport, now=lambda: NOW)
    edge = await client.query_weather("杭州", date(2026, 10, 13))  # 今天 + 2，仍在窗口内
    assert edge.status is LiveStatus.NO_RESULT and not edge.beyond_range and calls == ["weather"]
    far = await client.query_weather("杭州", date(2026, 10, 14))
    assert far.beyond_range and far.status is LiveStatus.NO_RESULT and calls == ["weather"]
    text = render_live_result(far)
    assert "尚未查询" in text and "长期天气预报" in text


@pytest.mark.asyncio
async def test_046_news_and_validation():
    result = await live_client("normal").query_news("杭州", "traffic")
    assert result.status is LiveStatus.OK and len(result.items) == 2
    assert result.items[0].source == "newsdata.io/city_daily" and result.items[1].source == "newsdata.io"
    assert result.items[1].text == "（无摘要）"
    assert "发布：2026-10-10 09:30:00" in render_live_result(result)
    with pytest.raises(ValueError):
        await live_client("normal").query_news("杭州", "gossip")
    with pytest.raises(ValueError):
        await live_client("normal").query_weather("  ")
    empty = await live_client("no_result").query_news("杭州")
    assert empty.status is LiveStatus.NO_RESULT


@pytest.mark.asyncio
async def test_046_non_object_response_is_unavailable():
    async def transport(kind, params):
        return ["not", "an", "object"]

    result = await DestinationLiveClient(transport, now=lambda: NOW).query_weather("杭州")
    assert result.status is LiveStatus.UNAVAILABLE


# ───────────────────────────── 047 天气 MCP（真实 stdio 协议） ─────────────────────────────


def weather_config(mode: str = "normal", **env: str) -> StdioMCPConfig:
    return StdioMCPConfig(command=sys.executable, args=[FAKE_WEATHER], env={"FAKE_WEATHER_MODE": mode, **env})


WINDOW = (date(2026, 10, 20), date(2026, 10, 22))


@pytest.mark.asyncio
async def test_047_connect_list_call_close_sends_only_city_and_dates(monkeypatch):
    from mcp.client.session import ClientSession

    sent: list[dict] = []
    original = ClientSession.call_tool

    async def spy(self, name, arguments=None, *args, **kwargs):
        sent.append({"name": name, "arguments": dict(arguments or {})})
        return await original(self, name, arguments, *args, **kwargs)

    monkeypatch.setattr(ClientSession, "call_tool", spy)
    service = WeatherMcpService(weather_config(), enabled_tools=["forecast"])
    assert not service.is_connected
    async with service:
        assert service.is_connected
        tools = await service.list_tools()
        assert [t.name for t in tools] == ["forecast"]
        assert sorted(tools[0].input_schema["properties"]) == ["city", "end_date", "start_date"]
        forecast = await service.get_forecast("  杭州 ", *WINDOW)
    assert not service.is_connected
    assert forecast.city == "杭州" and forecast.tool_name == "forecast"
    assert [d["date"] for d in forecast.payload["days"]] == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert forecast.sent_arguments == ["city", "end_date", "start_date"]
    assert sent == [{"name": "forecast", "arguments": {"city": "杭州", "start_date": "2026-10-20", "end_date": "2026-10-22"}}]
    # 签名里根本没有用户、会话、事由等参数位置
    with pytest.raises(TypeError):
        await service.get_forecast("杭州", *WINDOW, user_id="u1")


@pytest.mark.asyncio
async def test_047_whitelist_hides_and_blocks_other_tools():
    async with WeatherMcpService(weather_config(), enabled_tools=["air_quality"]) as service:
        assert [t.name for t in await service.list_tools()] == ["air_quality"]
        with pytest.raises(WeatherMcpError) as raised:
            await service.get_forecast("杭州", *WINDOW)
    assert raised.value.code is WeatherMcpErrorCode.MCP_TOOL_NOT_ALLOWED
    async with WeatherMcpService(weather_config()) as service:  # 不设白名单时两个工具都可见
        assert {t.name for t in await service.list_tools()} == {"forecast", "air_quality"}


@pytest.mark.asyncio
async def test_047_not_connected_and_argument_errors():
    service = WeatherMcpService(weather_config())
    with pytest.raises(WeatherMcpError) as not_connected:
        await service.list_tools()
    assert not_connected.value.code is WeatherMcpErrorCode.MCP_NOT_CONNECTED
    with pytest.raises(WeatherMcpError) as after_close:
        async with service:
            pass
        await service.get_forecast("杭州", *WINDOW)
    assert after_close.value.code is WeatherMcpErrorCode.MCP_NOT_CONNECTED
    async with service:
        for args in (("杭州", WINDOW[1], WINDOW[0]), ("", *WINDOW), ("杭州", date(2026, 10, 1), date(2026, 11, 20))):
            with pytest.raises(WeatherMcpError) as invalid:
                await service.get_forecast(*args)
            assert invalid.value.code is WeatherMcpErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_047_close_is_called_even_when_body_raises():
    service = WeatherMcpService(weather_config(), enabled_tools=["forecast"])
    with pytest.raises(RuntimeError, match="业务异常"):
        async with service:
            assert service.is_connected
            raise RuntimeError("业务异常")
    assert not service.is_connected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [
    ("tool_error", WeatherMcpErrorCode.MCP_TOOL_ERROR),
    ("bad_payload", WeatherMcpErrorCode.MCP_INVALID_RESPONSE),
    ("crash_call", WeatherMcpErrorCode.MCP_UNAVAILABLE),
])
async def test_047_server_side_failures_are_converted(mode, code):
    async with WeatherMcpService(weather_config(mode), enabled_tools=["forecast"], call_timeout_s=3.0) as service:
        with pytest.raises(WeatherMcpError) as raised:
            await service.get_forecast("杭州", *WINDOW)
        assert raised.value.code is code
        if mode == "crash_call":  # 断连之后继续调用仍是明确错误，而不是挂起
            with pytest.raises(WeatherMcpError) as again:
                await service.get_forecast("杭州", *WINDOW)
            assert again.value.code in (WeatherMcpErrorCode.MCP_UNAVAILABLE, WeatherMcpErrorCode.MCP_TIMEOUT)
    assert not service.is_connected


@pytest.mark.asyncio
async def test_047_call_timeout():
    started = time.monotonic()
    async with WeatherMcpService(weather_config("hang_call"), enabled_tools=["forecast"], call_timeout_s=1.0) as service:
        with pytest.raises(WeatherMcpError) as raised:
            await service.get_forecast("杭州", *WINDOW)
        assert time.monotonic() - started < 4  # 调用超时本身不依赖关闭挂起服务的耗时
    assert raised.value.code is WeatherMcpErrorCode.MCP_TIMEOUT


@pytest.mark.asyncio
async def test_047_connect_timeout_and_start_failures():
    service = WeatherMcpService(weather_config("hang_start", FAKE_WEATHER_HANG_S="3"), connect_timeout_s=1.0)
    with pytest.raises(WeatherMcpError) as slow:
        await service.connect()
    assert slow.value.code is WeatherMcpErrorCode.MCP_TIMEOUT and not service.is_connected
    with pytest.raises(WeatherMcpError) as missing:
        await WeatherMcpService(StdioMCPConfig(command="/nonexistent/weather-server")).connect()
    assert missing.value.code is WeatherMcpErrorCode.MCP_UNAVAILABLE
    exits = StdioMCPConfig(command=sys.executable, args=["-c", "import sys; sys.exit(3)"])
    with pytest.raises(WeatherMcpError) as early_exit:
        await WeatherMcpService(exits, connect_timeout_s=5.0).connect()
    assert early_exit.value.code is WeatherMcpErrorCode.MCP_UNAVAILABLE
    await asyncio.sleep(0)  # 此后任务仍可正常调度：取消计数已被恢复
    async with asyncio.timeout(1):
        await asyncio.sleep(0.01)


def test_047_arguments_follow_declared_schema_only():
    values = {"city": "杭州", "start_date": "2026-10-20", "end_date": "2026-10-22"}
    only_city = {"properties": {"city": {}}, "required": ["city"]}
    assert weather_mcp._arguments_for_schema(values, only_city) == {"city": "杭州"}
    for schema in (
        {"properties": {"start_date": {}}, "required": []},  # 没有 city
        {"properties": {"city": {}, "user_id": {}}, "required": ["city", "user_id"]},  # 要求我们不会提供的字段
    ):
        with pytest.raises(WeatherMcpError) as raised:
            weather_mcp._arguments_for_schema(values, schema)
        assert raised.value.code is WeatherMcpErrorCode.MCP_SCHEMA_MISMATCH


# ───────────────────────────── 048 清单 ─────────────────────────────


def entry(entry_id: str) -> CatalogEntry:
    return next(item for item in CATALOG if item.entry_id == entry_id)


def replaced(entry_id: str, **updates) -> list[CatalogEntry]:
    return [item.model_copy(update=updates) if item.entry_id == entry_id else item for item in CATALOG]


def test_048_catalog_is_valid_and_matches_java_facts():
    assert validate_catalog() == []
    tuniu = [item for item in CATALOG if item.entry_id.startswith("tuniu.")]
    assert tuniu and all(item.enabled and "ItineraryPlanAgent" in item.registered_agents for item in tuniu)
    for dormant in ("flightmanager.search", "flightmanager.order", "rollinggo.search", "rollinggo.order",
                    "flyai.flight.search", "flyai.train.search"):
        item = entry(dormant)
        assert not item.enabled and item.decision is Decision.NOT_ENABLED
        assert not item.authoritative and item.python_module is None
    migrated = {item.entry_id for item in CATALOG if item.decision is Decision.MIGRATE_STAGE_E}
    assert migrated == {"tuniu.flight.search", "tuniu.train.search", "tuniu.hotel.search",
                        "wttr.weather.short", "newsdata.news", "weather.mcp.long"}
    assert all(item.access is AccessMode.READ_ONLY for item in CATALOG if item.python_planning_allowed)
    assert all(not item.python_planning_allowed for item in CATALOG if item.access is not AccessMode.READ_ONLY)
    assert all(item.evidence for item in CATALOG)


def test_048_each_enabled_behavior_has_exactly_one_authoritative_entry():
    authoritative: dict[str, list[str]] = {}
    for item in CATALOG:
        if item.authoritative:
            authoritative.setdefault(item.behavior, []).append(item.entry_id)
    assert all(len(ids) == 1 for ids in authoritative.values())
    assert authoritative["hotel.search"] == ["tuniu.hotel.search"]  # Rolling Go 也能搜酒店，但未启用，不能成为权威入口


def test_048_negative_cases_are_detected():
    assert any("未启用" in p or "未注册" in p for p in validate_catalog(
        replaced("rollinggo.search", decision=Decision.MIGRATE_STAGE_E)))
    assert any("权威入口" in p for p in validate_catalog(replaced("rollinggo.search", authoritative=True)))
    assert any("恰有 1 个权威入口" in p for p in validate_catalog(
        replaced("rollinggo.search", authoritative=True, decision=Decision.NOT_ENABLED,
                 registered_agents=("ItineraryPlanAgent",))))
    assert any("没有 Python 模块" in p for p in validate_catalog(replaced("tuniu.hotel.search", python_module=None)))
    assert any("写入类入口" in p for p in validate_catalog(replaced("tuniu.flight.order", python_planning_allowed=True)))
    assert any("只迁移只读" in p for p in validate_catalog(
        replaced("tuniu.flight.order", decision=Decision.MIGRATE_STAGE_E, python_module="x")))
    no_authority = replaced("tuniu.flight.search", authoritative=False)
    assert any("flight.search" in p and "恰有 1 个" in p for p in validate_catalog(no_authority))
    assert any("重复" in p for p in validate_catalog([*CATALOG, entry("tuniu.flight.search")]))
    with pytest.raises(ValidationError):
        CatalogEntry.model_validate({**entry("tuniu.flight.search").model_dump(), "evidence": ()})
