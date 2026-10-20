"""058 外部 CLI/Skill 的安全运行器：白名单、限额、超时杀进程树、用户隔离目录与密钥屏蔽。

Java 基线把 CLI 当作 `execute_shell_command` 的一段 shell 文本执行，再由 Hook 改写命令
（`TuniuApiKeyHook` 在命令前拼 `env TUNIU_API_KEY=...`，`RghUserIsolationHook` 包 `env HOME=... bash -c '...'`）。
Python 这里改为：命令以参数列表交给 `create_subprocess_exec`（不经 shell），密钥只经环境变量传入，
用户隔离靠独立的 HOME/cwd/TMPDIR，而不是命令文本改写。

边界（如实声明）：隔离依赖 cwd/HOME/TMPDIR 指向用户专属目录，加上对路径类参数的拒绝；
没有内核级沙箱（如 seccomp、bwrap、sandbox-exec），恶意 CLI 仍可能凭绝对路径写目录之外。
进程树清理依赖 `start_new_session` + `killpg`，仅支持 POSIX；自行 `setsid` 脱离进程组的孙进程无法被清理。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_RESERVED_ENV = frozenset({"HOME", "TMPDIR"})
_EXCERPT_CHARS = 200
_READ_CHUNK = 64 * 1024
_REAP_TIMEOUT_S = 5


class SkillErrorCode(StrEnum):
    """外部 Skill 调用失败的稳定错误码；调用方只应依赖这些值，不要解析消息文本。"""

    TIMEOUT = "TIMEOUT"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    NONZERO_EXIT = "NONZERO_EXIT"
    INVALID_JSON = "INVALID_JSON"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    START_FAILED = "START_FAILED"
    COMMAND_NOT_ALLOWED = "COMMAND_NOT_ALLOWED"
    VENDOR_ERROR = "VENDOR_ERROR"


class SkillError(Exception):
    """统一的外部 Skill 异常；`message` 与 `stderr` 已屏蔽调用方声明的密钥值。"""

    def __init__(
        self,
        code: SkillErrorCode,
        message: str,
        *,
        exit_code: int | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(f"[{code.value}] {message}")
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.stderr = stderr


class SkillPolicy(BaseModel):
    """一个外部 CLI 的运行策略：可执行文件、命令白名单、输入/输出上限、超时与环境变量白名单。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, description="Skill 名称，仅用于日志与错误信息，如 tuniu-cli")
    executable: str = Field(..., min_length=1, description="可执行文件名或绝对路径，运行前经 PATH 解析为绝对路径")
    fixed_args: tuple[str, ...] = Field(
        default=(), description="固定前置参数（如解释器后的脚本路径），由部署方写死，不接受调用方输入",
    )
    allowed_commands: tuple[tuple[str, ...], ...] = Field(
        ..., min_length=1, description="允许的命令前缀白名单；调用方参数必须以其中某一项开头，逐词精确匹配",
    )
    denied_tokens: frozenset[str] = Field(
        default=frozenset(), description="显式拒绝的子命令词（如下单/取消）；任何参数词与之相等即拒绝",
    )
    max_args: int = Field(default=10, gt=0, description="参数个数上限（不含固定前置参数）")
    max_arg_bytes: int = Field(default=4096, gt=0, description="单个参数的 UTF-8 字节上限")
    max_total_arg_bytes: int = Field(default=8192, gt=0, description="全部参数的 UTF-8 字节总上限")
    timeout_s: float = Field(default=30.0, gt=0, description="整次调用超时秒数，超时杀整个进程组")
    max_stdout_bytes: int = Field(default=1_048_576, gt=0, description="标准输出字节上限，超限立即中止并杀进程")
    max_stderr_bytes: int = Field(default=65_536, gt=0, description="标准错误字节上限，超限立即中止并杀进程")
    env_allowlist: tuple[str, ...] = Field(
        default=(), description="允许显式传给子进程的环境变量名；HOME 与 TMPDIR 由运行器固定指向隔离目录",
    )
    exit_code_messages: dict[int, str] = Field(
        default_factory=dict, description="非零退出码到中文含义的映射，未列出的退出码报告为未知错误",
    )


class SkillResult(BaseModel):
    """一次成功调用的结果：解析后的 JSON 与运行元数据。"""

    model_config = ConfigDict(extra="forbid")

    data: Any = Field(..., description="标准输出解析得到的 JSON 值")
    exit_code: int = Field(..., description="进程退出码，成功时恒为 0")
    duration_ms: int = Field(..., ge=0, description="从启动到解析完成的毫秒数")
    workdir: str = Field(..., description="本次使用的隔离工作目录路径；返回时目录已清理，仅供排查对照")


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    """把文本中的密钥原文及其 JSON 转义形式替换为 ***。"""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        for form in {secret, json.dumps(secret, ensure_ascii=False)[1:-1]}:
            if form:
                text = text.replace(form, "***")
    return text


def user_dir_name(user_id: str) -> str:
    """由用户标识得到目录名；只含哈希前缀，不含用户标识原文。"""
    return "u_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


class _OutputOverflow(Exception):
    """某个输出流超过字节上限；仅在运行器内部传递。"""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.stream = stream


async def _read_capped(stream: asyncio.StreamReader, limit: int, name: str) -> bytes:
    """分块读取并在超限的当前块立即抛出，保证不会先读完再截断。"""
    buffer = bytearray()
    while chunk := await stream.read(_READ_CHUNK):
        buffer += chunk
        if len(buffer) > limit:
            raise _OutputOverflow(name)
    return bytes(buffer)


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """向子进程所在进程组发 SIGKILL；进程组已不存在时静默。"""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """杀进程后丢弃剩余输出并等待退出。

    asyncio 的 `Process.wait()` 要等管道关闭；输出超限时读取已停止、管道可能被流控暂停，直接等会永久挂起，
    所以先把剩余数据读空。若有脱离进程组的孙进程仍握着管道，最多等 5 秒后放弃并记录警告。
    """

    async def drain(stream: asyncio.StreamReader) -> None:
        while await stream.read(_READ_CHUNK):
            pass

    try:
        async with asyncio.timeout(_REAP_TIMEOUT_S):
            await asyncio.gather(drain(proc.stdout), drain(proc.stderr), proc.wait())
    except TimeoutError:
        logger.warning("子进程在 %s 秒内未能回收，可能有脱离进程组的孙进程", _REAP_TIMEOUT_S)


class SkillRunner:
    """按策略运行外部 CLI 并返回 JSON；每次调用使用独立的用户隔离临时目录。"""

    def __init__(self, policy: SkillPolicy, *, base_dir: Path | None = None) -> None:
        self._policy = policy
        self._base_dir = Path(base_dir) if base_dir else Path(tempfile.gettempdir()) / "gogo-agent-skill"

    async def run_json(
        self,
        *,
        user_id: str,
        args: Sequence[str],
        env: Mapping[str, str] | None = None,
        secrets: Sequence[str] = (),
    ) -> SkillResult:
        """运行一次命令；`secrets` 中的值不会出现在异常、日志或返回的文本里。"""
        policy = self._policy
        secrets = tuple(secrets)
        started = time.monotonic()
        if not isinstance(user_id, str) or not user_id.strip():
            raise SkillError(SkillErrorCode.INVALID_ARGUMENT, "用户标识不能为空")
        self._validate_args(args, secrets)
        child_env_extra = self._validate_env(env)

        executable = shutil.which(policy.executable)
        if executable is None:
            raise SkillError(SkillErrorCode.START_FAILED, f"找不到可执行文件：{policy.executable}")

        user_key = user_dir_name(user_id)
        run_dir = self._base_dir / user_key / f"r_{uuid.uuid4().hex[:12]}"
        run_dir.mkdir(parents=True, mode=0o700)
        # 子进程仍需 PATH 才能解析 node 等运行时依赖（如 tuniu CLI 的 shim）；
        # 默认继承宿主 PATH，白名单内显式传入的值优先。
        child_env = {
            "HOME": str(run_dir),
            "TMPDIR": str(run_dir),
            "PATH": child_env_extra.pop("PATH", os.environ.get("PATH") or os.defpath),
            **child_env_extra,
        }
        proc: asyncio.subprocess.Process | None = None
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    executable, *policy.fixed_args, *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=run_dir,
                    env=child_env,
                    start_new_session=True,
                )
            except OSError as exc:
                raise self._fail(SkillErrorCode.START_FAILED, f"启动失败：{exc.strerror or exc}", secrets) from None
            stdout, stderr = await self._collect(proc, secrets)
            exit_code = proc.returncode
            if exit_code != 0:
                excerpt = self._excerpt(stderr, secrets)
                meaning = policy.exit_code_messages.get(exit_code, "未知错误") if exit_code > 0 else f"被信号 {-exit_code} 终止"
                raise self._fail(
                    SkillErrorCode.NONZERO_EXIT,
                    f"{policy.name} 退出码 {exit_code}：{meaning}" + (f"；stderr：{excerpt}" if excerpt else ""),
                    secrets, exit_code=exit_code, stderr=excerpt,
                )
            data = self._parse_json(stdout, secrets)
            duration_ms = int((time.monotonic() - started) * 1000)
            logger.info("skill=%s user=%s ok exit=0 ms=%d", policy.name, user_key, duration_ms)
            return SkillResult(data=data, exit_code=0, duration_ms=duration_ms, workdir=str(run_dir))
        except SkillError as exc:
            logger.warning(
                "skill=%s user=%s failed code=%s msg=%s",
                policy.name, user_key, exc.code.value, redact_secrets(exc.message, secrets),
            )
            raise
        finally:
            if proc is not None:
                _kill_process_group(proc)
                await _reap(proc)
            shutil.rmtree(run_dir, ignore_errors=True)
            try:
                run_dir.parent.rmdir()
            except OSError:
                pass

    async def _collect(self, proc: asyncio.subprocess.Process, secrets: Sequence[str]) -> tuple[bytes, bytes]:
        """并发读取两路输出与等待退出；超时或超限时抛 SkillError，进程组由 run_json 的 finally 统一杀掉。"""
        policy = self._policy
        out_task = asyncio.create_task(_read_capped(proc.stdout, policy.max_stdout_bytes, "stdout"))
        err_task = asyncio.create_task(_read_capped(proc.stderr, policy.max_stderr_bytes, "stderr"))
        wait_task = asyncio.create_task(proc.wait())
        tasks = (out_task, err_task, wait_task)
        try:
            async with asyncio.timeout(policy.timeout_s):
                await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in tasks:
                if task.done() and task.exception() is not None:
                    raise task.exception()
            return out_task.result(), err_task.result()
        except TimeoutError:
            raise self._fail(
                SkillErrorCode.TIMEOUT, f"{policy.name} 超过 {policy.timeout_s:g} 秒未完成，已终止进程树", secrets,
            ) from None
        except _OutputOverflow as exc:
            limit = policy.max_stdout_bytes if exc.stream == "stdout" else policy.max_stderr_bytes
            raise self._fail(
                SkillErrorCode.OUTPUT_TOO_LARGE, f"{policy.name} 的 {exc.stream} 超过 {limit} 字节上限，已中止并终止进程树", secrets,
            ) from None
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _validate_args(self, args: Sequence[str], secrets: Sequence[str]) -> None:
        """校验类型、个数、字节数，再按显式拒绝词与白名单前缀判定。"""
        policy = self._policy
        if isinstance(args, (str, bytes)) or not isinstance(args, Sequence):
            raise SkillError(SkillErrorCode.INVALID_ARGUMENT, "命令必须是参数列表，不接受整段文本")
        if not args or len(args) > policy.max_args:
            raise SkillError(SkillErrorCode.INVALID_ARGUMENT, f"参数个数必须在 1 到 {policy.max_args} 之间")
        total = 0
        for token in args:
            if not isinstance(token, str):
                raise SkillError(SkillErrorCode.INVALID_ARGUMENT, "参数必须全部是字符串")
            if "\x00" in token:
                raise SkillError(SkillErrorCode.INVALID_ARGUMENT, "参数不能包含 NUL 字符")
            size = len(token.encode("utf-8"))
            if size > policy.max_arg_bytes:
                raise SkillError(SkillErrorCode.INVALID_ARGUMENT, f"单个参数超过 {policy.max_arg_bytes} 字节")
            total += size
        if total > policy.max_total_arg_bytes:
            raise SkillError(SkillErrorCode.INVALID_ARGUMENT, f"参数总长度超过 {policy.max_total_arg_bytes} 字节")

        for token in args:
            if token in policy.denied_tokens:
                raise SkillError(SkillErrorCode.COMMAND_NOT_ALLOWED, f"子命令 {token} 属于写操作，已被显式拒绝")
        for prefix in policy.allowed_commands:
            if tuple(args[: len(prefix)]) == prefix:
                tail = args[len(prefix):]
                break
        else:
            shown = redact_secrets(" ".join(args[:3]), secrets)[:80]
            raise SkillError(SkillErrorCode.COMMAND_NOT_ALLOWED, f"命令不在白名单内：{shown!r}")
        for token in tail:
            if token[:1] in ("{", "["):
                continue
            if token.startswith(("/", "~")) or ".." in Path(token).parts:
                raise SkillError(SkillErrorCode.INVALID_ARGUMENT, "不接受路径类参数")

    def _validate_env(self, env: Mapping[str, str] | None) -> dict[str, str]:
        """环境变量必须在白名单内，且不能覆盖运行器固定的 HOME/TMPDIR。"""
        result: dict[str, str] = {}
        for name, value in (env or {}).items():
            if name in _RESERVED_ENV:
                raise SkillError(SkillErrorCode.INVALID_ARGUMENT, f"环境变量 {name} 由运行器固定，不能传入")
            if name not in self._policy.env_allowlist:
                raise SkillError(SkillErrorCode.INVALID_ARGUMENT, f"环境变量 {name} 不在白名单内")
            if not isinstance(value, str) or "\x00" in value:
                raise SkillError(SkillErrorCode.INVALID_ARGUMENT, f"环境变量 {name} 的值必须是不含 NUL 的字符串")
            result[name] = value
        return result

    @staticmethod
    def _excerpt(raw: bytes, secrets: Sequence[str]) -> str:
        """取输出前若干字符并屏蔽密钥，用于错误信息。"""
        text = raw.decode("utf-8", errors="replace").strip()
        return redact_secrets(text, secrets)[:_EXCERPT_CHARS]

    @staticmethod
    def _fail(
        code: SkillErrorCode, message: str, secrets: Sequence[str],
        *, exit_code: int | None = None, stderr: str | None = None,
    ) -> SkillError:
        """构造已屏蔽密钥的异常。"""
        return SkillError(code, redact_secrets(message, secrets), exit_code=exit_code, stderr=stderr)

    def _parse_json(self, stdout: bytes, secrets: Sequence[str]) -> Any:
        """解析标准输出为 JSON，失败映射为 INVALID_JSON 并附屏蔽后的片段。"""
        try:
            return json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise self._fail(
                SkillErrorCode.INVALID_JSON,
                f"{self._policy.name} 的标准输出不是合法 JSON：{self._excerpt(stdout, secrets)[:100]!r}",
                secrets,
            ) from None
