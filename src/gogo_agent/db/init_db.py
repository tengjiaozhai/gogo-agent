"""Database schema initialization and seed data population."""

import sys
from typing import Optional
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from pathlib import Path

# 确保在 PyCharm 中直接右键执行脚本时，src 目录在 sys.path 中
_src_dir = str(Path(__file__).resolve().parents[2])
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from gogo_agent.db.base import Base
from gogo_agent.db.models import UserAccountModel
from gogo_agent.db.session import get_engine, get_session_factory



def init_database(engine: Optional[Engine] = None, seed_users: bool = True) -> bool:
    """初始化数据库表结构并填充初始脱敏测试数据。

    安全设计：
    1. 仅针对当前模型定义的表执行 CREATE TABLE IF NOT EXISTS，绝不影响数据库中已有的其他业务表。
    2. 幂等执行：多次运行不会重复插入种子用户。
    """
    eng = engine or get_engine()
    if eng is None:
        print("[DB] 未配置 GOGO_DATABASE_URL，跳过数据库建表初始化。")
        return False

    print(f"[DB] 正在连接目标数据库初始化表结构: {eng.url.render_as_string(hide_password=True)}")
    # 创建所有注册在 Base 上的表
    Base.metadata.create_all(bind=eng)
    
    # 确保 chat_message 表中存在 extra, feedback, feedback_at 字段 (兼容已有环境)
    from sqlalchemy import text
    with eng.connect() as conn:
        for col_ddl in [
            "ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS extra TEXT NULL",
            "ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS feedback VARCHAR(16) NULL",
            "ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS feedback_at DATETIME NULL",
        ]:
            try:
                conn.execute(text(col_ddl))
                conn.commit()
            except Exception:
                pass

    print("[DB] 表结构创建/检查完成 (user_account, chat_conversation, chat_message, agentscope_session)。")

    if seed_users:
        factory = get_session_factory()
        if factory:
            with factory() as session:
                _seed_initial_users(session)
    return True


def _seed_initial_users(session: Session) -> None:
    """注入 Java 版 schema.sql 规定的 5 个初始脱敏账号（若尚未存在）。"""
    existing_count = session.scalar(select(UserAccountModel).limit(1))
    if existing_count is not None:
        print("[DB] user_account 表中已存在用户数据，跳过种子初始化。")
        return

    seeds = [
        UserAccountModel(
            user_id="u_001",
            username="admin",
            password="123456",
            real_name="系统管理员",
            role="ADMIN",
        ),
        UserAccountModel(
            user_id="u001",
            username="alice",
            password="123456",
            real_name="张三",
            role="USER",
        ),
        UserAccountModel(
            user_id="u002",
            username="bob",
            password="123456",
            real_name="李四",
            role="USER",
        ),
        UserAccountModel(
            user_id="u003",
            username="charlie",
            password="123456",
            real_name="王五",
            role="USER",
        ),
        UserAccountModel(
            user_id="u004",
            username="david",
            password="123456",
            real_name="赵六",
            role="USER",
        ),
    ]
    session.add_all(seeds)
    session.commit()
    print("[DB] 成功导入 5 个初始种子用户 (admin, alice, bob, charlie, david)。")


if __name__ == "__main__":
    success = init_database()
    if not success:
        sys.exit(1)
    print("[DB] 数据库初始化成功！")
