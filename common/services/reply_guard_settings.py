# -*- coding: utf-8 -*-
"""
common.services.reply_guard_settings —— 回复保底与人工介入设置
================================================================
本文件用途：集中定义「客户消息必须有商家回复」这条硬约束的常量、两个可配秒值
的校验规则，以及设置的读写存储，供 backend / websocket / scheduler 三服务共享。

业务背景（拼多多客服考核）：
- 考核时段 8:00-23:00 内，消费者当日任意一条消息未在 3 分钟内被回复，该消费者
  即计入「未回复」；同一消费者当天多条消息只累计一次，但一条都不能漏。
- 因此人工介入期间机器人只能「延迟回复」，绝不能「不回复」。

硬约束：
- ``GUARD_DEADLINE_SECONDS`` = 90 秒：客户消息发出后到这个时刻，若该消息之后
  仍无任何商家消息，独立保底扫描强制发一条轮换过的默认回复。该值不开放配置、
  不受人工介入 / AI 开关 / 全店暂停 / 风控 / 默认回复只回一次等任何开关影响。
  90 秒触发 + 10 秒扫描周期 + 发送重试余量，端到端约 90~110 秒，稳在平台
  3 分钟（180 秒）红线内，也短于用户要求的 2 分钟。

可配项（按店铺存于 pdd_sys_setting，秒为单位，无需建表 / 迁移）：
- ``manual_delay_seconds``：人工介入期间机器延迟多久再回，默认 30 秒，0~60。
  上限 60 秒是为了给 90 秒保底前的正常回复留足发送余量。
- ``manual_recover_seconds``：人工介入状态保持多久后自动恢复，默认 120 秒，
  30~3600；填 0 表示不自动恢复。计时按「店铺 + 客户」独立，从该客户最后一次
  人工消息起算，客服每回一句刷新一次。

实现约束：中文注释、参数化查询（经 common 仓储）、读取失败一律回退默认值，
绝不因设置读取失败而阻断回复主流程。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from common.db.repository import Repository
from common.db.session import session_scope
from common.models.setting_models import SysSetting

logger = logging.getLogger("common.reply_guard_settings")

# ----------------------------------------------------------------------
# 硬约束常量（不可配置、不可关闭）
# ----------------------------------------------------------------------
# 客户消息发出后，仍无商家消息就必须触发保底回复的秒数。
GUARD_DEADLINE_SECONDS: int = 90
# 保底扫描周期（秒）；实际触发时刻落在 90~100 秒之间。
GUARD_SCAN_INTERVAL_SECONDS: int = 10
# 只回补最近这段时间内的消息，避免服务重启后对历史会话群发。
GUARD_LOOKBACK_SECONDS: int = 600
# 单轮扫描最多处理的会话数，防止异常情况下一次发送过多消息。
GUARD_MAX_PER_SCAN: int = 50

# 未配置任何默认回复时使用的兜底话术（中性、不承诺、不含联系方式）。
FALLBACK_REPLY_TEXT: str = "亲，消息已收到，客服正在为您核实，稍后为您回复～"

# ----------------------------------------------------------------------
# 可配秒值：默认值与合法范围
# ----------------------------------------------------------------------
DEFAULT_MANUAL_DELAY_SECONDS: int = 30
DEFAULT_MANUAL_RECOVER_SECONDS: int = 120
DELAY_MIN_SECONDS: int = 0
DELAY_MAX_SECONDS: int = 60
RECOVER_MIN_SECONDS: int = 30
RECOVER_MAX_SECONDS: int = 3600
# 0 = 不自动恢复（人工状态一直保持到容器重启或客服重新介入）。
RECOVER_NEVER: int = 0

# 设置存储：沿用 pdd_sys_setting，按店铺一个 key，无需建表 / 改库结构。
_SETTING_KEY_PREFIX: str = "reply_guard:shop:"
_SCOPE_GLOBAL: str = "global"


def setting_key(shop_pk: int) -> str:
    """返回某店铺的设置键（按店铺隔离，各店铺可填不同秒数）。"""
    return f"{_SETTING_KEY_PREFIX}{int(shop_pk)}"


def default_config() -> Dict[str, int]:
    """返回未配置时的默认秒值字典（新字典，避免调用方修改污染默认值）。"""
    return {
        "manual_delay_seconds": DEFAULT_MANUAL_DELAY_SECONDS,
        "manual_recover_seconds": DEFAULT_MANUAL_RECOVER_SECONDS,
    }


def _as_int(value: Any, label: str) -> int:
    """把外部输入规整为整数秒；布尔、小数、非数字一律拒绝并给中文提示。"""
    if isinstance(value, bool):
        raise ValueError(f"{label}须为整数秒数")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{label}须为整数秒数")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}须为整数秒数") from None


def normalize_delay(value: Any) -> int:
    """校验并返回「人工介入后机器延迟回复」秒数（0~60）。"""
    seconds = _as_int(value, "机器延迟回复时间")
    if not DELAY_MIN_SECONDS <= seconds <= DELAY_MAX_SECONDS:
        raise ValueError(
            f"机器延迟回复时间须为 {DELAY_MIN_SECONDS}~{DELAY_MAX_SECONDS} 秒"
        )
    return seconds


def normalize_recover(value: Any) -> int:
    """校验并返回「人工介入自动恢复时长」秒数（30~3600，0 表示不自动恢复）。"""
    seconds = _as_int(value, "人工介入自动恢复时长")
    if seconds == RECOVER_NEVER:
        return seconds
    if not RECOVER_MIN_SECONDS <= seconds <= RECOVER_MAX_SECONDS:
        raise ValueError(
            f"人工介入自动恢复时长须为 {RECOVER_MIN_SECONDS}~{RECOVER_MAX_SECONDS} 秒，"
            "或填 0 表示不自动恢复"
        )
    return seconds


def load_config(session: Session, shop_pk: int) -> Dict[str, int]:
    """读取某店铺配置；无记录 / 值非法 / 读取异常时一律回退默认值。"""
    config = default_config()
    try:
        record = Repository(SysSetting, session).get_by(
            setting_key=setting_key(shop_pk),
            scope=_SCOPE_GLOBAL,
            owner_user_id=None,
        )
        if record is None or not record.setting_value:
            return config
        raw = json.loads(record.setting_value)
        if not isinstance(raw, dict):
            return config
        config["manual_delay_seconds"] = normalize_delay(
            raw.get("manual_delay_seconds", config["manual_delay_seconds"])
        )
        config["manual_recover_seconds"] = normalize_recover(
            raw.get("manual_recover_seconds", config["manual_recover_seconds"])
        )
    except Exception as exc:  # noqa: BLE001 - 设置读取失败不得阻断回复
        logger.warning("读取回复保底设置失败，按默认值处理: shop_pk=%s, %s", shop_pk, exc)
        return default_config()
    return config


def load_config_for_runtime(shop_pk: int) -> Dict[str, int]:
    """供 websocket 消费器 / 保底扫描读取（自带数据库会话，失败回退默认值）。"""
    try:
        with session_scope() as session:
            return load_config(session, shop_pk)
    except Exception as exc:  # noqa: BLE001 - 设置读取失败不得阻断回复
        logger.warning("读取回复保底设置异常，按默认值处理: shop_pk=%s, %s", shop_pk, exc)
        return default_config()


def save_config(
    session: Session,
    shop_pk: int,
    delay: Any,
    recover: Any,
    operator_id: Optional[int] = None,
) -> Dict[str, int]:
    """按店铺 upsert 两个秒值；入参非法时抛 ValueError（中文提示）。"""
    value = {
        "manual_delay_seconds": normalize_delay(delay),
        "manual_recover_seconds": normalize_recover(recover),
    }
    key = setting_key(shop_pk)
    payload = json.dumps(value, ensure_ascii=False)
    repo = Repository(SysSetting, session)
    record = repo.get_by(
        setting_key=key, scope=_SCOPE_GLOBAL, owner_user_id=None
    )
    if record is None:
        repo.create(
            setting_key=key,
            setting_value=payload,
            scope=_SCOPE_GLOBAL,
            owner_user_id=None,
            created_by=operator_id,
        )
    else:
        repo.update(record.id, setting_value=payload)
    return value


__all__ = [
    "GUARD_DEADLINE_SECONDS",
    "GUARD_SCAN_INTERVAL_SECONDS",
    "GUARD_LOOKBACK_SECONDS",
    "GUARD_MAX_PER_SCAN",
    "FALLBACK_REPLY_TEXT",
    "DEFAULT_MANUAL_DELAY_SECONDS",
    "DEFAULT_MANUAL_RECOVER_SECONDS",
    "DELAY_MIN_SECONDS",
    "DELAY_MAX_SECONDS",
    "RECOVER_MIN_SECONDS",
    "RECOVER_MAX_SECONDS",
    "RECOVER_NEVER",
    "setting_key",
    "default_config",
    "normalize_delay",
    "normalize_recover",
    "load_config",
    "load_config_for_runtime",
    "save_config",
]
