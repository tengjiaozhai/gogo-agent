"""088 命令执行护栏：补充 test_058 未覆盖的恶意输入（换行、反引号、绝对路径、空参数、过长参数）。

被测对象：gogo_agent.integrations.skill_runner.SkillRunner 与 tuniu.TuniuCliAdapter。
外部程序为 scripts/fake_tuniu_cli.py。“外部程序未执行”由两点证明：
create_subprocess_exec 的调用记录为空，且校验失败时运行目录尚未创建；注入命令的标记文件也不得出现。
已知缺口用 strict xfail 标明，修复后会变为 XPASS 并提示移除标记。不连接数据库或 Redis。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

from gogo_agent.integrations.skill_runner import SkillError, SkillErrorCode, SkillRunner
from gogo_agent.integrations.tuniu import TuniuCliAdapter, tuniu_policy

ROOT = Path(__file__).resolve().parents[1]
FAKE_TUNIU = str(ROOT / "scripts" / "fake_tuniu_cli.py")
KEY = "sk-test-SECRET-8888"
ENV = {"TUNIU_API_KEY": KEY}
SEARCH = ["call", "flight", "searchLowestPriceFlight", "-a", "{}"]
NOT_ALLOWED = SkillErrorCode.COMMAND_NOT_ALLOWED
INVALID = SkillErrorCode.INVALID_ARGUMENT
PAYLOADS = ["`touch {marker}`", "$(touch {marker})", "a\nb; touch {marker}"]
PAYLOAD_IDS = ["反引号", "命令替换", "换行后接分号"]


def make_runner(base_dir: Path) -> SkillRunner:
    policy = tuniu_policy(executable=sys.executable, fixed_args=[FAKE_TUNIU], extra_env_names=("FAKE_TUNIU_MODE",))
    return SkillRunner(policy, base_dir=base_dir)


@pytest.fixture
def spawned(monkeypatch) -> list[tuple[str, ...]]:
    """记录每次真正尝试启动子进程的 argv；仍放行真实启动，使正常调用确实执行了外部程序。"""
    calls: list[tuple[str, ...]] = []
    original = asyncio.create_subprocess_exec

    async def recording(program, *args, **kwargs):
        calls.append((program, *args))
        return await original(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    return calls


REJECTED = [
    pytest.param(["call", "flight", "searchLowestPriceFlight\n"], {NOT_ALLOWED}, id="换行附在白名单命令后"),
    pytest.param(["call", "flight", "searchLowestPriceFlight\nsaveOrder"], {NOT_ALLOWED}, id="换行后夹带写操作"),
    pytest.param(["call", "flight", "`whoami`"], {NOT_ALLOWED}, id="反引号作为子命令"),
    pytest.param(["/bin/sh", "-c", "id"], {NOT_ALLOWED}, id="绝对路径可执行文件"),
    pytest.param(["", "call", "flight", "searchLowestPriceFlight"], {NOT_ALLOWED}, id="空的首个参数"),
    pytest.param(["call", "flight", "searchLowestPriceFlight", "~/.ssh/id_rsa"], {INVALID}, id="家目录路径参数"),
    pytest.param(["call", "flight", "searchLowestPriceFlight", "x" * 4097], {INVALID}, id="单参数超过4096字节"),
    pytest.param(["call", "flight", "searchLowestPriceFlight", "x" * 4090, "y" * 4090], {INVALID},
                 id="单参数均未超限但总和超过8192"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("args", "codes"), REJECTED)
async def test_088_malicious_arguments_are_rejected_before_any_process(tmp_path, spawned, args, codes):
    with pytest.raises(SkillError) as raised:
        await make_runner(tmp_path).run_json(user_id="u", args=args, env=ENV)
    assert raised.value.code in codes
    assert spawned == []  # 外部程序从未被启动
    assert list(tmp_path.iterdir()) == []  # 校验先于建目录，运行目录没有被创建


@pytest.mark.asyncio
@pytest.mark.parametrize("city", ["", "   ", "北" * 41], ids=["空字符串", "全部空白", "超过40个字符"])
async def test_088_adapter_rejects_empty_and_overlong_city(tmp_path, spawned, city):
    adapter = TuniuCliAdapter(executable=sys.executable, fixed_args=[FAKE_TUNIU], base_dir=tmp_path)
    with pytest.raises(SkillError) as raised:
        await adapter.search_flights(user_id="alice", api_key=KEY, departure_city=city,
                                     arrival_city="上海", departure_date=date(2026, 3, 15))
    assert raised.value.code is INVALID
    assert spawned == [] and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_088_absolute_path_in_city_name_should_be_rejected(tmp_path, spawned):
    adapter = TuniuCliAdapter(executable=sys.executable, fixed_args=[FAKE_TUNIU], base_dir=tmp_path)
    rejected = False
    try:
        await adapter.search_flights(user_id="alice", api_key=KEY, departure_city="/etc/passwd",
                                     arrival_city="上海", departure_date=date(2026, 3, 15))
    except SkillError as exc:
        rejected = exc.code is INVALID
    assert rejected and spawned == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", PAYLOADS, ids=PAYLOAD_IDS)
async def test_088_shell_syntax_in_tail_argument_should_be_rejected(tmp_path, spawned, payload):
    marker = tmp_path / "pwned.txt"
    rejected = False
    try:
        await make_runner(tmp_path / "w").run_json(
            user_id="u", args=["call", "flight", "searchLowestPriceFlight", "-a", payload.format(marker=marker)],
            env=ENV,
        )
    except SkillError as exc:
        rejected = exc.code is INVALID
    assert rejected and spawned == [] and not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", PAYLOADS, ids=PAYLOAD_IDS)
async def test_088_shell_syntax_is_never_interpreted(tmp_path, spawned, payload):
    """无论是否被拒绝：若启动，整段文本必须作为单个 argv 原样交给程序；标记文件不得出现。"""
    marker = tmp_path / "pwned.txt"
    literal = payload.format(marker=marker)
    args = ["call", "flight", "searchLowestPriceFlight", "-a", literal]
    rejected = False
    try:
        await make_runner(tmp_path / "w").run_json(user_id="u", args=args, env=ENV)
    except SkillError as exc:
        rejected = True
        assert exc.code is INVALID
    if rejected:
        assert spawned == []  # 被拒绝则从未启动外部程序
    else:
        assert spawned == [(sys.executable, FAKE_TUNIU, *args)]  # 启动时整段文本是单个 argv 元素
    assert not marker.exists()


@pytest.mark.asyncio
async def test_088_normal_cli_call_still_runs_once_and_parses(tmp_path, spawned):
    result = await make_runner(tmp_path).run_json(user_id="u", args=SEARCH, env=ENV, secrets=[KEY])
    assert result.exit_code == 0 and result.data["success"] is True
    assert spawned == [(sys.executable, FAKE_TUNIU, *SEARCH)]  # 正常调用恰好启动一次


@pytest.mark.asyncio
async def test_088_adapter_normal_search_still_succeeds(tmp_path, spawned):
    adapter = TuniuCliAdapter(executable=sys.executable, fixed_args=[FAKE_TUNIU], base_dir=tmp_path)
    found = await adapter.search_flights(user_id="alice", api_key=KEY, departure_city="北京",
                                         arrival_city="上海", departure_date=date(2026, 3, 15))
    assert [t.code for t in found.transports] == ["MU5101", "CA1832"]
    assert len(spawned) == 1 and KEY not in str(found.model_dump())


@pytest.mark.asyncio
async def test_088_empty_tail_argument_should_be_rejected(tmp_path, spawned):
    rejected = False
    try:
        await make_runner(tmp_path).run_json(user_id="u", args=[*SEARCH[:3], ""], env=ENV)
    except SkillError as exc:
        rejected = exc.code is INVALID
    assert rejected and spawned == []
