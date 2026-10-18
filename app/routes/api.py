from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import (
    IrcConfig,
    LlmConfig,
    MessageLog,
    get_engine,
)

router = APIRouter(tags=["api"])


# ---------- IRC ----------

class IrcConfigIn(BaseModel):
    host: str
    port: int
    tls: bool
    user: str
    password: str
    nick: str
    ident: str
    realname: str
    channels: str


@router.get("/irc")
def get_irc() -> IrcConfig:
    with Session(get_engine()) as s:
        cfg = s.get(IrcConfig, 1)
        if cfg is None:
            raise HTTPException(404)
        return cfg


@router.post("/irc")
def update_irc(body: IrcConfigIn) -> IrcConfig:
    with Session(get_engine()) as s:
        cfg = s.get(IrcConfig, 1)
        if cfg is None:
            cfg = IrcConfig(id=1)
            s.add(cfg)
        for k, v in body.model_dump().items():
            setattr(cfg, k, v)
        s.commit()
        s.refresh(cfg)
        return cfg


# ---------- LLM ----------

class LlmConfigIn(BaseModel):
    llm_url: str
    llm_api_key: str
    model: str
    max_tokens: int
    temperature: float
    system_prompt: str
    throttle_global_sec: float
    throttle_per_nick_sec: float
    web_search_enabled: bool
    query_intent_enabled: bool = True
    intent_extraction_prompt: str = ""
    humanization_prompt: str = ""


@router.get("/llm")
def get_llm() -> LlmConfig:
    with Session(get_engine()) as s:
        cfg = s.get(LlmConfig, 1)
        if cfg is None:
            raise HTTPException(404)
        return cfg


@router.post("/llm")
def update_llm(body: LlmConfigIn) -> LlmConfig:
    with Session(get_engine()) as s:
        cfg = s.get(LlmConfig, 1)
        if cfg is None:
            cfg = LlmConfig(id=1)
            s.add(cfg)
        for k, v in body.model_dump().items():
            setattr(cfg, k, v)
        s.commit()
        s.refresh(cfg)
        return cfg


# ---------- Messages ----------
# Note : les endpoints /triggers ont été retirés — le trigger keyword est
# désormais un champ de UserAcl (voir /users). Les anciennes tables Trigger
# sont laissées dormantes en DB, sans API pour les manipuler.

@router.get("/messages")
def recent_messages(limit: int = 100) -> list[MessageLog]:
    limit = max(1, min(limit, 500))
    with Session(get_engine()) as s:
        stmt = select(MessageLog).order_by(MessageLog.id.desc()).limit(limit)
        rows = list(s.exec(stmt))
        rows.reverse()
        return rows


# ---------- Status ----------

@router.get("/status")
def status() -> dict:
    from ..irc_client import supervisor
    client = supervisor.get_client()
    return {
        "irc_connected": bool(client and client.connected),
        "irc_nick": client.nickname if client else None,
        "server_time": datetime.utcnow().isoformat() + "Z",
    }
