# -*- coding: utf-8 -*-
"""
common.services.default_reply_variants —— 默认回复多语句轮换公共逻辑
====================================================================
本文件用途：为「按店铺配置多条默认回复 + 时间窗口内不重复轮换」提供跨服务复用的
纯逻辑（backend 负责配置校验与持久化，websocket 负责发送前选句）：

- ``normalize_variants``：规整用户输入（去空白、丢空行、去重、限长、限量）；
- ``encode_variants`` / ``decode_variants``：多条语句以 JSON 数组文本存入数据库，
  兼容历史单条配置（``content`` 字段）；
- ``normalize_rotate_minutes``：轮换窗口分钟数规整（默认 10 分钟，范围 1-120）；
- ``variant_key``：按回复正文生成稳定短指纹，用于记录「这条语句最近是否发过」；
- ``choose_variant``：核心选句算法 —— 优先选取「时间窗口内尚未发过」的语句；
  若窗口内所有语句都已用过，则取最久未用的一条，并追加不可见的零宽字符变化，
  保证同一店铺在窗口内每次默认回复的实际发送文本都不相同，避免拼多多重复拦截。

设计约束：本模块为纯逻辑、无 I/O（不访问数据库 / 不发送消息），便于属性测试与
backend / websocket 双端复用；时间统一使用「北京时间朴素 datetime」比较，与
common.models.base 的审计字段口径一致。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, List, Optional, Sequence, Tuple

# 默认轮换窗口（分钟）：同一店铺在该窗口内每次默认回复使用不同语句。
DEFAULT_ROTATE_MINUTES: int = 10
MIN_ROTATE_MINUTES: int = 1
MAX_ROTATE_MINUTES: int = 120
# 单个店铺最多可配置的默认回复条数，以及单条最大字符数（防止超长文本拖慢发送）。
# 后台以「一个输入框、一条一行」粘贴录入，故上限放宽到 100 条；对应的
# pdd_default_reply.contents 列使用 LONGTEXT，避免 JSON 文本超出 TEXT 的 64KB 限制。
MAX_VARIANTS: int = 100
MAX_VARIANT_LENGTH: int = 1000

# 零宽字符：仅用于「窗口内语句用尽」时生成肉眼不可见的变化，不改变回复语义。
_ZERO_WIDTH_ZERO: str = "\u200b"  # 零宽空格，编码二进制 0
_ZERO_WIDTH_ONE: str = "\u200c"  # 零宽不连字，编码二进制 1


def normalize_variants(
    values: Optional[Iterable[Any]] = None,
    fallback: Optional[str] = None,
) -> List[str]:
    """规整多条默认回复语句：去空白、丢空行、去重、限长、限量。

    Args:
        values: 用户输入的多条语句（可含 None / 非字符串）。
        fallback: 全部为空时回退的历史单条回复内容（``content`` 字段）。

    Returns:
        规整后的语句列表；无任何有效语句时返回空列表。
    """
    result: List[str] = []
    seen: set[str] = set()
    for item in list(values) if values is not None else []:
        if item is None:
            continue
        text = str(item).strip()
        if not text or text in seen:
            continue
        if len(text) > MAX_VARIANT_LENGTH:
            text = text[:MAX_VARIANT_LENGTH]
        seen.add(text)
        result.append(text)
        if len(result) >= MAX_VARIANTS:
            break
    if not result and fallback:
        text = str(fallback).strip()[:MAX_VARIANT_LENGTH]
        if text:
            result.append(text)
    return result


def encode_variants(variants: Sequence[str]) -> str:
    """把多条语句编码为数据库存储用的 JSON 数组文本。

    Args:
        variants: 已规整的语句列表。

    Returns:
        JSON 数组文本（保留中文，不转义为 \\uXXXX）。
    """
    return json.dumps(list(variants), ensure_ascii=False)


def decode_variants(raw: Any, fallback: Optional[str] = None) -> List[str]:
    """把数据库字段解码为多条语句，兼容历史单条配置与非法 JSON。

    Args:
        raw: ``contents`` 字段值（JSON 文本 / 列表 / None）。
        fallback: 解码失败或为空时回退的历史单条回复内容。

    Returns:
        规整后的语句列表。
    """
    if raw is None or raw == "":
        return normalize_variants(None, fallback)
    if isinstance(raw, (list, tuple)):
        return normalize_variants(raw, fallback)
    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return normalize_variants([str(raw)], fallback)
    if not isinstance(parsed, list):
        return normalize_variants([str(raw)], fallback)
    return normalize_variants(parsed, fallback)


def normalize_rotate_minutes(value: Any) -> int:
    """规整轮换窗口分钟数：非法 / 越界值回退到 10 分钟。

    Args:
        value: 用户输入的分钟数（可能是字符串 / None）。

    Returns:
        1-120 之间的整数分钟数。
    """
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ROTATE_MINUTES
    if minutes < MIN_ROTATE_MINUTES or minutes > MAX_ROTATE_MINUTES:
        return DEFAULT_ROTATE_MINUTES
    return minutes


def variant_key(text: str) -> str:
    """按回复正文生成稳定短指纹（16 位十六进制）。

    Args:
        text: 默认回复正文。

    Returns:
        sha1 前 16 位；相同正文恒得相同指纹。
    """
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class VariantChoice:
    """一次默认回复的选句结果。

    Attributes:
        text: 实际要发送的回复文本（可能带不可见变化）。
        key: 基础语句指纹（用于记录发送历史）。
        varied: 是否启用了「窗口内用尽」时的不可见变化。
        sequence: 该基础语句在窗口内被使用的序号（用于生成不同变化）。
    """

    text: str
    key: str
    varied: bool = False
    sequence: int = 1


def variation_suffix(sequence: int) -> str:
    """按使用序号生成互不相同的不可见后缀（零宽字符二进制编码）。

    Args:
        sequence: 该语句在窗口内的使用序号（≥1）。

    Returns:
        由零宽字符组成的后缀；不同序号必然得到不同后缀。
    """
    bits = bin(max(int(sequence), 1))[2:]
    return "".join(
        _ZERO_WIDTH_ZERO if bit == "0" else _ZERO_WIDTH_ONE for bit in bits
    )


def choose_variant(
    variants: Sequence[str],
    recent: Sequence[Tuple[str, Optional[datetime]]],
    now: datetime,
    window_seconds: float,
) -> Optional[VariantChoice]:
    """在「时间窗口内不重复」约束下选出一条默认回复语句。

    选句优先级：
    1. 按配置顺序取第一条「窗口内尚未发过」的语句（自然形成轮换）；
    2. 若窗口内所有语句都已发过，取最久未用的一条，并追加不可见变化，
       保证本次发送文本与窗口内任何一次都不完全相同。

    Args:
        variants: 已规整的多条默认回复语句。
        recent: 窗口附近的发送历史，元素为 (语句指纹, 发送时间)，按发送先后
            （记录主键）升序排列。顺序作为「同一秒内时间戳相同」时的稳定依据。
        now: 当前北京时间（朴素 datetime）。
        window_seconds: 不重复窗口长度（秒）。

    Returns:
        选句结果；无任何有效语句时返回 None。
    """
    items = [v for v in variants if isinstance(v, str) and v.strip()]
    if not items:
        return None

    window = timedelta(seconds=max(1.0, float(window_seconds)))
    cutoff = now - window
    last_used: dict[str, datetime] = {}
    last_order: dict[str, int] = {}
    counts: dict[str, int] = {}
    for order, (key, sent_at) in enumerate(recent):
        if not key or sent_at is None or sent_at < cutoff:
            continue
        counts[key] = counts.get(key, 0) + 1
        # MySQL DATETIME 默认不带小数秒：同一秒内多条记录时间相同，故额外按
        # 记录顺序判定「最久未用」，保证超出语句条数时仍能均匀轮换。
        last_order[key] = order
        if key not in last_used or sent_at > last_used[key]:
            last_used[key] = sent_at

    # 1) 优先选未在本窗口内使用过的语句（按配置顺序，天然轮换）。
    for text in items:
        key = variant_key(text)
        if key not in last_used:
            return VariantChoice(text=text, key=key, varied=False, sequence=1)

    # 2) 全部用过：选最久未使用的一条，追加不可见变化保证文本不同。
    # 次序键：(最后使用时间, 最后使用顺序, 配置下标) —— 同一秒时间戳相同则靠
    # 记录顺序打破平局，避免始终固定选第一条。
    def _recency(index: int) -> tuple[datetime, int, int]:
        key = variant_key(items[index])
        return (last_used.get(key, datetime.min), last_order.get(key, -1), index)

    chosen_index = min(range(len(items)), key=_recency)
    base_text = items[chosen_index]
    base_key = variant_key(base_text)
    sequence = counts.get(base_key, 0) + 1
    return VariantChoice(
        text=base_text + variation_suffix(sequence),
        key=base_key,
        varied=True,
        sequence=sequence,
    )


__all__ = [
    "DEFAULT_ROTATE_MINUTES",
    "MIN_ROTATE_MINUTES",
    "MAX_ROTATE_MINUTES",
    "MAX_VARIANTS",
    "MAX_VARIANT_LENGTH",
    "normalize_variants",
    "encode_variants",
    "decode_variants",
    "normalize_rotate_minutes",
    "variant_key",
    "VariantChoice",
    "variation_suffix",
    "choose_variant",
]
