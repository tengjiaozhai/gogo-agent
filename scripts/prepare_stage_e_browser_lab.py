"""为浏览器调试阶段 E（042–060）准备一次性练习库：含账号、用户级途牛 Key、假途牛 CLI。

最短运行路线（在仓库根目录执行）：
    PYTHONPATH=src .venv/bin/python scripts/prepare_stage_e_browser_lab.py

脚本会新建一份系统临时 SQLite（拒绝覆盖已有文件），并生成：
1. 阶段 A–E 全部 10 张表（含 ``user_api_key``，阶段 D 的准备脚本不建这张表）；
2. 账号 alice(u001/P6/北京)、bob(u002/P5/上海)、admin(u_001/P8/北京)，练习密码均为 123456；
3. 给 alice 预置服务商 ``tuniu-cli`` 的**假** API Key——真实 Key 需要用户在途牛平台申请，本脚本不接触；
4. 一个把 `scripts/fake_tuniu_cli.py` 包装成可执行文件的 `tuniu` 命令，供 `GOGO_TUNIU_CLI` 指向；
5. 一份只含本次练习变量的 `lab.env`（权限 600）。密钥值不打印到终端，只打印文件路径。

为什么需要第 5 步：`planning.search.build_tuniu_key_provider()` 只有在设置了 `GOGO_SECRET_KEYS`
且数据库可用时才返回真实 Key 服务，否则一律 `TUNIU_KEY_SERVICE_UNCONFIGURED`，规划搜索必然失败。

脚本不以仓库 `.env` 里的数据库与密钥为准：练习库 URL、`GOGO_SECRET_KEYS`、`GOGO_TUNIU_CLI` 全部显式生成并写入 `lab.env`。
注意导入 `gogo_agent.db.models` 会间接触发 `db/session.py` 的 `load_dotenv()`，把 `.env` 变量读进 `os.environ`；
但由于上述三项都被显式覆盖，功能上不会用到 `.env` 的数据库或密钥。本脚本也不连接真实途牛。
"""

from __future__ import annotations

import base64
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path
from shlex import quote
import sys
from tempfile import mkdtemp

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from gogo_agent.auth.security import PasswordManager
from gogo_agent.db.base import Base
from gogo_agent.db.models import UserAccountModel, UserProfileModel
from gogo_agent.security.apikey import ApiKeyService, UserApiKeyModel  # noqa: F401  # 导入即把 user_api_key 注册进 Base
from gogo_agent.security.cipher import SecretCipher, generate_keys_env

ROOT = Path(__file__).resolve().parents[1]
FAKE_TUNIU_CLI = ROOT / "scripts" / "fake_tuniu_cli.py"
TUNIU_PROVIDER = "tuniu-cli"
# 阶段 A–E 的固定表数；见 prepare_database() 中的断言。
EXPECTED_TABLES = 10
# 明确标注为练习替身：真实途牛会对它返回 104 认证失败。这里只用于假 CLI，不会发往任何网络。
LAB_API_KEY = "lab-fake-tuniu-key-not-a-real-credential"
PRACTICE_PASSWORD = "123456"

ACCOUNTS = (
    (1, "u001", "alice", "张三", "USER", "P6", "北京"),
    (2, "u002", "bob", "李四", "USER", "P5", "上海"),
    (3, "u_001", "admin", "系统管理员", "ADMIN", "P8", "北京"),
)


def prepare_database(path: Path) -> tuple[str, str]:
    """建库、建表、写账号与练习 Key；返回 (数据库 URL, 本次使用的密钥配置)。"""
    path = path.expanduser().resolve()
    if path.exists():
        raise ValueError(f"练习库文件已存在，请换一个路径：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+pysqlite:///{path}"
    engine = create_engine(url)
    keys_env = generate_keys_env("v1")
    # 这里手工还原密钥而没走 SecretCipher.from_env()，只是为了避免把密钥塞进 os.environ。
    # 与 cipher.py 的 `版本:base64url` 格式必须保持同步；两处一旦分叉，这里会静默生成解不开的密文。
    cipher = SecretCipher([("v1", base64.urlsafe_b64decode(keys_env.split(":", 1)[1]))])
    try:
        Base.metadata.create_all(engine)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as session, session.begin():
            for pk, user_id, username, real_name, role, level, base_city in ACCOUNTS:
                session.add(UserAccountModel(
                    id=pk, user_id=user_id, username=username,
                    password=PasswordManager.hash_password(PRACTICE_PASSWORD),
                    real_name=real_name, role=role, created_time=now,
                ))
                session.add(UserProfileModel(user_id=user_id, level=level, base_city=base_city))
        # 只给 alice 写 Key：用来观察“有 Key 的用户能搜索、Bob 会明确失败”这一边界。
        ApiKeyService(factory, cipher).save("u001", TUNIU_PROVIDER, LAB_API_KEY)
        tables = inspect(engine).get_table_names()
        expected = set(Base.metadata.tables)
        if not expected.issubset(tables):
            raise RuntimeError(f"练习库缺表：{sorted(expected - set(tables))}")
        # 断言而非硬编码打印：若某处间接导入 planning.store，itinerary_plan 会一起建出来，
        # 表数就不再是 10，这里必须显式失败而不是打印一个失真的数字。
        if len(tables) != EXPECTED_TABLES:
            raise RuntimeError(f"练习库表数为 {len(tables)}，预期 {EXPECTED_TABLES}：{sorted(tables)}")
    except BaseException:
        engine.dispose()
        path.unlink(missing_ok=True)
        raise
    else:
        engine.dispose()
    path.chmod(0o600)
    return url, keys_env


def write_tuniu_wrapper(directory: Path) -> Path:
    """把仓库内的假途牛 CLI 包装成可执行文件，并打开日期回显。"""
    wrapper = directory / "tuniu"
    wrapper.write_text(
        "#!/bin/sh\n"
        "# 阶段 E 练习替身：只返回固定样例，不访问真实途牛。\n"
        "export FAKE_TUNIU_ECHO_DATES=1\n"
        f'exec {quote(sys.executable)} {quote(str(FAKE_TUNIU_CLI))} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def main() -> None:
    parser = ArgumentParser(description="新建一次性的阶段 E 浏览器调试 SQLite 库与假途牛命令")
    parser.add_argument("--sqlite", type=Path, help="自选新文件路径；不填则在系统临时目录生成")
    parser.add_argument("--wrapper-dir", type=Path, help="假途牛命令的存放目录；默认与练习库同目录")
    parser.add_argument(
        "--force", action="store_true",
        help="允许覆盖已存在的 lab.env 与 tuniu 包装器（默认拒绝，避免覆盖掉上一份练习密钥）",
    )
    args = parser.parse_args()
    base_dir = Path(mkdtemp(prefix="gogo-stage-e-browser-"))
    path = args.sqlite or base_dir / "lab.sqlite"
    wrapper_dir = args.wrapper_dir or path.parent
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    try:
        url, keys_env = prepare_database(path)
    except ValueError as exc:
        parser.error(str(exc))
    env_file = path.parent / "lab.env"
    if not args.force:
        existing = [str(p) for p in (wrapper_dir / "tuniu", env_file) if p.exists()]
        if existing:
            parser.error(
                "以下练习文件已存在，默认不覆盖（lab.env 承载本次练习密钥）："
                + "、".join(existing)
                + "。换用 --sqlite/--wrapper-dir，或显式加 --force"
            )
    wrapper = write_tuniu_wrapper(wrapper_dir)
    env_file.write_text(
        f"export GOGO_DATABASE_URL={quote(url)}\n"
        f"export GOGO_REDIS_HOST=''\n"
        f"export GOGO_SECRET_KEYS={quote(keys_env)}\n"
        f"export GOGO_TUNIU_CLI={quote(str(wrapper))}\n"
        # httpx2 无法解析 NO_PROXY 里的 IPv6 字面量 [::1]，会让请求在建立模型客户端时抛 InvalidURL。
        "export NO_PROXY='localhost,127.0.0.1'\n"
        "export no_proxy='localhost,127.0.0.1'\n"
        "export PYTHONPATH=src\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    backend = (
        f"source {quote(str(env_file))} && "
        ".venv/bin/python -m uvicorn gogo_agent.api:app --host 127.0.0.1 --port 8081"
    )
    frontend = (
        "cd frontend && GOGO_API_PROXY_TARGET=http://127.0.0.1:8081 "
        "npm run dev -- --host 127.0.0.1 --port 5174"
    )
    print(f"已创建阶段 E 浏览器练习库：{EXPECTED_TABLES} 张表、3 个账号、alice 的练习用途牛 Key、假途牛命令。")
    print(f"练习库：{path}")
    print(f"假途牛命令：{wrapper}")
    print(f"练习变量文件（权限 600，密钥值不打印）：{env_file}")
    print(f"练习账号：alice / bob / admin；密码仅在此临时库中为 {PRACTICE_PASSWORD}。")
    print("后端命令：")
    print("  " + backend)
    print("前端命令：")
    print("  " + frontend)
    print("PyCharm 的 Environment variables 里请从变量文件取 GOGO_SECRET_KEYS 等值粘贴，不要写回仓库 .env。")
    print("练习库、密钥、假途牛命令均显式生成；未使用仓库 .env 的数据库或密钥，也未连接真实途牛。")


if __name__ == "__main__":
    main()
