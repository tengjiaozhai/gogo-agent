"""091 全链路脱敏：外部 CLI 的成功输出、失败输出与异常文本都不得含明文标记，授权字段仍保留。

预订确认凭据 token 保存在消息 extra 中是设计如此（服务端回读校验），不属于本测试的泄露范围。
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from gogo_agent.integrations.skill_runner import SkillError, SkillErrorCode, SkillPolicy, SkillRunner

KEY = "sk-test-SECRET-9999"
PHONE = "13800138000"
ID_CARD = "110101199003071234"
SK_KEY = "sk-live-abcdefghijkl1234"
BEARER_TOKEN = "abcdefghijklmnop"
PASSWORD = "hunter2"
PLAIN_MARKERS = (KEY, PHONE, ID_CARD, SK_KEY, BEARER_TOKEN, PASSWORD)
ARGS = ["call", "flight", "searchLowestPriceFlight", "-a", "{}"]

# 测试内自带的假 CLI：按 FAKE_MODE 输出成功 JSON、失败 stderr 或非 JSON 文本，全部夹带标记型明文。
FAKE_CLI = r'''
import json, os, sys
mode = os.environ.get("FAKE_MODE", "ok")
key = os.environ.get("FAKE_KEY", "")
if mode == "ok":
    body = {"success": True, "data": {"trip": {
        "name": "北京-上海出差行程", "phone": "13800138000", "idCard": "110101199003071234",
        "apiKey": "sk-live-abcdefghijkl1234", "auth": "Bearer abcdefghijklmnop",
        "phoneNum": 13800138000,
        "note": "password=hunter2", "echo": key}}}
    print(json.dumps(body))
elif mode == "fail":
    sys.stderr.write("login rejected 13800138000 password=hunter2 Bearer abcdefghijklmnop " + key + "\n")
    sys.exit(2)
else:
    print("not json 13800138000 sk-live-abcdefghijkl1234 " + key)
'''


def make_runner(base_dir: Path) -> SkillRunner:
    """把假 CLI 写入 base_dir，并构造只允许 flight 搜索的运行器。"""
    script = base_dir / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    policy = SkillPolicy(
        name="fake-cli",
        executable=sys.executable,
        fixed_args=(str(script),),
        allowed_commands=(("call", "flight", "searchLowestPriceFlight"),),
        env_allowlist=("FAKE_MODE", "FAKE_KEY"),
    )
    return SkillRunner(policy, base_dir=base_dir / "runs")


def env(mode: str) -> dict[str, str]:
    """选择假 CLI 的输出模式，并把密钥经白名单环境变量传入。"""
    return {"FAKE_MODE": mode, "FAKE_KEY": KEY}


def assert_no_plain(text: str) -> None:
    """断言文本中不含任何明文标记。"""
    for marker in PLAIN_MARKERS:
        assert marker not in text, "出现明文标记"


@pytest.mark.asyncio
async def test_091_success_output_is_masked_and_structure_kept(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    result = await make_runner(tmp_path).run_json(user_id="alice", args=ARGS, env=env("ok"), secrets=[KEY])
    assert result.exit_code == 0
    assert set(result.data) == {"success", "data"}
    # 授权字段（行程名称）原样保留；其余敏感片段按模式遮盖；JSON 数字形式的手机号也遮盖为字符串。
    assert result.data["data"]["trip"] == {
        "name": "北京-上海出差行程",
        "phone": "138****8000",
        "idCard": "110" + "*" * 11 + "1234",
        "apiKey": "****1234",
        "auth": "Bearer ****mnop",
        "phoneNum": "138****8000",
        "note": "password=****",
        "echo": "***",
    }
    assert_no_plain(json.dumps(result.data, ensure_ascii=False) + caplog.text)


@pytest.mark.asyncio
async def test_091_failure_stderr_exception_and_log_are_masked(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SkillError) as raised:
        await make_runner(tmp_path).run_json(user_id="alice", args=ARGS, env=env("fail"), secrets=[KEY])
    error = raised.value
    assert error.code is SkillErrorCode.NONZERO_EXIT
    assert error.exit_code == 2
    assert "138****8000" in (error.stderr or "")
    assert "password=****" in (error.stderr or "")
    assert_no_plain(" ".join([str(error), error.message, error.stderr or "", caplog.text]))


@pytest.mark.asyncio
async def test_091_invalid_json_excerpt_is_masked(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SkillError) as raised:
        await make_runner(tmp_path).run_json(user_id="alice", args=ARGS, env=env("bad_json"), secrets=[KEY])
    assert raised.value.code is SkillErrorCode.INVALID_JSON
    assert "138****8000" in raised.value.message
    assert_no_plain(" ".join([str(raised.value), caplog.text]))
