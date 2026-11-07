# -*- coding: utf-8 -*-
"""人工介入登记单元测试（修复点：拼多多后台真人手发消息须刷新人工介入状态）。

覆盖：
- 真人客服在拼多多后台 / APP 手动回复客户（from 带 cs_uid）→ 登记人工介入，
  下游延迟回复闸门生效；
- 本系统自动回复经拼多多回推的 mall_cs 报文（无 cs_uid）→ 不登记，
  避免系统自刷新人工介入状态。
"""
import asyncio

from engine.manual_session_state import manual_session_state
from test_message_consumer import _make_consumer, _runtime


def _raw_cs(content, cs_uid=None, to_uid="cust_1"):
    """构造一条客服发出的 push 报文；cs_uid 存在即代表真人手发。"""
    return {
        "response": "push",
        "message": {
            "type": 0,
            "content": content,
            "from": {
                "role": "mall_cs",
                "uid": "cs_1",
                **({"cs_uid": cs_uid} if cs_uid else {}),
            },
            "to": {"role": "user", "uid": to_uid},
            "msg_id": "m_h",
        },
    }


def test_human_reply_from_pdd_backend_marks_manual_state():
    """真人后台回客户 -> 人工介入状态 active。"""
    consumer, _, _, _ = _make_consumer(_runtime())
    manual_session_state.clear(1, "cust_1")
    outcome = asyncio.run(consumer.consume_raw(_raw_cs("在呢亲", cs_uid="CS123")))
    # 客服消息不进入自动回复链路，handled=False 属正常。
    assert outcome.handled is False
    assert manual_session_state.is_active(1, "cust_1", 120) is True


def test_system_auto_reply_echo_does_not_mark():
    """系统自动回推（无 cs_uid）-> 不登记人工介入状态。"""
    consumer, _, _, _ = _make_consumer(_runtime())
    manual_session_state.clear(1, "cust_1")
    outcome = asyncio.run(consumer.consume_raw(_raw_cs("亲，感谢咨询")))
    assert outcome.handled is False
    assert manual_session_state.is_active(1, "cust_1", 120) is False
