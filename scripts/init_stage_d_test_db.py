"""030 测试库建表：cd 仓库根目录；PYTHONPATH=src .venv/bin/python scripts/init_stage_d_test_db.py。

默认仅创建进程内 SQLite；--sqlite 路径可生成新测试文件，已存在文件会拒绝覆盖。
PyCharm：Script path 指向本文件，Working directory 设仓库根目录，Interpreter 设 .venv/bin/python。
断点：main 的 create_all 前后；观察 table_names 从空列表变为五张阶段 D 表。
逐步指南：docs/契约样例/阶段D-029-041-调试指南.md。
"""

from argparse import ArgumentParser
from pathlib import Path

from sqlalchemy import create_engine, inspect

from gogo_agent.db.base import Base
from gogo_agent.db.models import (
    ApprovalRecordModel, BookingRecordModel, BusinessActionAuditModel,
    TravelOrderModel, UserProfileModel,
)


STAGE_D_TABLES = (
    TravelOrderModel.__table__, ApprovalRecordModel.__table__,
    UserProfileModel.__table__, BookingRecordModel.__table__,
    BusinessActionAuditModel.__table__,
)


def main() -> None:
    parser = ArgumentParser(description="只创建阶段 D 隔离 SQLite 测试表")
    parser.add_argument("--sqlite", help="新 SQLite 文件路径；不传时使用进程内数据库")
    args = parser.parse_args()
    if args.sqlite:
        path = Path(args.sqlite).expanduser().resolve()
        if path.exists():
            parser.error("测试库文件已存在，不覆盖已有数据")
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+pysqlite:///{path}"
    else:
        url = "sqlite+pysqlite:///:memory:"
    engine = create_engine(url)
    try:
        before = inspect(engine).get_table_names()
        Base.metadata.create_all(engine, tables=STAGE_D_TABLES)
        after = inspect(engine).get_table_names()
        print("before:", before)
        print("after:", after)
        print("table_count:", len(after))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
