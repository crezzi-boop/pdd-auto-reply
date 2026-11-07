# -*- coding: utf-8 -*-
"""
websocket.engine.default_reply_rotation —— 默认回复多语句轮换器
================================================================
本文件用途：在真正发送默认回复之前，为「同一店铺」挑选一条「时间窗口内未发过」
的兜底语句，避免拼多多对重复回复做拦截导致回复超时。

工作机制：
1. 选句：读取本店铺最近的默认回复发送记录（``pdd_default_reply_rotation_record``），
   调用公共纯逻辑 ``common.services.default_reply_variants.choose_variant``：
   - 优先取窗口内尚未发过的语句（按用户配置顺序，天然形成轮换）；
   - 若窗口内所有语句都已发过（消息量超过语句条数），取最久未用的一条并追加
     不可见的零宽字符变化，保证发送文本仍与窗口内任何一次都不完全相同；
2. 预留：选中后立即在同一事务里登记一条发送记录，避免并发消息选中同一条；
3. 释放：若发送失败，删除该预留记录，不占用轮换名额；
4. 清理：每次读取时顺带清除本店铺已过窗口的旧记录，表不会无限增长。

窗口默认 10 分钟，可在店铺「默认回复」设置里调整（1-120 分钟）。发送记录存于
MySQL，容器重启后轮换状态不丢失；所有读写经 SQLAlchemy 参数化构造，无拼接 SQL。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, List, Optional, Sequence, Tuple

from sqlalchemy import delete, select

from common.db.repository import Repository, run_in_session
from common.models.reply_models import DefaultReplyRotationRecord
from common.services.default_reply_variants import (
    choose_variant,
    normalize_rotate_minutes,
    normalize_variants,
)
from common.utils.time_utils import now_beijing_naive

logger = logging.getLogger("websocket.engine.default_reply_rotation")

# 依赖注入用的函数类型（便于单元测试以内存桩替换数据库）。
RecentReader = Callable[[int, datetime], List[Tuple[str, Optional[datetime]]]]
RecordWriter = Callable[[int, str, datetime], Optional[int]]
RecordReleaser = Callable[[int], None]
NowFunc = Callable[[], datetime]


@dataclass(frozen=True)
class RotationChoice:
    """一次默认回复的选句结果（含预留记录主键，供发送失败时释放）。

    Attributes:
        text: 实际要发送的回复文本。
        key: 基础语句指纹。
        varied: 是否启用了「窗口内用尽」的不可见变化。
        record_id: 已预留的发送记录主键；None 表示未落库（例如注入桩）。
    """

    text: str
    key: str
    varied: bool = False
    record_id: Optional[int] = None


# ----------------------------------------------------------------------
# 默认数据库实现
# ----------------------------------------------------------------------
def _db_read_recent(
    shop_pk: int, cutoff: datetime
) -> List[Tuple[str, Optional[datetime]]]:
    """读取店铺在窗口内的发送记录，并顺带清除已过期的旧记录。

    Args:
        shop_pk: 店铺主键 shop.id。
        cutoff: 窗口起点（北京时间朴素 datetime）。

    Returns:
        (语句指纹, 发送时间) 列表。
    """

    def _do(session: Any) -> List[Tuple[str, Optional[datetime]]]:
        model = DefaultReplyRotationRecord
        # 轮换记录属于内部状态而非业务数据，过期记录可直接物理清理，避免表膨胀。
        session.execute(
            delete(model).where(
                model.shop_pk == shop_pk, model.created_at < cutoff
            )
        )
        # 按主键升序返回：MySQL DATETIME 不含小数秒，同一秒内多条记录时间相同，
        # 选句逻辑需要用记录顺序判定「最久未用」。
        stmt = (
            select(model.variant_key, model.created_at)
            .where(model.shop_pk == shop_pk, model.created_at >= cutoff)
            .order_by(model.id.asc())
        )
        rows = session.execute(stmt).all()
        return [(str(row[0]), row[1]) for row in rows]

    return run_in_session(_do)


def _db_write_record(
    shop_pk: int, variant_key: str, sent_at: datetime
) -> Optional[int]:
    """登记一次默认回复发送，返回记录主键（供失败时释放）。

    Args:
        shop_pk: 店铺主键 shop.id。
        variant_key: 基础语句指纹。
        sent_at: 发送时刻（北京时间朴素 datetime）。

    Returns:
        新增记录主键。
    """

    def _do(session: Any) -> Optional[int]:
        record = Repository(DefaultReplyRotationRecord, session).create(
            shop_pk=shop_pk, variant_key=variant_key, created_at=sent_at
        )
        return record.id

    return run_in_session(_do)


def _db_release_record(record_id: int) -> None:
    """删除一条预留记录（发送失败时调用）。

    Args:
        record_id: 预留记录主键。
    """

    def _do(session: Any) -> None:
        session.execute(
            delete(DefaultReplyRotationRecord).where(
                DefaultReplyRotationRecord.id == record_id
            )
        )

    run_in_session(_do)


# ----------------------------------------------------------------------
# 轮换器
# ----------------------------------------------------------------------
class DefaultReplyRotator:
    """按店铺在「不重复窗口」内轮换挑选默认回复语句。"""

    def __init__(
        self,
        *,
        reader: Optional[RecentReader] = None,
        writer: Optional[RecordWriter] = None,
        releaser: Optional[RecordReleaser] = None,
        now_func: Optional[NowFunc] = None,
    ) -> None:
        """构造轮换器；依赖可注入以便测试。

        Args:
            reader: 读取窗口内发送记录的函数；缺省走 MySQL。
            writer: 登记发送记录的函数；缺省走 MySQL。
            releaser: 删除预留记录的函数；缺省走 MySQL。
            now_func: 当前时间函数；缺省取北京时间。
        """
        self._reader = reader or _db_read_recent
        self._writer = writer or _db_write_record
        self._releaser = releaser or _db_release_record
        self._now = now_func or now_beijing_naive

    def pick(
        self,
        shop_pk: int,
        variants: Sequence[str],
        rotate_minutes: Any = None,
    ) -> Optional[RotationChoice]:
        """挑选一条窗口内未发过的默认回复，并立即预留记录。

        Args:
            shop_pk: 店铺主键 shop.id。
            variants: 用户配置的多条默认回复语句。
            rotate_minutes: 不重复窗口分钟数（1-120，默认 10）。

        Returns:
            选句结果；无有效语句或数据库异常时返回 None（上层回退到原单条回复）。
        """
        items = normalize_variants(variants)
        if not items:
            return None
        window_seconds = normalize_rotate_minutes(rotate_minutes) * 60
        now = self._now()
        cutoff = now - timedelta(seconds=window_seconds)
        try:
            recent = self._reader(shop_pk, cutoff)
            chosen = choose_variant(items, recent, now, window_seconds)
            if chosen is None:
                return None
            record_id = self._writer(shop_pk, chosen.key, now)
            if chosen.varied:
                logger.warning(
                    "默认回复在 %s 分钟窗口内已全部用过，本次启用不可见变化: shop_pk=%s, 第 %s 次",
                    window_seconds // 60,
                    shop_pk,
                    chosen.sequence,
                )
            return RotationChoice(
                text=chosen.text,
                key=chosen.key,
                varied=chosen.varied,
                record_id=record_id,
            )
        except Exception as exc:  # noqa: BLE001 - 轮换失败不得阻断回复
            logger.error("默认回复轮换选句失败: shop_pk=%s, %s", shop_pk, exc)
            return None

    def release(self, choice: Optional[RotationChoice]) -> None:
        """发送失败时释放预留下的名额。

        Args:
            choice: ``pick`` 的返回值；None 或没有记录主键时为空操作。
        """
        if choice is None or choice.record_id is None:
            return
        try:
            self._releaser(choice.record_id)
        except Exception as exc:  # noqa: BLE001 - 释放失败只记日志
            logger.error("释放默认回复轮换记录失败: %s", exc)


__all__ = ["DefaultReplyRotator", "RotationChoice"]
