"""阶段 H 089：外部 CLI 的受限执行边界。

判定（与文档一致）：不实施内核级沙箱（seccomp、bwrap、sandbox-exec）。理由见
`integrations/skill_runner.py` 模块文档的边界声明：隔离依赖 cwd/HOME/TMPDIR 与路径类参数拒绝，
没有内核级限制。本文件只证明“受限参数执行”确实生效，不声称“沙箱已启用”。
未覆盖项：外部 CLI 凭绝对路径写入目录之外的文件（需内核沙箱才能阻断）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gogo_agent.integrations.skill_runner import SkillError, SkillErrorCode, SkillRunner
from gogo_agent.integrations.tuniu import tuniu_policy

FAKE_TUNIU = str(Path(__file__).resolve().parents[1] / "scripts" / "fake_tuniu_cli.py")
ENV = {"FAKE_TUNIU_MODE": "ok"}


def _runner(base_dir: Path) -> SkillRunner:
    policy = tuniu_policy(executable=sys.executable, fixed_args=[FAKE_TUNIU], extra_env_names=("FAKE_TUNIU_MODE",))
    return SkillRunner(policy, base_dir=base_dir)


@pytest.mark.asyncio
async def test_089_child_sees_only_its_isolated_cwd_home_and_whitelisted_env(tmp_path, monkeypatch):
    # 与 058 相同：由 fake CLI 回显子进程真实看到的 cwd、HOME 与环境键，不依赖 monkeypatch 的参数捕获。
    monkeypatch.setenv("PARENT_ONLY_SECRET", "must-not-reach-child")
    base = tmp_path / "runs"
    result = await _runner(base).run_json(
        user_id="alice", args=["call", "flight", "searchLowestPriceFlight", "-a", '{"from":"北京"}'],
        env={**ENV, "FAKE_TUNIU_MODE": "echo_env", "TUNIU_API_KEY": "k-089-probe"}, secrets=["k-089-probe"],
    )
    workdir = Path(result.workdir).resolve()
    assert base.resolve() in workdir.parents, "工作目录应在运行器的用户隔离目录内"
    assert Path(result.data["cwd"]).resolve().name == workdir.name
    assert result.data["home"] == result.workdir, "HOME 应指向隔离目录，而非真实用户目录"
    child_env = set(result.data["env_keys"])
    assert "PARENT_ONLY_SECRET" not in child_env
    assert {"HOME", "TMPDIR"} <= child_env
    assert "k-089-probe" not in str(result.model_dump())  # 密钥不进入返回值


@pytest.mark.asyncio
async def test_089_absolute_path_argument_is_refused_before_exec(tmp_path, monkeypatch):
    spawned: list[tuple] = []

    async def record(*argv, **_kwargs):
        spawned.append(argv)
        raise AssertionError("含绝对路径的参数不应启动外部程序")

    monkeypatch.setattr("asyncio.create_subprocess_exec", record)
    with pytest.raises(SkillError) as info:
        await _runner(tmp_path).run_json(
            user_id="alice", args=["call", "flight", "searchLowestPriceFlight", "-a", "/etc/passwd"], env=ENV,
        )
    assert info.value.code is SkillErrorCode.INVALID_ARGUMENT
    assert spawned == []
