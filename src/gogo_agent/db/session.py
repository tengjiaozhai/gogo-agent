"""Database engine, connection pooling, and session lifecycle management."""

import os
from collections.abc import Generator
from typing import Optional
from dotenv import load_dotenv
from sqlalchemy import create_engine, Engine
from sqlalchemy.orm import sessionmaker, Session

# 确保环境变量加载
load_dotenv()

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker[Session]] = None


def get_database_url() -> Optional[str]:
    """获取当前配置的数据库连接串。"""
    url = os.environ.get("GOGO_DATABASE_URL", "").strip()
    return url if url else None


def get_engine() -> Optional[Engine]:
    """获取或初始化全局 SQLAlchemy Engine（连接池）。"""
    global _engine
    if _engine is not None:
        return _engine

    url = get_database_url()
    if not url:
        return None

    _engine = create_engine(
        url,
        pool_pre_ping=True,       # 自动探测心跳保活
        pool_recycle=1800,        # 30分钟回收连接防止服务端断开
        pool_size=10,             # 基础连接池大小
        max_overflow=20,          # 最大突发连接数
        echo=False,
    )
    return _engine


def get_session_factory() -> Optional[sessionmaker[Session]]:
    """获取或初始化会话工厂。"""
    global _session_factory
    if _session_factory is not None:
        return _session_factory

    engine = get_engine()
    if not engine:
        return None

    _session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
        expire_on_commit=False,
    )
    return _session_factory


def get_db_session() -> Generator[Optional[Session], None, None]:
    """FastAPI 依赖注入：获取数据库会话并在请求结束时关闭。"""
    factory = get_session_factory()
    if not factory:
        yield None
        return

    session = factory()
    try:
        yield session
    finally:
        session.close()
