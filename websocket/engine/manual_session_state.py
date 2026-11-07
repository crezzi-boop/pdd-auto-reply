# -*- coding: utf-8 -*-
"""
websocket.engine.manual_session_state —— 人工介入会话状态（按店铺 + 客户）
==========================================================================
本文件用途：以「店铺主键 + 客户 UID」为粒度记录该客户最近一次人工消息时刻，
供消息消费器判断当前是否处于人工介入状态、机器应延迟多久再自动回复。

设计要点：
- **按客户隔离**：客服接手 A 客户只影响 A；B、C 客户照常立即自动回复。
- **计时从最后一次人工消息起算**：客服每回一句就刷新该客户的计时，因此
  「自动恢复时长」衡量的是客服沉默了多久，不会聊到一半被系统切回机器人。
- **标记为纯内存**：容器重启后状态丢失，最多让相关客户提前回到立即回复，
  不影响 90 秒强制保底，也不影响其他客户，因此无需建表 / 加数据库列。
- **安全不依赖它**：无论本状态判定是否准确，独立保底扫描都会保证每条客户
  消息在 90 秒内有一条商家消息，所以状态识别不准也不会造成漏回复。

触发登记的两个来源：
1. 命中转人工关键词且转移接口调用成功（message_consumer._do_transfer）；
2. 在线聊天页手动发送消息成功（websocket.routes.messages.send_message）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from common.services.reply_guard_settings import RECOVER_NEVER
from common.utils.time_utils import now_beijing_naive

# 状态键清理阈值（秒）：仅防止字典无界增长，远大于任何合理的恢复时长。
_PRUNE_AFTER_SECONDS: int = 24 * 3600


@dataclass
class ManualSessionState:
    """按 (shop_pk, customer_uid) 记录最后一次人工消息时刻。"""

    _last_manual: Dict[Tuple[int, str], datetime] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def _key(shop_pk: Any, customer_uid: Any) -> Tuple[int, str]:
        """构造状态键；客户 UID 统一转字符串，避免同一客户不同类型键不命中。"""
        return (int(shop_pk), str(customer_uid))

    def mark(
        self,
        shop_pk: Any,
        customer_uid: Any,
        now: Optional[datetime] = None,
    ) -> None:
        """登记一次人工消息（刷新该客户的自动恢复计时）。"""
        if shop_pk is None or customer_uid is None:
            return
        moment = now or now_beijing_naive()
        with self._lock:
            self._last_manual[self._key(shop_pk, customer_uid)] = moment
            self._prune_locked(moment)

    def is_active(
        self,
        shop_pk: Any,
        customer_uid: Any,
        recover_seconds: int,
        now: Optional[datetime] = None,
    ) -> bool:
        """判断该客户当前是否仍处于人工介入状态。

        Args:
            shop_pk: 店铺主键。
            customer_uid: 客户 UID。
            recover_seconds: 自动恢复时长（秒）；0 表示不自动恢复。
            now: 当前时间（可注入，便于单元测试）。

        Returns:
            处于人工介入状态返回 True，否则 False。
        """
        if shop_pk is None or customer_uid is None:
            return False
        with self._lock:
            last = self._last_manual.get(self._key(shop_pk, customer_uid))
        if last is None:
            return False
        if int(recover_seconds) == RECOVER_NEVER:
            return True
        moment = now or now_beijing_naive()
        return (moment - last).total_seconds() < int(recover_seconds)

    def pending_delay(
        self,
        shop_pk: Any,
        customer_uid: Any,
        recover_seconds: int,
        delay_seconds: int,
        now: Optional[datetime] = None,
    ) -> int:
        """返回本条客户消息应延迟多少秒再进入自动回复决策链。

        人工介入状态内返回配置的延迟秒数；否则（或状态已自动恢复）返回 0，
        即立即按原有 AI / 关键词 / 默认回复逻辑处理。
        """
        if not self.is_active(shop_pk, customer_uid, recover_seconds, now=now):
            return 0
        return max(0, int(delay_seconds))

    def clear(self, shop_pk: Any, customer_uid: Any) -> None:
        """清除某客户的人工状态（供测试 / 未来手动结束接管使用）。"""
        with self._lock:
            self._last_manual.pop(self._key(shop_pk, customer_uid), None)

    def _prune_locked(self, now: datetime) -> None:
        """清理超过保留期的状态键（调用方须已持有锁）。"""
        cutoff = now - timedelta(seconds=_PRUNE_AFTER_SECONDS)
        expired = [key for key, value in self._last_manual.items() if value < cutoff]
        for key in expired:
            del self._last_manual[key]


# 进程级单例：websocket 服务内所有店铺消费器与 HTTP 路由共享同一张状态表。
manual_session_state = ManualSessionState()


__all__ = ["ManualSessionState", "manual_session_state"]
