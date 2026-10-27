"""显式给已配置 MySQL/MariaDB 的 itinerary_plan 增加会话列与审批单列，并可回填历史行。

从仓库根目录运行（与 API 共用 GOGO_DATABASE_URL，未设置时从 .env 加载）：
    PYTHONPATH=src .venv/bin/python scripts/migrate_itinerary_plan_session.py
    PYTHONPATH=src .venv/bin/python scripts/migrate_itinerary_plan_session.py --apply
    PYTHONPATH=src .venv/bin/python scripts/migrate_itinerary_plan_session.py --apply --backfill

默认只读预检：报告列/索引是否存在、可回填候选与冲突。
--apply 加列与索引；--backfill 才写历史行，无法证明的保持 NULL。
两列回填证据不同：order_id 直接取 state_json 里的同名字段；session_id 只接受同一 user_id
且计划 id 出现在该会话 AgentState 中的证据，冲突或缺失一律跳过。
"""

from argparse import ArgumentParser
import json
import re

from sqlalchemy import Engine, text

from gogo_agent.db.session import get_engine

TABLE = "itinerary_plan"
PLAN_ID = re.compile(r"\b[0-9a-f]{32}\b")
# 列定义：(列名, 列注释, 索引名, DDL 中的位置)
COLUMNS = (
    ("session_id", "创建计划的聊天会话标识，由服务端从可信请求上下文注入；独立脚本或旧数据为空",
     "idx_plan_user_session_updated", "AFTER user_id"),
    ("order_id", "关联的本人差旅单标识，只保存引用；未绑定审批单时为空",
     "idx_plan_user_order", "AFTER session_id"),
)


def inspect_columns(engine: Engine) -> dict:
    """读取目标表与两列的存在性；只接受 MySQL/MariaDB。"""
    if engine.dialect.name not in ("mysql", "mariadb"):
        raise ValueError("计划表迁移只接受 MySQL/MariaDB")
    with engine.connect() as connection:
        table = connection.execute(text("""
            SELECT COUNT(*) FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t
        """), {"t": TABLE}).scalar()
        if not table:
            raise ValueError(f"缺少 {TABLE}，请先运行阶段 E 的建表迁移")
        total = connection.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()
        result: dict[str, dict] = {"total_rows": total}
        for name, _comment, index, _position in COLUMNS:
            column = connection.execute(text("""
                SELECT DATA_TYPE AS data_type, IS_NULLABLE AS is_nullable,
                       CHARACTER_MAXIMUM_LENGTH AS max_length, COLUMN_COMMENT AS comment
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c
            """), {"t": TABLE, "c": name}).mappings().one_or_none()
            present = connection.execute(text("""
                SELECT INDEX_NAME AS name FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND INDEX_NAME = :i
            """), {"t": TABLE, "i": index}).scalar()
            empty = (connection.execute(
                text(f"SELECT COUNT(*) FROM {TABLE} WHERE {name} IS NULL")).scalar() if column is not None else total)
            result[name] = {"column": dict(column) if column is not None else None,
                            "index_present": present is not None, "rows_without_value": empty}
    return result


def migrate_columns(engine: Engine, *, apply: bool = False) -> dict:
    """缺失时才加列与索引；已存在的列类型不符则显式失败，不自动改类型。"""
    before = inspect_columns(engine)
    added, indexed = [], []
    for name, comment, index, position in COLUMNS:
        column = before[name]["column"]
        if column is not None and (column["data_type"] != "varchar" or column["max_length"] != 64):
            raise ValueError(f"{TABLE}.{name} 已是 {column['data_type']}({column['max_length']})，不自动改类型")
        if apply and column is None:
            with engine.begin() as connection:
                connection.execute(text(
                    f"ALTER TABLE {TABLE} ADD COLUMN {name} VARCHAR(64) NULL COMMENT '{comment}' {position}"
                ))
            added.append(name)
        if apply and not before[name]["index_present"]:
            with engine.begin() as connection:
                connection.execute(text(f"CREATE INDEX {index} ON {TABLE} (user_id, {name}, updated_at)"))
            indexed.append(name)
    after = inspect_columns(engine)
    if apply:
        missing = [name for name, *_ in COLUMNS if after[name]["column"] is None]
        if missing:
            raise RuntimeError(f"迁移后仍缺少列：{missing}")
    return {"before": before, "after": after, "added_columns": added, "created_indexes": indexed}


def plan_session_evidence(engine: Engine) -> dict:
    """从 agentscope_session 的会话键与 state_data 提取「计划属于哪个聊天会话」的证据。

    会话键形如 4:u001:session_1792489151301:ItineraryPlanAgent；只取最后三段做归属，
    用户标识必须与计划行一致，否则该证据作废。列还没建时预检仍可运行。
    """
    has_column = inspect_columns(engine)["session_id"]["column"] is not None
    with engine.connect() as connection:
        states = connection.execute(text(
            "SELECT session_id, state_data FROM agentscope_session"
        )).mappings().all()
        selected = "plan_id, user_id, session_id" if has_column else "plan_id, user_id, NULL AS session_id"
        plans = connection.execute(text(f"SELECT {selected} FROM {TABLE}")).mappings().all()
    claims: dict[str, list[dict]] = {}
    for row in states:
        parts = row["session_id"].split(":")
        if len(parts) < 3:
            continue
        agent, chat_session, user_id = parts[-1], parts[-2], parts[-3]
        for plan_id in PLAN_ID.findall(row["state_data"] or ""):
            claims.setdefault(plan_id, []).append(
                {"user_id": user_id, "session_id": chat_session, "agent": agent}
            )
    assignable, conflicts, unmatched = [], [], []
    for plan in plans:
        found = claims.get(plan["plan_id"], [])
        distinct = {(c["user_id"], c["session_id"]) for c in found if c["user_id"] == plan["user_id"]}
        if not found:
            unmatched.append(plan["plan_id"])
        elif len(distinct) == 1:
            assignable.append({"plan_id": plan["plan_id"], "user_id": plan["user_id"],
                               "session_id": next(iter(distinct))[1],
                               "evidence": sorted({c["agent"] for c in found if c["user_id"] == plan["user_id"]}),
                               "already": plan["session_id"]})
        elif not distinct:
            conflicts.append({"plan_id": plan["plan_id"], "reason": "证据来自其他用户，拒绝回填"})
        else:
            conflicts.append({"plan_id": plan["plan_id"], "reason": "同一计划出现在多个会话",
                              "candidates": sorted(s for _, s in distinct)})
    return {"assignable": assignable, "conflicts": conflicts, "unmatched": unmatched}


def plan_order_evidence(engine: Engine) -> dict:
    """order_id 的证据就在 state_json 里，按用户身份逐行读回即可，不需要跨表推断。"""
    has_column = inspect_columns(engine)["order_id"]["column"] is not None
    with engine.connect() as connection:
        selected = "plan_id, user_id, order_id, " if has_column else "plan_id, user_id, NULL AS order_id, "
        rows = connection.execute(text(
            f"SELECT {selected}"
            "JSON_UNQUOTE(JSON_EXTRACT(state_json, '$.order_id')) AS snapshot_order "
            f"FROM {TABLE}"
        )).mappings().all()
    assignable, conflicts = [], []
    for row in rows:
        snapshot = row["snapshot_order"]
        if not snapshot or snapshot == "null":
            continue
        current = row["order_id"]
        if current is None or current == snapshot:
            assignable.append({"plan_id": row["plan_id"], "user_id": row["user_id"],
                               "order_id": snapshot, "already": current})
        else:
            conflicts.append({"plan_id": row["plan_id"], "reason": "列与快照不一致",
                              "column": current, "snapshot": snapshot})
    return {"assignable": assignable, "conflicts": conflicts}


def backfill(engine: Engine, *, apply: bool = False) -> dict:
    """只写有单一证据的行；其余保持 NULL，由后续正常写入补齐。"""
    sessions = plan_session_evidence(engine)
    pending_sessions = [item for item in sessions["assignable"] if item["already"] is None]
    orders = plan_order_evidence(engine)
    pending_orders = [item for item in orders["assignable"] if item["already"] is None]
    if apply:
        with engine.begin() as connection:
            for item in pending_sessions:
                connection.execute(text(
                    f"UPDATE {TABLE} SET session_id = :s WHERE plan_id = :p AND user_id = :u AND session_id IS NULL"
                ), {"s": item["session_id"], "p": item["plan_id"], "u": item["user_id"]})
            for item in pending_orders:
                connection.execute(text(
                    f"UPDATE {TABLE} SET order_id = :o WHERE plan_id = :p AND user_id = :u AND order_id IS NULL"
                ), {"o": item["order_id"], "p": item["plan_id"], "u": item["user_id"]})
    return {"applied": bool(apply),
            "session_id_would_update": len(pending_sessions), **sessions,
            "order_id_would_update": len(pending_orders), **orders}


def main() -> None:
    parser = ArgumentParser(description="预检或补上 itinerary_plan 的会话列与审批单列，并可选回填历史行")
    parser.add_argument("--apply", action="store_true", help="执行加列与索引等 DDL")
    parser.add_argument("--backfill", action="store_true", help="按历史证据回填两列，需配合 --apply")
    args = parser.parse_args()
    engine = get_engine()
    if engine is None:
        raise SystemExit("未配置 GOGO_DATABASE_URL")
    result = migrate_columns(engine, apply=args.apply)
    if args.backfill:
        result["backfill"] = backfill(engine, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
