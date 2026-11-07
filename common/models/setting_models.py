# -*- coding: utf-8 -*-
"""
common.models.setting_models —— 通知、系统设置与字典数据表模型
==============================================================
本文件用途：定义「拼多多自动回复」系统通知渠道、系统设置与数据字典业务域的
数据表结构模型，覆盖：
- notify_channel  通知渠道（邮件/Webhook/企业微信）
- sys_setting     系统设置（主题/分页/基础设置/SMTP/菜单显隐/品牌/代理等）
- sys_dict        数据字典（枚举值的中文文案来源，需求 24.7）

关键约束（开发规范）：
- 规范 9：每张表均有自增 BIGINT 主键。
- 规范 10：owner_user_id 等关联键均为普通列，无外键。
- 规范 15：枚举值必须入数据字典表，前端从字典查中文展示。
- 规范 17：审计时间字段统一北京时间。
- sys_setting 涵盖 SMTP 密码等敏感配置，敏感值以加密存储且对外脱敏（需求 21.7）。
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import AuditMixin, Base


class NotifyChannel(AuditMixin, Base):
    """通知渠道表 notify_channel。

    配置系统事件通知渠道（邮件 / Webhook / 企业微信）。渠道类型枚举入字典。
    """

    __tablename__ = "pdd_notify_channel"
    __table_args__ = {"comment": "通知渠道表（渠道类型 / 目标地址 / 启停用）"}

    # 归属店铺主键：普通列，无外键（规范 10）。店铺级通知渠道，按店铺隔离配置与推送。
    # 可空以兼容历史数据（旧的全局渠道 shop_pk 为 NULL，启动自检自动补列，需求 24.5）。
    shop_pk: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True, comment="归属店铺主键 shop.id（普通列，无外键）"
    )
    # 渠道类型：email/webhook/wecom/feishu（枚举入字典）
    channel_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="渠道类型：email/webhook/wecom/feishu（枚举入字典）"
    )
    # 目标：邮箱地址 / Webhook URL 等（含敏感配置时由设置侧加密，对外脱敏）
    target: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="通知目标（邮箱/URL 等）"
    )
    # 飞书机器人「自定义关键词」安全设置：发送时自动补到消息文本里（可空兼容历史数据）
    keyword: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="飞书自定义关键词（选填）"
    )
    # 飞书机器人「签名校验」安全设置：开启后按官方算法加签（可空兼容历史数据）
    secret: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="飞书机器人签名密钥（选填）"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用"
    )


class SysSetting(AuditMixin, Base):
    """系统设置表 sys_setting。

    以键值对存储系统 / 用户级设置，``setting_value`` 为 JSON 文本。``scope``
    区分全局或用户维度，``owner_user_id`` 为用户级设置的归属用户（普通列，无外键）。
    涵盖主题、分页默认、基础设置（含 log.retention_days 1~365）、SMTP（密码加密）、
    菜单显隐、登录页品牌、免责声明、二维码、代理等。敏感值对外脱敏（需求 21.7）。
    """

    __tablename__ = "pdd_sys_setting"
    __table_args__ = {"comment": "系统设置表（键值对存储，全局 / 用户维度）"}

    # 设置键：如 theme.color / page.size / log.retention_days / smtp.* / proxy.* 等
    setting_key: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="设置键（如 theme.color / proxy.enabled）"
    )
    # 设置值：JSON 文本（敏感值如 SMTP 密码以加密形式存储，对外脱敏）
    setting_value: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="设置值（JSON 文本；敏感值加密存储）"
    )
    # 作用域：global=全局 / user=用户（枚举入字典）
    scope: Mapped[str] = mapped_column(
        String(16), default="global", nullable=False, comment="作用域：global/user"
    )
    # 用户级设置的归属用户 id：普通列，无外键（数据按用户维度隔离，需求 22.7）
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="用户级设置归属用户 ID（普通列，无外键）"
    )


class SysDict(AuditMixin, Base):
    """数据字典表 sys_dict。

    登记系统全部枚举值及其中文文案，前端展示从本表查出中文（规范 15、需求 24.7）。
    涵盖连接状态、账号登录态、关键词匹配方式、回复类型、风控类型、售后状态/类型、
    消息处理结果、通知渠道类型、消息方向、反馈处理状态、定时任务调度方式/执行结果、
    菜单键等。同 (dict_type, dict_key) 组合逻辑唯一（代码层校验）。
    """

    __tablename__ = "pdd_sys_dict"
    __table_args__ = (
        # 逻辑唯一：同一字典类型下字典键唯一（upsert 幂等的库层保障，需求 15.4）。
        # 启动自检迁移器会在无重复数据时自动补建该唯一索引（有重复则跳过告警，不删数据）。
        UniqueConstraint("dict_type", "dict_key", name="uix_pdd_sys_dict_type_key"),
        {"comment": "数据字典表（枚举类型 / 键 / 中文标签 / 排序）"},
    )

    # 字典类型：标识一类枚举（如 login_state / match_type 等）
    dict_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="字典类型（枚举分组键）"
    )
    # 字典键：该类枚举下的具体取值键
    dict_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="字典键（枚举取值键）"
    )
    # 字典标签：中文展示文案
    dict_label: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="字典标签（中文展示文案）"
    )
    order_no: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="排序序号（升序）"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用"
    )


__all__ = [
    "NotifyChannel",
    "SysSetting",
    "SysDict",
]
