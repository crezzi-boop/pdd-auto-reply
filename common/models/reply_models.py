# -*- coding: utf-8 -*-
"""
common.models.reply_models —— 回复规则数据表模型
================================================
本文件用途：定义「拼多多自动回复」系统回复规则业务域的数据表结构模型，覆盖：
- keyword_rule   关键词规则（匹配方式 / 回复类型 / 优先级 / 启停用）
- default_reply  默认回复（多条兜底语句 / 轮换窗口 / 启停用）
- default_reply_rotation_record  默认回复轮换发送记录（窗口内不重复）
- goods_reply    商品专属回复（绑定 goods_id，优先级高于默认）

关键约束（开发规范）：
- 规范 9：每张表均有自增 BIGINT 主键。
- 规范 10：shop_pk 等关联键均为普通列，无外键。
- 规范 17：审计时间字段统一北京时间。
- 匹配方式（全匹配/包含/正则）、回复类型（文本/图片）等枚举登记入 sys_dict
  （需求 24.7），本表以字符串存键，前端从字典查中文展示。
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import AuditMixin, Base


class KeywordRule(AuditMixin, Base):
    """关键词规则表 keyword_rule。

    按优先级匹配客户消息，命中多条时仅取优先级最高的一条；停用规则不参与匹配
    （需求 6.x）。匹配方式与回复类型为枚举键，入字典前端展示中文。
    """

    __tablename__ = "pdd_keyword_rule"
    __table_args__ = {"comment": "关键词规则表（匹配方式 / 回复类型 / 优先级 / 启停用）"}

    # 关联店铺主键 shop.id（即 shop_pk）：普通列，无外键
    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（普通列，无外键）"
    )
    keyword: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="关键词（匹配文本或正则）"
    )
    # 匹配方式：full=全匹配 / contains=包含 / regex=正则（枚举入字典）
    match_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="匹配方式：full/contains/regex（枚举入字典）"
    )
    # 回复类型：text=文本 / image=图片（枚举入字典）
    reply_type: Mapped[str] = mapped_column(
        String(32), default="text", nullable=False, comment="回复类型：text/image（枚举入字典）"
    )
    reply_content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="回复内容（文本或图片地址）"
    )
    # 优先级：数值越大优先级越高（命中多条取最高）
    priority: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="优先级（越大越优先）"
    )
    # 是否启用：停用不参与匹配，下一条消息生效（需求 6.7）
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用（停用不参与匹配）"
    )


class DefaultReply(AuditMixin, Base):
    """默认回复表 default_reply。

    店铺兜底回复内容，当关键词 / 商品专属 / AI 均未命中时使用（需求 7.1）。
    支持「只回复一次」：开启后同一客户在本店铺只会收到一次默认回复（参照参考
    项目 xianyu-auto-reply-wangpan 的 reply_once 设置）。

    多语句轮换（防拼多多重复拦截）：``contents`` 以 JSON 数组文本保存用户手工
    填写的多条兜底语句，``content`` 恒为其中第一条（兼容历史单条配置及其它
    读取方）；``rotate_minutes`` 为「同一店铺窗口内每次默认回复不重复」的窗口
    分钟数，默认 10 分钟。两个字段均为可空新列，由启动自检迁移器自动补齐，
    历史数据无需改动即可按单条回复继续工作。
    """

    __tablename__ = "pdd_default_reply"
    __table_args__ = {"comment": "默认回复表（多条兜底语句 / 轮换窗口 / 启停用 / 只回复一次）"}

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（普通列，无外键）"
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="默认回复内容（多条配置中的第一条，兼容历史数据）"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用"
    )
    # 只回复一次：True 时同一客户在本店铺仅首次触发默认回复，后续不再发送
    reply_once: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否只回复一次（同一客户仅发送一次默认回复）"
    )
    # 多条兜底语句（JSON 数组文本）：为空 / NULL 时回退到 content 单条。
    contents: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="多条默认回复语句（JSON 数组文本；为空时按 content 单条处理）"
    )
    # 不重复轮换窗口（分钟）：同一店铺在该窗口内每次默认回复使用不同语句。
    rotate_minutes: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="默认回复不重复轮换窗口（分钟，默认 10）"
    )


class DefaultReplyRecord(AuditMixin, Base):
    """默认回复发送记录表 default_reply_record。

    记录「已向某客户发送过默认回复」的事实，用于支撑默认回复的「只回复一次」
    （需求 7.1 配套）：以 (shop_pk, customer_uid) 唯一标识一个客户在本店铺是否
    已收到过默认回复。普通列、无外键（规范 10），关系由代码维护。
    """

    __tablename__ = "pdd_default_reply_record"
    __table_args__ = {"comment": "默认回复发送记录表（支撑只回复一次：已回复客户）"}

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（普通列，无外键）"
    )
    customer_uid: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="已收到默认回复的客户唯一标识"
    )


class DefaultReplyRotationRecord(AuditMixin, Base):
    """默认回复轮换发送记录表 default_reply_rotation_record。

    记录「某店铺在某个时刻发送了哪条默认回复语句」，用于支撑「同一店铺 10 分钟
    内每次默认回复换用不同语句」的防重复拦截策略。以语句指纹 ``variant_key``
    标识语句（由 common.services.default_reply_variants.variant_key 生成），
    ``created_at`` 为北京时间发送时刻；超过窗口的记录由轮换器自动清理。

    普通列、无外键（规范 10），关系由代码维护。
    """

    __tablename__ = "pdd_default_reply_rotation_record"
    __table_args__ = {"comment": "默认回复轮换发送记录（时间窗口内避免重复语句）"}

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="关联店铺主键 shop.id（普通列，无外键）"
    )
    variant_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="默认回复语句指纹（sha1 前 16 位）"
    )


class GoodsReply(AuditMixin, Base):
    """商品专属回复表 goods_reply。

    针对特定商品（goods_id）配置专属回复，优先级高于默认回复（需求 7.2/7.4）。
    """

    __tablename__ = "pdd_goods_reply"
    __table_args__ = {"comment": "商品专属回复表（绑定 goods_id，优先级高于默认）"}

    shop_pk: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="关联店铺主键 shop.id（普通列，无外键）"
    )
    # 拼多多商品业务标识：普通列，无外键
    goods_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="拼多多商品业务标识 goods_id（无外键）"
    )
    reply_type: Mapped[str] = mapped_column(
        String(32), default="text", nullable=False, comment="回复类型：text/image（枚举入字典）"
    )
    reply_content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="回复内容"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用"
    )


__all__ = [
    "KeywordRule",
    "DefaultReply",
    "DefaultReplyRecord",
    "DefaultReplyRotationRecord",
    "GoodsReply",
]
