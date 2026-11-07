# -*- coding: utf-8 -*-
"""
channel_pdd.pdd_message —— 拼多多消息解析与上下文提取
====================================================
本文件用途：复用改造 Customer-Agent 的 ``PDDChatMessage`` / ``MessageTypeHandler``，
将拼多多 WebSocket 推送的原始报文解析为本系统统一的 ``Context`` 数据结构，并提取
会话订单上下文与商品上下文，供自动回复引擎、AI 回复引擎与消息日志使用。

职责（需求 17.1 / 17.2）：
- 解析文本 / 图片 / 视频 / 表情 / 撤回 / 商品咨询 / 商品规格 / 订单 / 转接等消息类型；
- 订单消息：提取订单号、商品名、``goods_id``、规格、售后状态、售后类型；
- 商品咨询消息：提取 ``goods_id``、商品名、价格、缩略图；
- 商品规格消息：提取 ``goods_id``、商品名、价格、规格。

设计说明：
- ``ContextType`` 为字符串枚举，对应消息语义类型；
- ``Context`` 采用标准库 ``dataclass`` 实现（不引入 pydantic 依赖），承载解析结果；
- ``MessageTypeHandler`` 为各消息类型的纯函数解析器，便于属性测试覆盖；
- ``PDDChatMessage`` 为解析入口，按 ``response`` / ``type`` / ``sub_type`` 分发。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Dict, Optional, Tuple


class ContextType(str, Enum):
    """上下文类型枚举（消息语义类型）。"""

    TEXT = "text"  # 文本
    IMAGE = "image"  # 图片
    VIDEO = "video"  # 视频
    EMOTION = "emotion"  # 表情
    GOODS_CARD = "goods_card"  # 商品卡片
    GOODS_INQUIRY = "goods_inquiry"  # 商品咨询
    GOODS_SPEC = "goods_spec"  # 商品规格
    ORDER_INFO = "order_info"  # 订单信息
    SYSTEM_STATUS = "system_status"  # 系统状态
    MALL_SYSTEM_MSG = "mall_system_msg"  # 商城系统消息
    MALL_CS = "mall_cs"  # 商城客服（非用户消息）
    WITHDRAW = "withdraw"  # 撤回
    AUTH = "auth"  # 认证
    TRANSFER = "transfer"  # 转接

    def __str__(self) -> str:  # pragma: no cover - 仅便于日志展示
        return self.value


class PDDMsgType(IntEnum):
    """拼多多消息类型枚举（report 报文中的 message.type）。"""

    TEXT = 0
    IMAGE = 1
    VIDEO = 14
    WITHDRAW = 1002
    EMOTION = 5
    GOODS_SPEC = 64
    TRANSFER = 24


class PDDSubType(IntEnum):
    """拼多多文本消息子类型枚举（message.sub_type）。"""

    ORDER_INFO = 1
    GOODS_INQUIRY = 0


@dataclass
class Context:
    """统一消息上下文数据结构。

    字段说明：
    - ``type``：消息语义类型（``ContextType``）；
    - ``content``：消息主体内容（文本内容 / 结构化字典 / 提示文本等）；
    - ``order_context``：订单上下文（仅订单消息提取，否则为 None）；
    - ``goods_context``：商品上下文（商品咨询 / 商品规格消息提取，否则为 None）；
    - ``kwargs``：渠道专用元信息（消息 ID、收发方、昵称、时间戳、店铺等）。
    """

    type: ContextType
    content: Any = None
    order_context: Optional[Dict[str, Any]] = None
    goods_context: Optional[Dict[str, Any]] = None
    kwargs: Dict[str, Any] = field(default_factory=dict)


def _safe_get(data: Any, *keys: str, default: Any = None) -> Any:
    """安全获取嵌套字典值。

    逐层下钻 ``keys``，任一层非字典或取值为 None 时返回 ``default``，
    避免链式 ``get()`` 时中间值为 None 触发 AttributeError。
    """
    result = data
    for key in keys:
        if not isinstance(result, dict):
            return default
        result = result.get(key)
        if result is None:
            return default
    return result


def _merge_info(msg_data: Any) -> Dict[str, Any]:
    """合并商品 / 订单消息的 ``message.info`` 顶层与其下 ``data`` 子层。

    不同消息子类型的商品字段位置不一致：商品咨询多挂在 ``message.info`` 顶层，
    商品规格 / 部分卡片挂在 ``message.info.data`` 下。合并后统一从一个扁平字典取
    值，避免因层级差异取空（``data`` 同名键覆盖顶层，优先用更具体的 data）。

    Args:
        msg_data: 原始报文字典。

    Returns:
        合并后的扁平字典（info 顶层 + info.data）。
    """
    info = _safe_get(msg_data, "message", "info", default={})
    info = info if isinstance(info, dict) else {}
    data = info.get("data") if isinstance(info.get("data"), dict) else {}
    return {**info, **data}


def _pick(source: Dict[str, Any], *keys: str) -> Any:
    """从字典按候选键顺序取第一个非空值（兼容下划线 / 驼峰命名）。

    Args:
        source: 源字典。
        *keys: 候选键（按优先级排列）。

    Returns:
        第一个非空值；全部缺失返回 None。
    """
    for key in keys:
        value = source.get(key)
        if value is not None:
            return value
    return None


class MessageTypeHandler:
    """消息类型解析器：每个静态方法返回 ``(ContextType, content)`` 二元组。"""

    @staticmethod
    def _get_content(
        msg_data: Dict[str, Any], context_type: ContextType, path: Tuple[str, ...]
    ) -> Tuple[ContextType, Any]:
        """通用内容提取：按路径取出内容并附上指定上下文类型。"""
        return context_type, _safe_get(msg_data, *path)

    @staticmethod
    def handle_text(msg_data: Dict[str, Any]) -> Tuple[ContextType, Any]:
        """处理文本消息。"""
        return MessageTypeHandler._get_content(
            msg_data, ContextType.TEXT, ("message", "content")
        )

    @staticmethod
    def handle_image(msg_data: Dict[str, Any]) -> Tuple[ContextType, Any]:
        """处理图片消息。"""
        return MessageTypeHandler._get_content(
            msg_data, ContextType.IMAGE, ("message", "content")
        )

    @staticmethod
    def handle_video(msg_data: Dict[str, Any]) -> Tuple[ContextType, Any]:
        """处理视频消息。"""
        return MessageTypeHandler._get_content(
            msg_data, ContextType.VIDEO, ("message", "content")
        )

    @staticmethod
    def handle_emotion(msg_data: Dict[str, Any]) -> Tuple[ContextType, Any]:
        """处理表情消息。

        注意：表情消息的 ``description`` 字段挂在顶层 ``info`` 下（非
        ``message.info``），与文本 / 订单 / 商品咨询消息的层级不同（与参照项目
        Customer-Agent 实测报文一致）。
        """
        return MessageTypeHandler._get_content(
            msg_data, ContextType.EMOTION, ("info", "description")
        )

    @staticmethod
    def handle_withdraw(msg_data: Dict[str, Any]) -> Tuple[ContextType, Any]:
        """处理撤回消息。

        注意：撤回消息的 ``withdraw_hint`` 字段挂在顶层 ``info`` 下（非
        ``message.info``），与参照项目 Customer-Agent 实测报文一致。
        """
        return MessageTypeHandler._get_content(
            msg_data, ContextType.WITHDRAW, ("info", "withdraw_hint")
        )

    @staticmethod
    def handle_goods_inquiry(msg_data: Dict[str, Any]) -> Tuple[ContextType, Dict[str, Any]]:
        """处理商品咨询消息：提取 goods_id / 商品名 / 价格 / 缩略图（需求 17.2）。

        兼容拼多多实测的下划线字段（goods_id/goods_name/...）与历史驼峰字段
        （goodsID/goodsName/...），并兼容字段挂在 ``info`` 顶层或 ``info.data`` 下。
        """
        info = _merge_info(msg_data)
        goods_info = {
            "goods_id": _pick(info, "goods_id", "goodsID"),
            "goods_name": _pick(info, "goods_name", "goodsName"),
            "goods_price": _pick(info, "goods_price", "goodsPrice"),
            "goods_thumb_url": _pick(info, "thumb_url", "goodsThumbUrl"),
            "link_url": _pick(info, "link_url", "linkUrl"),
        }
        return ContextType.GOODS_INQUIRY, goods_info

    @staticmethod
    def handle_goods_spec(msg_data: Dict[str, Any]) -> Tuple[ContextType, Dict[str, Any]]:
        """处理商品规格消息：提取 goods_id / 商品名 / 价格 / 规格（需求 17.2）。

        兼容下划线 / 驼峰字段，并兼容字段挂在 ``info`` 顶层或 ``info.data`` 下
        （实测商品规格卡片字段位于 ``message.info.data`` 内）。
        """
        info = _merge_info(msg_data)
        goods_info = {
            "goods_id": _pick(info, "goods_id", "goodsID"),
            "goods_name": _pick(info, "goods_name", "goodsName"),
            "goods_price": _pick(info, "goods_price", "goodsPrice"),
            "goods_spec": _pick(info, "goods_spec", "spec"),
        }
        return ContextType.GOODS_SPEC, goods_info

    @staticmethod
    def handle_order_info(msg_data: Dict[str, Any]) -> Tuple[ContextType, Dict[str, Any]]:
        """处理订单信息消息：提取订单号 / 商品 / 规格 / 售后状态与类型（需求 17.1）。

        兼容下划线 / 驼峰字段，并兼容字段挂在 ``info`` 顶层或 ``info.data`` 下。
        """
        info = _merge_info(msg_data)
        order_info = {
            "order_id": _pick(info, "order_sequence_no", "orderSequenceNo"),
            "goods_id": _pick(info, "goods_id", "goodsID"),
            "goods_name": _pick(info, "goods_name", "goodsName"),
            "afterSalesStatus": _pick(info, "after_sales_status", "afterSalesStatus"),
            "afterSalesType": _pick(info, "after_sales_type", "afterSalesType"),
            "spec": _pick(info, "goods_spec", "spec"),
        }
        return ContextType.ORDER_INFO, order_info

    @staticmethod
    def handle_mall_system_msg(msg_data: Dict[str, Any]) -> Tuple[ContextType, Dict[str, Any]]:
        """处理商城系统消息。"""
        system_msg = {
            "user_id": _safe_get(msg_data, "message", "data", "user_id"),
        }
        return ContextType.MALL_SYSTEM_MSG, system_msg

    @staticmethod
    def handle_auth(msg_data: Dict[str, Any]) -> Tuple[ContextType, Dict[str, Any]]:
        """处理认证消息。"""
        auth_info = {
            "uid": _safe_get(msg_data, "uid"),
            "result": _safe_get(msg_data, "auth", "result"),
            "status": _safe_get(msg_data, "status"),
        }
        return ContextType.AUTH, auth_info

    @staticmethod
    def handle_transfer(msg_data: Dict[str, Any]) -> Tuple[ContextType, Dict[str, Any]]:
        """处理转接消息。"""
        transfer_info = {
            "from_uid": _safe_get(msg_data, "message", "from", "uid"),
            "to_uid": _safe_get(msg_data, "message", "to", "uid"),
        }
        return ContextType.TRANSFER, transfer_info


class BaseMessageHandler:
    """基础信息处理器：从原始报文抽取收发方、昵称、时间戳等元信息。"""

    def __init__(self, msg: Dict[str, Any]) -> None:
        self.msg = msg if isinstance(msg, dict) else {}
        self.data = self.msg.get("message", {}) or {}

    def get_basic_info(self) -> Dict[str, Any]:
        """获取消息基础信息（用于填充 Context.kwargs）。"""
        return {
            "msg_id": self.data.get("msg_id"),
            "nickname": self.data.get("nickname"),
            "from_role": _safe_get(self.data, "from", "role"),
            "from_uid": _safe_get(self.data, "from", "uid"),
            "from_cs_uid": _safe_get(self.data, "from", "cs_uid"),
            "to_role": _safe_get(self.data, "to", "role"),
            "to_uid": _safe_get(self.data, "to", "uid"),
            "timestamp": self.data.get("time"),
        }


class PDDChatMessage:
    """拼多多消息解析入口。

    构造时解析原始报文，得到：
    - ``user_msg_type``：解析出的 ``ContextType``；
    - ``content``：消息内容（文本 / 结构化字典 / 提示文本）；
    - ``msg_id`` / ``nickname`` / ``from_user`` / ``from_uid`` / ``to_user`` /
      ``to_uid`` / ``timestamp``：消息元信息。

    并提供 ``to_context()`` 将解析结果转换为统一的 ``Context`` 数据结构，
    其中订单消息填充 ``order_context``，商品咨询 / 规格消息填充 ``goods_context``。
    """

    def __init__(self, msg: Dict[str, Any]) -> None:
        self.msg: Dict[str, Any] = msg if isinstance(msg, dict) else {}
        self.base_handler = BaseMessageHandler(self.msg)

        basic_info = self.base_handler.get_basic_info()
        self.msg_id = basic_info.get("msg_id")
        self.nickname = basic_info.get("nickname")
        self.from_user = basic_info.get("from_role")
        self.from_uid = basic_info.get("from_uid")
        self.from_cs_uid = basic_info.get("from_cs_uid")
        self.to_user = basic_info.get("to_role")
        self.to_uid = basic_info.get("to_uid")
        self.timestamp = basic_info.get("timestamp")

        self.user_msg_type: ContextType = ContextType.SYSTEM_STATUS
        self.content: Any = None

        # 商城客服（非用户）消息：直接取内容，不做后续业务解析。
        if self.from_user == "mall_cs":
            self.user_msg_type = ContextType.MALL_CS
            self.content = _safe_get(self.msg, "message", "content")
            return

        self._process_message()

    def _process_message(self) -> None:
        """按 response / type / sub_type 分发到对应解析器。"""
        response = self.msg.get("response")
        if response == "push":
            self._process_push_message()
        elif response == "auth":
            self.user_msg_type, self.content = MessageTypeHandler.handle_auth(self.msg)
        elif response == "mall_system_msg":
            self.user_msg_type, self.content = MessageTypeHandler.handle_mall_system_msg(
                self.msg
            )
        else:
            self.user_msg_type = ContextType.SYSTEM_STATUS
            self.content = f"不支持的消息类型: {response}"

    def _process_push_message(self) -> None:
        """处理 push 推送消息：按 message.type 与 sub_type 分发。"""
        user_msg_type = _safe_get(self.msg, "message", "type")
        if user_msg_type == PDDMsgType.TEXT:
            sub_type = _safe_get(self.msg, "message", "sub_type")
            if sub_type == PDDSubType.ORDER_INFO:
                self.user_msg_type, self.content = MessageTypeHandler.handle_order_info(
                    self.msg
                )
            elif sub_type == PDDSubType.GOODS_INQUIRY:
                self.user_msg_type, self.content = MessageTypeHandler.handle_goods_inquiry(
                    self.msg
                )
            else:
                self.user_msg_type, self.content = MessageTypeHandler.handle_text(self.msg)
        elif user_msg_type == PDDMsgType.IMAGE:
            self.user_msg_type, self.content = MessageTypeHandler.handle_image(self.msg)
        elif user_msg_type == PDDMsgType.VIDEO:
            self.user_msg_type, self.content = MessageTypeHandler.handle_video(self.msg)
        elif user_msg_type == PDDMsgType.WITHDRAW:
            self.user_msg_type, self.content = MessageTypeHandler.handle_withdraw(self.msg)
        elif user_msg_type == PDDMsgType.EMOTION:
            self.user_msg_type, self.content = MessageTypeHandler.handle_emotion(self.msg)
        elif user_msg_type == PDDMsgType.GOODS_SPEC:
            self.user_msg_type, self.content = MessageTypeHandler.handle_goods_spec(self.msg)
        elif user_msg_type == PDDMsgType.TRANSFER:
            self.user_msg_type, self.content = MessageTypeHandler.handle_transfer(self.msg)
        else:
            self.user_msg_type = ContextType.SYSTEM_STATUS
            self.content = f"不支持的消息类型: {user_msg_type}"

    def to_context(self, shop_id: Optional[str] = None, shop_name: Optional[str] = None) -> Context:
        """将解析结果转换为统一的 Context 数据结构。

        订单消息将结构化内容同时写入 ``order_context``；商品咨询 / 商品规格消息
        将结构化内容写入 ``goods_context``，供自动回复引擎与 AI 回复引擎使用。
        """
        order_context: Optional[Dict[str, Any]] = None
        goods_context: Optional[Dict[str, Any]] = None
        if self.user_msg_type == ContextType.ORDER_INFO and isinstance(self.content, dict):
            order_context = dict(self.content)
        elif (
            self.user_msg_type in (ContextType.GOODS_INQUIRY, ContextType.GOODS_SPEC)
            and isinstance(self.content, dict)
        ):
            goods_context = dict(self.content)

        kwargs: Dict[str, Any] = {
            "msg_id": self.msg_id,
            "nickname": self.nickname,
            "from_user": self.from_user,
            "from_uid": self.from_uid,
            "from_cs_uid": self.from_cs_uid,
            "to_user": self.to_user,
            "to_uid": self.to_uid,
            "timestamp": self.timestamp,
            "shop_id": shop_id,
            "shop_name": shop_name,
        }

        return Context(
            type=self.user_msg_type,
            content=self.content,
            order_context=order_context,
            goods_context=goods_context,
            kwargs=kwargs,
        )
