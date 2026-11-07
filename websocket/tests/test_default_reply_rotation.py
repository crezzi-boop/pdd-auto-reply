# -*- coding: utf-8 -*-
"""默认回复多语句轮换（防拼多多重复拦截）回归测试。

覆盖：
- 公共纯逻辑：窗口内按配置顺序轮换、窗口内文本互不相同、窗口过期后可复用；
- 轮换器：注入内存存储时选句与预留记录一致、发送失败可释放名额。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from common.services.default_reply_variants import (
    choose_variant,
    decode_variants,
    encode_variants,
    normalize_rotate_minutes,
    normalize_variants,
    variation_suffix,
)
from engine.default_reply_rotation import DefaultReplyRotator

VARIANTS: List[str] = [
    "您好，客服稍后回复",
    "亲，稍等一下哦",
    "您好，正在为您查询",
]


def test_choose_variant_rotates_in_config_order() -> None:
    """窗口内优先按配置顺序取没发过的语句。"""
    now = datetime(2026, 11, 7, 9, 0, 0)
    recent: List[Tuple[str, Optional[datetime]]] = []
    picked = []
    for _ in range(3):
        choice = choose_variant(VARIANTS, recent, now, 600)
        assert choice is not None and not choice.varied
        picked.append(choice.text)
        now += timedelta(seconds=1)
        recent.append((choice.key, now))
    assert picked == VARIANTS


def test_choose_variant_never_repeats_exact_text_in_window() -> None:
    """消息量超过语句条数时，窗口内每次发送文本仍互不相同。"""
    now = datetime(2026, 11, 7, 9, 0, 0)
    recent: List[Tuple[str, Optional[datetime]]] = []
    texts = []
    for _ in range(12):
        choice = choose_variant(VARIANTS, recent, now, 600)
        assert choice is not None
        texts.append(choice.text)
        now += timedelta(seconds=1)
        recent.append((choice.key, now))
    assert len(set(texts)) == 12


def test_choose_variant_reuses_first_after_window() -> None:
    """超过窗口后历史记录作废，可重新从第 1 条开始。"""
    now = datetime(2026, 11, 7, 9, 0, 0)
    recent: List[Tuple[str, Optional[datetime]]] = [
        (choose_variant(VARIANTS, [], now, 600).key, now)  # type: ignore[union-attr]
    ]
    later = now + timedelta(minutes=11)
    choice = choose_variant(VARIANTS, recent, later, 600)
    assert choice is not None and choice.text == VARIANTS[0] and not choice.varied


def test_single_variant_still_differs_within_window() -> None:
    """历史单条配置也通过不可见变化保证窗口内不出现完全相同的文本。"""
    now = datetime(2026, 11, 7, 9, 0, 0)
    recent: List[Tuple[str, Optional[datetime]]] = []
    texts = []
    for _ in range(5):
        choice = choose_variant(["您好，在的"], recent, now, 600)
        assert choice is not None
        texts.append(choice.text)
        now += timedelta(seconds=1)
        recent.append((choice.key, now))
    assert len(set(texts)) == 5


def test_encode_decode_compat_with_legacy_single() -> None:
    """多条 JSON 存取与历史单条配置兼容。"""
    assert decode_variants(encode_variants(VARIANTS)) == VARIANTS
    assert decode_variants(None, "老的单条") == ["老的单条"]
    assert normalize_variants([" a ", "a", "", None, " b "]) == ["a", "b"]
    assert normalize_rotate_minutes("15") == 15
    assert normalize_rotate_minutes(999) == 10
    assert variation_suffix(2) != variation_suffix(3)


def test_rotator_pick_and_release_with_memory_store() -> None:
    """轮换器选句即预留记录；释放后该名额可再次被选中。"""
    base = datetime(2026, 11, 7, 9, 0, 0)
    records: Dict[int, Tuple[int, str, datetime]] = {}
    next_id = [1]

    def reader(shop_pk: int, cutoff: datetime):
        return [
            (key, sent_at)
            for _shop, key, sent_at in records.values()
            if _shop == shop_pk and sent_at >= cutoff
        ]

    def writer(shop_pk: int, key: str, sent_at: datetime):
        record_id = next_id[0]
        next_id[0] += 1
        records[record_id] = (shop_pk, key, sent_at)
        return record_id

    def releaser(record_id: int) -> None:
        records.pop(record_id, None)

    rotator = DefaultReplyRotator(
        reader=reader, writer=writer, releaser=releaser, now_func=lambda: base
    )
    texts = []
    for _ in range(5):
        choice = rotator.pick(1, VARIANTS, 10)
        assert choice is not None and choice.record_id is not None
        texts.append(choice.text)
        # 模拟最后一次发送失败：释放名额
        if len(texts) == 5:
            rotator.release(choice)
    assert len(set(texts)) == 5
    assert len(records) == 4
