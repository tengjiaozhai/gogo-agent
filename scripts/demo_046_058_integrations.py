"""阶段 E 外部能力演示：046 实时查询、047 天气 MCP、048 清单、049 途牛搜索、058 安全运行器。

逐断点表、Java 对照、练习与答案见 docs/契约样例/阶段E-046-049-058-调试指南.md。
所有外部服务都是替身：scripts/fake_tuniu_cli.py（假途牛 CLI）、scripts/fake_weather_mcp_server.py（假天气 MCP）、
destination_live.simulated_transport（假 wttr.in/NewsData）。不会连接任何真实服务，也不会下单。

最短运行路线（仓库根目录，不需要网络、模型密钥或数据库）：
    PYTHONPATH=src .venv/bin/python scripts/demo_046_058_integrations.py --case all
单项：--case 046 | 047 | 048 | 049 | 058。047 会真的拉起假 MCP 服务子进程，关闭挂起的服务约需 5 秒。
稳定输出：形如 `[058] timeout code=TIMEOUT child_alive=False` 的键值行；每次运行会变化的只有耗时类数字
（本脚本不打印它们）与临时目录里的随机 r_xxxx 后缀（本脚本只打印哈希目录名）。

建议断点（函数名比行号稳定）：
- SkillRunner.run_json / _collect / _validate_args（058）；TuniuCliAdapter._call、normalize_trains、to_candidate_set（049）
- DestinationLiveClient._fetch、render_live_result（046）；WeatherMcpService.connect、_guarded、get_forecast（047）
- validate_catalog（048）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

from agentscope.mcp import StdioMCPConfig

from gogo_agent.integrations.catalog import CATALOG, Decision, validate_catalog
from gogo_agent.integrations.destination_live import (
    DestinationLiveClient,
    render_live_result,
    simulated_transport,
)
from gogo_agent.integrations.skill_runner import SkillError, SkillRunner, user_dir_name
from gogo_agent.integrations.tuniu import TuniuCliAdapter, to_candidate_set, tuniu_policy
from gogo_agent.integrations.weather_mcp import WeatherMcpError, WeatherMcpService

SCRIPTS = Path(__file__).resolve().parent
FAKE_TUNIU = str(SCRIPTS / "fake_tuniu_cli.py")
FAKE_WEATHER = str(SCRIPTS / "fake_weather_mcp_server.py")
FIXED_NOW = datetime(2026, 10, 11, 13, 48, tzinfo=UTC)  # 北京时间 2026-10-11 21:48
SEARCH_ARGS = ["call", "flight", "searchLowestPriceFlight", "-a", "{}"]
FAKE_KEY = "sk-demo-SECRET-0001"
MODE_ENV = ("FAKE_TUNIU_MODE", "FAKE_TUNIU_PIDFILE")


def runner(**policy_updates) -> SkillRunner:
    """用假途牛 CLI 建运行器；policy_updates 覆盖超时、输出上限等。"""
    policy = tuniu_policy(executable=sys.executable, fixed_args=[FAKE_TUNIU], extra_env_names=MODE_ENV)
    return SkillRunner(policy.model_copy(update=policy_updates))


async def case_058() -> None:
    """安全运行器：白名单、写命令拒绝、超时杀进程树、巨大输出中止、非法 JSON、非零退出、隔离与脱敏。"""
    os.environ["PARENT_ONLY_SECRET"] = "must-not-reach-child"
    ok = await runner().run_json(
        user_id="alice", args=SEARCH_ARGS, env={"TUNIU_API_KEY": FAKE_KEY, "FAKE_TUNIU_MODE": "echo_env"},
        secrets=[FAKE_KEY],
    )
    child_keys = set(ok.data["env_keys"])
    print(f"[058] ok exit_code={ok.exit_code} explicit_env_present={sorted(child_keys & {'TUNIU_API_KEY', 'FAKE_TUNIU_MODE', 'HOME', 'TMPDIR'})}")
    print(f"[058] env_whitelist parent_secret_leaked={'PARENT_ONLY_SECRET' in child_keys} child_home_is_workdir={ok.data['home'] == ok.workdir}")
    print(f"[058] isolation dir={Path(ok.workdir).parent.name} expected={user_dir_name('alice')} raw_user_in_path={'alice' in ok.workdir}")
    print(f"[058] cleanup workdir_exists={Path(ok.workdir).exists()}")

    for label, args in (
        ("metachar", ["call", "flight", "searchLowestPriceFlight; rm -rf /"]),
        ("write_command", ["call", "flight", "saveOrder", "-a", "{}"]),
        ("string_not_list", "call flight searchLowestPriceFlight"),
        ("path_arg", ["call", "flight", "searchLowestPriceFlight", "-a", "/etc/passwd"]),
    ):
        try:
            await runner().run_json(user_id="alice", args=args)
        except SkillError as exc:
            print(f"[058] {label} code={exc.code.value}")

    pidfile = Path(os.environ.get("TMPDIR", "/tmp")) / f"demo058-{os.getpid()}.pid"
    started = time.monotonic()
    try:
        await runner(timeout_s=1.0).run_json(
            user_id="alice", args=SEARCH_ARGS,
            env={"TUNIU_API_KEY": FAKE_KEY, "FAKE_TUNIU_MODE": "hang", "FAKE_TUNIU_PIDFILE": str(pidfile)},
        )
    except SkillError as exc:
        child = int(pidfile.read_text())
        alive = _alive(child)
        print(f"[058] timeout code={exc.code.value} child_alive={alive} returned_before_5s={time.monotonic() - started < 5}")
    pidfile.unlink(missing_ok=True)

    started = time.monotonic()
    try:
        await runner(max_stdout_bytes=1024, timeout_s=20).run_json(
            user_id="alice", args=SEARCH_ARGS, env={"TUNIU_API_KEY": FAKE_KEY, "FAKE_TUNIU_MODE": "huge"},
        )
    except SkillError as exc:
        print(f"[058] huge_output code={exc.code.value} aborted_early={time.monotonic() - started < 10}")

    for mode in ("bad_json", "fail"):
        try:
            await runner().run_json(
                user_id="alice", args=SEARCH_ARGS, env={"TUNIU_API_KEY": FAKE_KEY, "FAKE_TUNIU_MODE": mode},
                secrets=[FAKE_KEY],
            )
        except SkillError as exc:
            leaked = FAKE_KEY in str(exc) or FAKE_KEY in (exc.stderr or "")
            print(f"[058] {mode} code={exc.code.value} exit_code={exc.exit_code} secret_in_error={leaked}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _adapter(mode: str = "ok") -> TuniuCliAdapter:
    return TuniuCliAdapter(
        executable=sys.executable, fixed_args=[FAKE_TUNIU], timeout_s=3.0, extra_env={"FAKE_TUNIU_MODE": mode},
    )


async def case_049() -> None:
    """途牛适配器：固定样例规范化、编号规则、0 余票过滤、请求 ID、失败映射。"""
    adapter = _adapter()
    common = {"user_id": "alice", "api_key": FAKE_KEY}
    flights = await adapter.search_flights(
        departure_city="北京", arrival_city="上海", departure_date=date(2026, 3, 15), **common)
    trains = await adapter.search_trains(
        departure_city="北京", arrival_city="上海", departure_date=date(2026, 3, 15), **common)
    hotels = await adapter.search_hotels(
        city="上海", check_in=date(2026, 3, 15), check_out=date(2026, 3, 17), **common)
    print(f"[049] request_ids flight={flights.vendor_request_id} train={trains.vendor_request_id} hotel={hotels.vendor_request_id}")
    candidates = to_candidate_set([hotels, trains, flights], captured_at=FIXED_NOW)  # 故意乱序传入
    print(f"[049] source={candidates.source} transports={len(candidates.transports)} hotels={len(candidates.hotels)}")
    for item in candidates.transports:
        print(f"[049] {item.id} {item.code} {item.cabin_class} price={item.price} direct={item.is_direct}")
    for hotel in candidates.hotels:
        print(f"[049] {hotel.id} {hotel.name} star={hotel.star_rating} price={hotel.price_per_night} nights={hotel.nights}")
    print(f"[049] zero_stock_skipped={not any(i.code == 'G103' and i.cabin_class == '一等座' for i in candidates.transports)}")

    for label, mode, key in (("auth_fail", "fail", FAKE_KEY), ("not_json", "bad_json", FAKE_KEY),
                             ("vendor_error", "vendor_error", FAKE_KEY), ("no_key", "ok", "")):
        try:
            await _adapter(mode).search_flights(
                user_id="alice", api_key=key, departure_city="北京", arrival_city="上海",
                departure_date=date(2026, 3, 15))
        except SkillError as exc:
            print(f"[049] {label} code={exc.code.value} secret_in_error={FAKE_KEY in str(exc)}")
    try:
        await TuniuCliAdapter(
            executable=sys.executable, fixed_args=[FAKE_TUNIU], timeout_s=1.0, extra_env={"FAKE_TUNIU_MODE": "hang"},
        ).search_flights(user_id="alice", api_key=FAKE_KEY, departure_city="北京", arrival_city="上海",
                         departure_date=date(2026, 3, 15))
    except SkillError as exc:
        print(f"[049] timeout code={exc.code.value}")


async def case_046() -> None:
    """实时查询：正常、超时、无结果、不可用、日期超窗五种结果与给模型的文本。"""
    for mode in ("normal", "timeout", "no_result", "unavailable"):
        client = DestinationLiveClient(simulated_transport(mode), weather_timeout_s=0.3, now=lambda: FIXED_NOW)
        result = await client.query_weather("杭州", date(2026, 10, 12))
        print(f"[046] weather mode={mode} status={result.status.value} items={len(result.items)}")
        print(render_live_result(result))
    client = DestinationLiveClient(simulated_transport("normal"), now=lambda: FIXED_NOW)
    news = await client.query_news("杭州", "traffic")
    print(f"[046] news status={news.status.value} items={len(news.items)}")
    far = await client.query_weather("杭州", date(2026, 10, 20))
    print(f"[046] beyond_range={far.beyond_range} status={far.status.value}")
    print(render_live_result(far))


def _weather_config(mode: str, **extra: str) -> StdioMCPConfig:
    return StdioMCPConfig(command=sys.executable, args=[FAKE_WEATHER], env={"FAKE_WEATHER_MODE": mode, **extra})


async def case_047() -> None:
    """天气 MCP：真实 stdio 协议连接、白名单工具发现、调用、断连与超时。"""
    live = DestinationLiveClient(simulated_transport("normal"), now=lambda: FIXED_NOW)
    routed = await live.query_weather("杭州", date(2026, 10, 20))
    print(f"[047] route beyond_range={routed.beyond_range} -> use MCP")

    async with WeatherMcpService(_weather_config("normal"), enabled_tools=["forecast"]) as service:
        tools = await service.list_tools()
        print(f"[047] tools={[t.name for t in tools]} schema_params={sorted(tools[0].input_schema['properties'])}")
        forecast = await service.get_forecast("杭州", date(2026, 10, 20), date(2026, 10, 22))
        print(f"[047] forecast tool={forecast.tool_name} sent={forecast.sent_arguments} days={len(forecast.payload['days'])}")
        try:
            await service.get_forecast("杭州", date(2026, 10, 22), date(2026, 10, 20))
        except WeatherMcpError as exc:
            print(f"[047] bad_range code={exc.code.value}")
    try:
        await service.get_forecast("杭州", date(2026, 10, 20), date(2026, 10, 22))
    except WeatherMcpError as exc:
        print(f"[047] after_close code={exc.code.value} connected={service.is_connected}")

    async with WeatherMcpService(_weather_config("normal"), enabled_tools=["air_quality"]) as service:
        try:
            await service.get_forecast("杭州", date(2026, 10, 20), date(2026, 10, 22))
        except WeatherMcpError as exc:
            print(f"[047] not_whitelisted code={exc.code.value}")

    for label, mode in (("crash", "crash_call"), ("tool_error", "tool_error"), ("bad_payload", "bad_payload")):
        async with WeatherMcpService(_weather_config(mode), enabled_tools=["forecast"]) as service:
            try:
                await service.get_forecast("杭州", date(2026, 10, 20), date(2026, 10, 22))
            except WeatherMcpError as exc:
                print(f"[047] {label} code={exc.code.value}")
    async with WeatherMcpService(_weather_config("hang_call"), enabled_tools=["forecast"], call_timeout_s=1.0) as service:
        try:
            await service.get_forecast("杭州", date(2026, 10, 20), date(2026, 10, 22))
        except WeatherMcpError as exc:
            print(f"[047] call_timeout code={exc.code.value}")
    print("[047] 下一项会触发 AgentScope 2.0.8 的握手取消清理缺陷：stderr 出现长回溯与 BrokenPipe 属预期噪声")
    try:
        async with WeatherMcpService(
            _weather_config("hang_start", FAKE_WEATHER_HANG_S="4"), connect_timeout_s=1.0,
        ):
            pass
    except WeatherMcpError as exc:
        print(f"[047] connect_timeout code={exc.code.value}")
    try:
        async with WeatherMcpService(StdioMCPConfig(command="/nonexistent/weather-server")):
            pass
    except WeatherMcpError as exc:
        print(f"[047] start_failed code={exc.code.value}")


def case_048() -> None:
    """清单：校验通过，并展示反例会被抓到。"""
    print(f"[048] entries={len(CATALOG)} problems={validate_catalog()}")
    enabled = sorted(e.entry_id for e in CATALOG if e.enabled and e.decision is Decision.MIGRATE_STAGE_E)
    print(f"[048] migrate_stage_e={enabled}")
    print(f"[048] not_enabled={sorted(e.entry_id for e in CATALOG if not e.enabled)}")
    tuniu_write = [e for e in CATALOG if e.entry_id == "tuniu.flight.order"][0]
    print(f"[048] tuniu.flight.order access={tuniu_write.access.value} planning_allowed={tuniu_write.python_planning_allowed}")
    rolling = [e for e in CATALOG if e.entry_id == "rollinggo.search"][0]
    bad = rolling.model_copy(update={"decision": Decision.MIGRATE_STAGE_E, "authoritative": True})
    others = [e for e in CATALOG if e.entry_id != "rollinggo.search"]
    for problem in validate_catalog([*others, bad]):
        print(f"[048] negative: {problem}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", choices=["046", "047", "048", "049", "058", "all"], default="all")
    selected = parser.parse_args().case
    cases = {"046": case_046, "047": case_047, "048": case_048, "049": case_049, "058": case_058}
    for name in (cases if selected == "all" else [selected]):
        print(f"=== {name} ===")
        result = cases[name]()
        if asyncio.iscoroutine(result):
            asyncio.run(result)


if __name__ == "__main__":
    main()
