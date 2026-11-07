# -*- coding: utf-8 -*-
"""
websocket.routes.guard —— 客户消息「必须有回复」的强制保底扫描接口
====================================================================
本文件用途：提供 ``POST /guard/scan``，由 scheduler 服务每 10 秒触发一次。
扫描最近一段时间内「最后一条消息是客户发的、且已超过 90 秒仍无任何商家消息」
的会话，直接发送一条轮换过的默认回复并落库为 out 消息。

为什么是独立路径（不复用消息消费器的决策链）：
- ``_do_transfer()`` 命中转人工关键词后直接 return，不回复；
- ``default_reply_once``（每客户只回一次）会抑制第 2 条及以后的默认回复；
- 风控限频、非营业时间、过滤规则、黑名单都会让决策链选择「不回复」；
- AI 异常、消息队列空转、容器重启等故障会让消息根本走不到决策链。
以上任何情况下平台仍会计「未回复」，因此保底必须绕开全部业务判断，
只认一个事实：这条客户消息之后有没有商家消息。

判重依据（无需建表）：
``pdd_chat_message`` 里每条 in 消息落库、每次自动回复 / 手动发送 / 保底发送
成功后都落库一条 out 消息（direction=out）。因此「某会话最新一条消息是 in」
就等价于「这条客户消息还没有任何回复」；保底发出后最新一条变 out，自然销账。
另加内存冷却字典双保险，防止落库失败或并发扫描导致重复发送。

安全边界：
- 只回补最近 GUARD_LOOKBACK_SECONDS 内的消息，避免服务重启后对历史会话群发；
- 单轮最多处理 GUARD_MAX_PER_SCAN 个会话；
- 店铺停用（status != 1）时不发送；
- 发送失败释放轮换名额并进入短冷却，不会每 10 秒轰炸拼多多接口。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter
from sqlalchemy import func, select

from channel_pdd.api.send_message import SendMessage
from common.db.repository import Repository, run_in_session
from common.db.session import session_scope
from common.models.log_models import ChatMessage, Conversation
from common.models.reply_models import DefaultReply
from common.models.shop_models import Shop
from common.schemas.common import ApiResponse, error_response, success_response
from common.services.default_reply_variants import decode_variants
from common.services.reply_guard_settings import (
    FALLBACK_REPLY_TEXT,
    GUARD_DEADLINE_SECONDS,
    GUARD_LOOKBACK_SECONDS,
    GUARD_MAX_PER_SCAN,
)
from common.utils.time_utils import now_beijing_naive
from engine.default_reply_rotation import DefaultReplyRotator

logger = logging.getLogger("websocket.routes.guard")

router = APIRouter(tags=["回复保底"])

# 方向常量（与 pdd_chat_message.direction 约定一致）。
_DIRECTION_IN: str = "in"
_DIRECTION_OUT: str = "out"
_SHOP_STATUS_ENABLED: int = 1

# 冷却时长（秒）：成功后长冷却（且已落库 out，正常不会再被扫到），失败后短冷却。
_COOLDOWN_SUCCESS_SECONDS: float = 300.0
_COOLDOWN_FAILURE_SECONDS: float = 60.0

# 内存冷却表：key=(shop_pk, customer_uid, 最后一条 in 消息 id) -> 下次可处理时刻。
_cooldown: Dict[Tuple[int, str, int], float] = {}
# 扫描互斥锁：防止并发扫描对同一会话重复发送。
_scan_lock = asyncio.Lock()


def _find_pending(session: Any, now: datetime) -> List[ChatMessage]:
    """查出「最后一条消息是客户发的且已超时未回复」的会话消息。"""
    cutoff = now - timedelta(seconds=GUARD_DEADLINE_SECONDS)
    lower = now - timedelta(seconds=GUARD_LOOKBACK_SECONDS)
    latest = (
        select(func.max(ChatMessage.id).label("max_id"))
        .group_by(ChatMessage.shop_pk, ChatMessage.customer_uid)
        .subquery()
    )
    stmt = (
        select(ChatMessage)
        .join(latest, ChatMessage.id == latest.c.max_id)
        .where(
            ChatMessage.direction == _DIRECTION_IN,
            ChatMessage.msg_time >= lower,
            ChatMessage.msg_time <= cutoff,
        )
        .order_by(ChatMessage.msg_time.asc())
        .limit(GUARD_MAX_PER_SCAN)
    )
    return list(session.execute(stmt).scalars().all())


def _pick_reply(shop_pk: int) -> Tuple[str, Any, DefaultReplyRotator]:
    """从店铺默认回复配置中轮换挑一条；无配置时返回固定中性兜底话术。"""
    rotator = DefaultReplyRotator()
    choice = None
    variants: List[str] = []
    rotate_minutes = None
    try:
        with session_scope() as session:
            row = session.execute(
                select(DefaultReply)
                .where(
                    DefaultReply.shop_pk == shop_pk,
                    DefaultReply.enabled.is_(True),
                )
                .order_by(DefaultReply.id.desc())
                .limit(1)
            ).scalars().first()
            if row is not None:
                variants = decode_variants(row.contents, row.content)
                rotate_minutes = row.rotate_minutes
    except Exception as exc:  # noqa: BLE001 - 读取失败仍须发出兜底话术
        logger.error("保底扫描读取默认回复失败: shop_pk=%s, %s", shop_pk, exc)
    if variants:
        choice = rotator.pick(shop_pk, variants, rotate_minutes)
        if choice is not None:
            return choice.text, choice, rotator
        return variants[0], None, rotator
    return FALLBACK_REPLY_TEXT, None, rotator


def _write_outbound(shop_pk: int, customer_uid: str, content: str) -> None:
    """把保底回复落库为 out 消息并刷新会话时间（销账，防止重复兜底）。"""
    def _do(session: Any) -> None:
        now = now_beijing_naive()
        conv_repo = Repository(Conversation, session)
        conversation = conv_repo.get_by(
            shop_pk=shop_pk, customer_uid=customer_uid
        )
        if conversation is None:
            conv_repo.create(
                shop_pk=shop_pk,
                customer_uid=customer_uid,
                last_msg_at=now,
                unread_count=0,
            )
        else:
            conv_repo.update(conversation.id, last_msg_at=now)
        Repository(ChatMessage, session).create(
            shop_pk=shop_pk,
            customer_uid=customer_uid,
            msg_id=None,
            direction=_DIRECTION_OUT,
            msg_type="text",
            content=content,
            order_context=None,
            goods_context=None,
            msg_time=now,
            created_by=None,
        )

    run_in_session(_do)


def _clean_cooldown(now_mono: float) -> None:
    """清理已过期的冷却键，避免字典无界增长。"""
    expired = [key for key, until in _cooldown.items() if until <= now_mono]
    for key in expired:
        del _cooldown[key]


@router.post(
    "/guard/scan",
    response_model=ApiResponse,
    summary="扫描超时未回复的客户消息并强制发送默认回复",
)
async def guard_scan() -> ApiResponse:
    """执行一次强制保底扫描（由 scheduler 每 10 秒触发）。

    Returns:
        统一响应体：data 含 scanned / sent / failed / skipped 计数。
    """
    async with _scan_lock:
        try:
            now = now_beijing_naive()
            with session_scope() as session:
                pending = _find_pending(session, now)
                # 与会话解绑后读取属性（expire_on_commit=False 已配置，仍显式取值更稳）。
                targets = [
                    (int(row.shop_pk), str(row.customer_uid), int(row.id))
                    for row in pending
                ]

            now_mono = time.monotonic()
            _clean_cooldown(now_mono)
            sent_count = 0
            failed_count = 0
            skipped_count = 0

            for shop_pk, customer_uid, msg_id in targets:
                key = (shop_pk, customer_uid, msg_id)
                if _cooldown.get(key, 0) > now_mono:
                    skipped_count += 1
                    continue

                # 停用店铺不发送；同时取出发消息所需的 shop_id / owner_user_id。
                with session_scope() as session:
                    shop = session.get(Shop, shop_pk)
                    enabled = shop is not None and int(shop.status or 0) == _SHOP_STATUS_ENABLED
                    shop_id = shop.shop_id if shop is not None else None
                    owner_user_id = shop.owner_user_id if shop is not None else None
                if not enabled or not shop_id:
                    _cooldown[key] = now_mono + _COOLDOWN_SUCCESS_SECONDS
                    skipped_count += 1
                    continue

                # 发送前二次确认：该会话最新消息必须仍是刚才扫到的那条 in。
                # 若已变成 out（人工 / 正常自动回复刚发出）或客户又发了新消息，
                # 本轮跳过，避免与自动回复重复发送触发拼多多重复回复拦截。
                with session_scope() as session:
                    latest_id = session.execute(
                        select(func.max(ChatMessage.id)).where(
                            ChatMessage.shop_pk == shop_pk,
                            ChatMessage.customer_uid == customer_uid,
                        )
                    ).scalar()
                if latest_id != msg_id:
                    skipped_count += 1
                    continue

                text, choice, rotator = _pick_reply(shop_pk)
                try:
                    sender = SendMessage(shop_id=shop_id, user_id=owner_user_id)
                    result = await asyncio.to_thread(
                        sender.send_text, customer_uid, text
                    )
                    sent = result is not None
                except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它会话
                    logger.error(
                        "保底回复发送异常: shop_pk=%s, 客户=%s, %s",
                        shop_pk, customer_uid, exc,
                    )
                    sent = False

                if sent:
                    try:
                        _write_outbound(shop_pk, customer_uid, text)
                    except Exception as exc:  # noqa: BLE001 - 落库失败仍算已发送
                        logger.error(
                            "保底回复已发出但落库失败: shop_pk=%s, 客户=%s, %s",
                            shop_pk, customer_uid, exc,
                        )
                    _cooldown[key] = now_mono + _COOLDOWN_SUCCESS_SECONDS
                    sent_count += 1
                    logger.info(
                        "保底回复已发送: shop_pk=%s, 客户=%s, 消息id=%s, 内容=%s",
                        shop_pk, customer_uid, msg_id, text[:80],
                    )
                else:
                    rotator.release(choice)
                    _cooldown[key] = now_mono + _COOLDOWN_FAILURE_SECONDS
                    failed_count += 1
                    logger.warning(
                        "保底回复发送失败，进入短冷却: shop_pk=%s, 客户=%s, 消息id=%s",
                        shop_pk, customer_uid, msg_id,
                    )

            data = {
                "scanned": len(targets),
                "sent": sent_count,
                "failed": failed_count,
                "skipped": skipped_count,
                "deadline_seconds": GUARD_DEADLINE_SECONDS,
            }
            if sent_count or failed_count:
                logger.info("保底扫描完成: %s", data)
            return success_response(data=data, message="保底扫描完成")
        except Exception as exc:  # noqa: BLE001 - 扫描整体异常不得打断调度器
            logger.error("保底扫描执行失败: %s", exc)
            return error_response(-1, f"保底扫描执行失败：{exc}")


__all__ = ["router"]
