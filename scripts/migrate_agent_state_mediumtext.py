"""显式扩容已配置 MySQL/MariaDB 的 AgentState 存储，不修改历史状态。

从仓库根目录运行（与 API 共用 GOGO_DATABASE_URL，未设置时从 .env 加载）：
    PYTHONPATH=src .venv/bin/python scripts/migrate_agent_state_mediumtext.py
    PYTHONPATH=src .venv/bin/python scripts/migrate_agent_state_mediumtext.py --apply

默认只读预检；--apply 把既有 TEXT 改为 MEDIUMTEXT，保留列属性。
已是 MEDIUMTEXT 时不执行 DDL；其他列类型显式失败，不自动缩容。
"""

from argparse import ArgumentParser
import json

from sqlalchemy import Column, Engine, inspect, text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.schema import CreateColumn

from gogo_agent.db.session import get_engine


def inspect_state_data(engine: Engine) -> dict:
    """读取目标列的容量与原有属性；只接受 MySQL/MariaDB。"""
    if engine.dialect.name not in ("mysql", "mariadb"):
        raise ValueError("AgentState 扩容迁移只接受 MySQL/MariaDB")
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT DATA_TYPE AS data_type, IS_NULLABLE AS is_nullable,
                   COLUMN_DEFAULT AS column_default, CHARACTER_SET_NAME AS charset,
                   COLLATION_NAME AS collation, COLUMN_COMMENT AS comment,
                   CHARACTER_OCTET_LENGTH AS capacity_bytes
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agentscope_session'
                  AND COLUMN_NAME = 'state_data'
        """)).mappings().one_or_none()
    if row is None:
        raise ValueError("缺少 agentscope_session.state_data，请先初始化数据库")
    return dict(row)


def migrate_state_data(engine: Engine, *, apply: bool = False) -> dict:
    """仅扩容 TEXT；验证迁移后类型与原有字符集、排序规则等属性。"""
    before = inspect_state_data(engine)
    if before["data_type"] not in ("text", "mediumtext"):
        raise ValueError(f"不支持迁移列类型 {before['data_type']}，不会自动缩容")
    changed = apply and before["data_type"] == "text"
    if changed:
        existing = next(column for column in inspect(engine).get_columns("agentscope_session")
                        if column["name"] == "state_data")
        # SHOW CREATE TABLE 的反射结果保留 SQL 引号/表达式；information_schema 的原值不一定带引号。
        default = existing["default"]
        column = Column(
            "state_data", MEDIUMTEXT(charset=before["charset"], collation=before["collation"]),
            nullable=before["is_nullable"] == "YES",
            server_default=text(default) if default is not None else None,
            comment=before["comment"],
        )
        definition = str(CreateColumn(column).compile(dialect=engine.dialect))
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE agentscope_session MODIFY COLUMN " + definition))
    after = inspect_state_data(engine)
    if apply:
        if after["data_type"] != "mediumtext" or after["capacity_bytes"] != 16777215:
            raise RuntimeError("迁移后 state_data 未达到 MEDIUMTEXT 容量")
        for attribute in ("charset", "collation", "is_nullable", "column_default", "comment"):
            if before[attribute] != after[attribute]:
                raise RuntimeError(f"迁移改变了原有列属性：{attribute}")
    return {"before": before, "after": after, "changed": changed}


def main() -> None:
    parser = ArgumentParser(description="预检或扩容 AgentState 的 TEXT 存储列")
    parser.add_argument("--apply", action="store_true", help="执行 TEXT → MEDIUMTEXT 的显式迁移")
    args = parser.parse_args()
    engine = get_engine()
    if engine is None:
        raise SystemExit("未配置 GOGO_DATABASE_URL")
    try:
        # 不输出连接串、凭证或状态内容。
        print("target:", engine.url.host, engine.url.port, engine.url.database)
        print(json.dumps(migrate_state_data(engine, apply=args.apply), ensure_ascii=False))
        print("verified: MEDIUMTEXT" if args.apply else "dry_run: 未执行 DDL；加 --apply 扩容")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
