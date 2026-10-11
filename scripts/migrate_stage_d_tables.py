"""为 .env 指向的 MySQL 创建阶段 D 五张表，并核对九张项目表的字段注释。

从仓库根目录运行：
    PYTHONPATH=src .venv/bin/python scripts/migrate_stage_d_tables.py
    PYTHONPATH=src .venv/bin/python scripts/migrate_stage_d_tables.py --apply

默认只读预检；--apply 仅创建缺失的阶段 D 表，不修改现有项目表或其他 ai_* 表，
也不导入用户资料或政策数据。中途失败后可重新预检，再决定是否重试。
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL, make_url

from gogo_agent.db.models import (
    AgentScopeSessionModel, ApprovalRecordModel, BookingRecordModel,
    BusinessActionAuditModel, ChatConversationModel, ChatMessageModel,
    TravelOrderModel, UserAccountModel, UserProfileModel,
)
from gogo_agent.db.base import Base


ROOT = Path(__file__).resolve().parents[1]
STAGE_D_TABLES = (
    TravelOrderModel.__table__, ApprovalRecordModel.__table__,
    UserProfileModel.__table__, BookingRecordModel.__table__,
    BusinessActionAuditModel.__table__,
)
PROJECT_TABLES = (
    UserAccountModel.__table__, ChatConversationModel.__table__,
    ChatMessageModel.__table__, AgentScopeSessionModel.__table__,
    *STAGE_D_TABLES,
)


def database_url() -> URL:
    """严格读取仓库 .env，而非可能指向其他库的进程环境变量。"""
    raw = dotenv_values(ROOT / ".env").get("GOGO_DATABASE_URL")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("仓库 .env 缺少 GOGO_DATABASE_URL")
    url = make_url(raw.strip())
    if url.get_backend_name() != "mysql":
        raise ValueError("阶段 D 正式建表脚本只接受 .env 中的 MySQL/MariaDB URL")
    if not url.database:
        raise ValueError("GOGO_DATABASE_URL 缺少数据库名称")
    return url


def verify_existing(inspector, names: set[str]) -> tuple[list[str], int]:
    """拒绝缺列、模型外列或注释缺失；不会自动修改已有列类型。"""
    problems: list[str] = []
    columns_checked = 0
    for table in PROJECT_TABLES:
        if table.name not in names:
            continue
        live = {column["name"]: column for column in inspector.get_columns(table.name)}
        expected = {column.name: column for column in table.columns}
        missing = sorted(set(expected) - set(live))
        extra = sorted(set(live) - set(expected))
        if missing:
            problems.append(f"{table.name} 缺列：{', '.join(missing)}")
        if extra:
            problems.append(f"{table.name} 有模型外列：{', '.join(extra)}")
        for name, column in live.items():
            columns_checked += 1
            comment = column.get("comment") or ""
            if not any("\u4e00" <= character <= "\u9fff" for character in comment):
                problems.append(f"{table.name}.{name} 缺少中文字段注释")
        for name, column in expected.items():
            if not any("\u4e00" <= character <= "\u9fff" for character in (column.comment or "")):
                problems.append(f"ORM 模型 {table.name}.{name} 缺少中文字段注释")
            elif name in live and live[name].get("comment") != column.comment:
                problems.append(f"{table.name}.{name} 的字段注释与 ORM 模型不一致")
    return problems, columns_checked


def verify_constraints(inspector) -> list[str]:
    """检查本次业务幂等与活跃申请去重需要的两个唯一约束。"""
    checks = (
        ("travel_order", {"active_fingerprint"}),
        ("business_action_audit", {"user_id", "action", "idempotency_key"}),
        ("booking_record", {"platform", "biz_type", "external_order_no"}),
    )
    problems: list[str] = []
    for table, columns in checks:
        found = any(
            set(item.get("column_names") or ()) == columns
            for item in inspector.get_unique_constraints(table)
        )
        if not found:
            problems.append(f"{table} 缺少唯一约束：{', '.join(sorted(columns))}")
    return problems


def main() -> None:
    parser = ArgumentParser(description="预检或创建 .env MySQL 的阶段 D 五张项目表")
    parser.add_argument("--apply", action="store_true", help="执行缺失表的 CREATE TABLE")
    args = parser.parse_args()
    url = database_url()
    print("target:", url.render_as_string(hide_password=True))
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    try:
        inspector = inspect(engine)
        before = set(inspector.get_table_names())
        project_names = {table.name for table in PROJECT_TABLES}
        missing = [table for table in STAGE_D_TABLES if table.name not in before]
        unexpected_missing = sorted(project_names - before - {table.name for table in missing})
        problems, existing_columns = verify_existing(inspector, before)
        if unexpected_missing:
            problems.append("已有阶段 A 项目表缺失：" + ", ".join(unexpected_missing))
        print("project_existing:", sorted(before & project_names))
        print("stage_d_to_create:", [table.name for table in missing])
        print("other_tables_untouched:", sorted(before - project_names))
        print("existing_project_columns_checked:", existing_columns)
        if problems:
            raise RuntimeError("预检失败；" + "；".join(problems))
        if not args.apply:
            print("dry_run: 未执行数据库 DDL；加 --apply 创建缺失表")
            return

        Base.metadata.create_all(engine, tables=STAGE_D_TABLES, checkfirst=True)
        post = inspect(engine)
        after = set(post.get_table_names())
        missing_after = sorted(project_names - after)
        problems_after, columns_checked = verify_existing(post, after)
        if missing_after:
            problems_after.append("仍缺少项目表：" + ", ".join(missing_after))
        if not missing_after:
            problems_after.extend(verify_constraints(post))
        if problems_after:
            raise RuntimeError("建表后验证失败；" + "；".join(problems_after))
        print("created:", [table.name for table in missing])
        print("verified_project_tables:", len(project_names))
        print("verified_commented_columns:", columns_checked)
        print("other_tables_untouched:", sorted(after - project_names))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
