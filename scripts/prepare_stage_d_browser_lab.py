"""为浏览器调试创建一次性的本地 SQLite 练习库，不读取或修改 .env 数据库。

从仓库根目录运行：
    PYTHONPATH=src .venv/bin/python scripts/prepare_stage_d_browser_lab.py

输出独立的 GOGO_DATABASE_URL；将它填入另起的后端调试进程。
账号 alice、bob、admin 的练习密码均为 123456，仅用于本地临时库。
"""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
from shlex import quote
from tempfile import mkdtemp

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from gogo_agent.auth.security import PasswordManager
from gogo_agent.db.base import Base
from gogo_agent.db.models import BookingRecordModel, UserAccountModel, UserProfileModel


def prepare_database(path: Path) -> str:
    """只接受不存在的 SQLite 文件，避免覆盖已有练习或真实数据。"""
    path = path.expanduser().resolve()
    if path.exists():
        raise ValueError(f"练习库文件已存在，请换一个路径：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+pysqlite:///{path}"
    engine = create_engine(url)
    try:
        Base.metadata.create_all(engine)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        accounts = (
            (1, "u001", "alice", "张三", "USER", "P6", "北京"),
            (2, "u002", "bob", "李四", "USER", "P5", "上海"),
            (3, "u_001", "admin", "系统管理员", "ADMIN", "P8", "北京"),
        )
        with Session(engine) as session, session.begin():
            for pk, user_id, username, real_name, role, level, base_city in accounts:
                session.add(UserAccountModel(
                    id=pk, user_id=user_id, username=username,
                    password=PasswordManager.hash_password("123456"),
                    real_name=real_name, role=role, created_time=now,
                ))
                session.add(UserProfileModel(
                    user_id=user_id, level=level, base_city=base_city,
                ))
            for booking_id, biz_type, title, amount in (
                ("lab-hotel-037", "HOTEL", "本地演示酒店", Decimal("400")),
                ("lab-flight-037", "FLIGHT", "本地演示机票", Decimal("1200")),
            ):
                session.add(BookingRecordModel(
                    booking_id=booking_id, user_id="u001", biz_type=biz_type,
                    platform="browser-lab", external_order_no=booking_id,
                    status="CONFIRMED", payment_status="PAID", title=title,
                    total_amount=amount, currency="CNY", created_at=now,
                    updated_at=now, deleted=0,
                ))
        assert len(inspect(engine).get_table_names()) == 9
    except BaseException:
        engine.dispose()
        path.unlink(missing_ok=True)
        raise
    else:
        engine.dispose()
    os.chmod(path, 0o600)
    return url


def main() -> None:
    parser = ArgumentParser(description="新建一次性的阶段 D 浏览器调试 SQLite 库")
    parser.add_argument("--sqlite", type=Path, help="自选新文件路径；不填则在系统临时目录生成")
    args = parser.parse_args()
    path = args.sqlite or Path(mkdtemp(prefix="gogo-stage-d-browser-")) / "lab.sqlite"
    try:
        url = prepare_database(path)
    except ValueError as exc:
        parser.error(str(exc))
    print("已创建本地练习库：9 张表、3 个测试账号与职级、2 条模拟预订记录。")
    print("GOGO_DATABASE_URL=" + url)
    print("练习账号：alice / bob / admin；密码仅在此临时库中为 123456。")
    print("另起后端时设置 GOGO_REDIS_HOST=''，让 Token 仅保存在本进程内。")
    print("后端命令：GOGO_DATABASE_URL=" + quote(url) + " GOGO_REDIS_HOST='' PYTHONPATH=src "
          ".venv/bin/python -m uvicorn gogo_agent.api:app --host 127.0.0.1 --port 8001")
    print("前端命令：cd frontend && GOGO_API_PROXY_TARGET=http://127.0.0.1:8001 "
          "npm run dev -- --host 127.0.0.1 --port 5174")
    print("脚本没有读取或修改仓库 .env 指向的数据库。")


if __name__ == "__main__":
    main()
