# -*- coding: utf-8 -*-
"""
websocket.tests.test_reply_guard —— 回复保底配置与人工介入状态机单元测试
========================================================================
覆盖：
1. 两个秒值的默认值、边界与非法输入（前端 / 后端共用同一套校验口径）；
2. 90 秒硬约束常量不超出用户要求的 2 分钟；
3. 人工介入状态按「店铺 + 客户」隔离、按最后一次人工消息刷新、
   超时自动恢复、0 表示不自动恢复、延迟只在人工期内生效。
"""
from datetime import datetime, timedelta

import pytest

from common.services import reply_guard_settings as cfg
from engine.manual_session_state import ManualSessionState

_T0 = datetime(2026, 11, 7, 10, 0, 0)


def _t(seconds: int) -> datetime:
    """返回相对基准时刻偏移若干秒的时间。"""
    return _T0 + timedelta(seconds=seconds)


class TestGuardConstants:
    """硬约束常量与默认值。"""

    def test_deadline_within_two_minutes(self):
        assert cfg.GUARD_DEADLINE_SECONDS == 90
        assert cfg.GUARD_DEADLINE_SECONDS <= 120
        # 触发 + 扫描周期 + 发送余量仍应小于平台 3 分钟红线。
        assert cfg.GUARD_DEADLINE_SECONDS + cfg.GUARD_SCAN_INTERVAL_SECONDS < 180

    def test_defaults(self):
        assert cfg.default_config() == {
            "manual_delay_seconds": 30,
            "manual_recover_seconds": 120,
        }
        assert cfg.DEFAULT_MANUAL_RECOVER_SECONDS == 120

    def test_default_config_returns_fresh_dict(self):
        first = cfg.default_config()
        first["manual_delay_seconds"] = 999
        assert cfg.default_config()["manual_delay_seconds"] == 30

    def test_setting_key_is_per_shop(self):
        assert cfg.setting_key(1) != cfg.setting_key(2)


class TestDelayValidation:
    """人工介入后机器延迟回复：0~60 秒整数。"""

    @pytest.mark.parametrize("value", [0, 1, 30, 60, "45"])
    def test_valid(self, value):
        assert cfg.normalize_delay(value) == int(value)

    @pytest.mark.parametrize("value", [-1, 61, 1.5, True, None, "", "abc"])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            cfg.normalize_delay(value)


class TestRecoverValidation:
    """人工介入自动恢复：30~3600 秒整数，或 0（不自动恢复）。"""

    @pytest.mark.parametrize("value", [0, 30, 120, 3600, "300"])
    def test_valid(self, value):
        assert cfg.normalize_recover(value) == int(value)

    @pytest.mark.parametrize("value", [-1, 1, 29, 3601, 2.5, True, None, "", "abc"])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            cfg.normalize_recover(value)


class TestManualSessionState:
    """按店铺 + 客户独立的人工介入状态机。"""

    def test_no_manual_message_means_immediate_reply(self):
        state = ManualSessionState()
        assert state.is_active(1, "A", 120, now=_t(10)) is False
        assert state.pending_delay(1, "A", 120, 30, now=_t(10)) == 0

    def test_customer_isolation(self):
        state = ManualSessionState()
        state.mark(1, "A", now=_t(0))
        assert state.is_active(1, "A", 120, now=_t(10)) is True
        assert state.is_active(1, "B", 120, now=_t(10)) is False
        assert state.pending_delay(1, "B", 120, 30, now=_t(10)) == 0

    def test_shop_isolation(self):
        state = ManualSessionState()
        state.mark(1, "A", now=_t(0))
        assert state.is_active(2, "A", 120, now=_t(10)) is False

    def test_auto_recover_after_timeout(self):
        state = ManualSessionState()
        state.mark(1, "A", now=_t(0))
        assert state.is_active(1, "A", 120, now=_t(119)) is True
        assert state.is_active(1, "A", 120, now=_t(120)) is False
        assert state.pending_delay(1, "A", 120, 30, now=_t(121)) == 0

    def test_manual_message_refreshes_timer(self):
        state = ManualSessionState()
        state.mark(1, "A", now=_t(0))
        state.mark(1, "A", now=_t(90))
        # 到 120 秒时距最后一次人工消息仅 30 秒，仍处于人工模式。
        assert state.is_active(1, "A", 120, now=_t(120)) is True
        assert state.is_active(1, "A", 120, now=_t(210)) is False

    def test_zero_means_never_auto_recover(self):
        state = ManualSessionState()
        state.mark(1, "A", now=_t(0))
        assert state.is_active(1, "A", 0, now=_t(99999)) is True
        assert state.pending_delay(1, "A", 0, 30, now=_t(99999)) == 30

    def test_speed_up_recover_value(self):
        state = ManualSessionState()
        state.mark(1, "A", now=_t(0))
        assert state.is_active(1, "A", 30, now=_t(29)) is True
        assert state.is_active(1, "A", 30, now=_t(30)) is False

    def test_clear(self):
        state = ManualSessionState()
        state.mark(1, "A", now=_t(0))
        state.clear(1, "A")
        assert state.is_active(1, "A", 120, now=_t(1)) is False

    def test_none_customer_uid_is_ignored(self):
        state = ManualSessionState()
        state.mark(1, None, now=_t(0))
        assert state.is_active(1, None, 120, now=_t(1)) is False
