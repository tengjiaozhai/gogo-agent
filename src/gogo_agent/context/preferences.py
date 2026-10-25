"""085：显式用户偏好，采用本地 SQL 存储，不让模型抽取或授权写入。"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import os
from typing import Literal, cast

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, String, Table, Text, create_engine, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from gogo_agent.auth.dependencies import get_current_user
from gogo_agent.auth.models import UserAccount
from gogo_agent.db.base import Base


class PreferenceWrite(BaseModel):
    """认证用户显式保存或更新一条长期偏好的请求。"""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    value: str = Field(..., min_length=1, max_length=500, description="用户明确要求记住的偏好；不构成政策或执行授权")
    source_message: str = Field(..., min_length=1, max_length=1000, description="本次显式设置的来源原文，可用于追溯与撤回")


class PreferenceView(PreferenceWrite):
    """当前仍然生效的一条本人长期偏好。"""
    key: Literal["transport", "hotel", "schedule", "other"] = Field(..., description="偏好分类；每类只保留最新有效值")
    updated_at: datetime = Field(..., description="最近显式更新的 UTC 时间")


class PreferenceRow(Base):
    """用户长期偏好表；按认证用户与分类唯一存储最新有效偏好。"""
    __tablename__ = "user_travel_preference"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="认证用户标识，模型不能指定")
    key: Mapped[str] = mapped_column(String(32), primary_key=True, comment="偏好分类")
    value: Mapped[str] = mapped_column(Text, nullable=False, comment="用户显式保存的偏好文本")
    source_message: Mapped[str] = mapped_column(Text, nullable=False, comment="本次设置来源原文")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="最近显式更新 UTC 时间")


class PreferenceStore:
    """唯一偏好入口；用户 ID 由认证入口传入，撤回后物理删除，不召回旧版本。"""
    def __init__(self, factory: sessionmaker[Session]):
        self.factory = factory

    def list(self, user_id: str) -> list[PreferenceView]:
        with self.factory() as session:
            rows = session.scalars(select(PreferenceRow).where(PreferenceRow.user_id == user_id).order_by(PreferenceRow.key))
            return [PreferenceView.model_validate(dict(key=row.key, value=row.value, source_message=row.source_message,
                                   updated_at=row.updated_at.replace(tzinfo=timezone.utc))) for row in rows]

    def put(self, user_id: str, key: str, request: PreferenceWrite) -> PreferenceView:
        # 同一契约同时约束 HTTP 和脚本调用，未知分类不得入库。
        view = PreferenceView.model_validate(dict(key=key, **request.model_dump(), updated_at=datetime.now(timezone.utc)))
        with self.factory() as session, session.begin():
            row = session.get(PreferenceRow, (user_id, key))
            if row is None:
                session.add(PreferenceRow(user_id=user_id, **view.model_dump()))
            else:
                row.value, row.source_message, row.updated_at = view.value, view.source_message, view.updated_at
        return view

    def delete(self, user_id: str, key: str) -> None:
        with self.factory() as session, session.begin():
            row = session.get(PreferenceRow, (user_id, key))
            if row is not None:
                session.delete(row)


@lru_cache(maxsize=1)
def get_preference_store() -> PreferenceStore:
    """默认持久化至用户本机 SQLite；不连接 .env 共享业务数据库，也不调用外部记忆服务。"""
    path = Path(os.getenv("GOGO_PREFERENCE_DB", "").strip() or "~/.gogo-agent/preferences.sqlite3").expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+pysqlite:///{path}", connect_args={"check_same_thread": False})
    cast(Table, PreferenceRow.__table__).create(engine, checkfirst=True)
    path.chmod(0o600)
    return PreferenceStore(sessionmaker(engine, expire_on_commit=False))


router = APIRouter(prefix="/api/preferences", tags=["preferences"])


@router.get("", response_model=list[PreferenceView], summary="读取本人的有效长期偏好")
def list_preferences(user: UserAccount = Depends(get_current_user), store: PreferenceStore = Depends(get_preference_store)):
    return store.list(user.user_id)


@router.put("/{key}", response_model=PreferenceView, summary="本人显式保存或替换一类长期偏好")
def put_preference(key: Literal["transport", "hotel", "schedule", "other"], request: PreferenceWrite,
                   user: UserAccount = Depends(get_current_user), store: PreferenceStore = Depends(get_preference_store)):
    return store.put(user.user_id, key, request)


@router.delete("/{key}", status_code=204, summary="本人撤回一类长期偏好")
def delete_preference(key: Literal["transport", "hotel", "schedule", "other"],
                      user: UserAccount = Depends(get_current_user), store: PreferenceStore = Depends(get_preference_store)) -> None:
    store.delete(user.user_id, key)
