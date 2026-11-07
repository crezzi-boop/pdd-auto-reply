# -*- coding: utf-8 -*-
"""
backend.app.services.notify_service —— 通知渠道与消息通知业务服务
================================================================
本文件用途：实现 backend 服务的「通知渠道与消息通知」业务逻辑，供 notify 路由
复用，满足需求 18（通知渠道与消息通知）：

- ``create_notify_channel(...)`` / ``update_notify_channel(...)``：配置通知渠道
  （渠道类型 + 目标地址 + 启停用），持久化并返回统一响应体（需求 18.1）。渠道
  类型须为合法枚举（email/webhook/wecom），枚举入数据字典 ``channel_type``。
- ``list_notify_channels(...)``：通知渠道列表后端分页查询（需求 18.5 配套）。
- ``test_notify_channel(...)``：对某通知渠道发起测试发送并返回发送结果，写入
  通知记录（需求 18.2）；发送失败记日志不中断（需求 18.4）。
- ``push_system_event(...)``：发生需通知的系统事件（连接断开 / 登录态失效 /
  风控触发）时，经「该店铺已启用」渠道推送通知（需求 18.3，店铺级方案 A）；逐渠道
  发送，单个渠道失败仅记通知失败日志与系统日志，不中断对其它渠道的推送与主流程
  （需求 18.4）。
- ``list_notify_records(...)``：通知记录后端分页查询（需求 18.5），按北京时间
  倒序返回。
- ``list_channel_types(...)``：查询「通知渠道类型」枚举字典（需求 18.x / 24.7）。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3 / 需求 24.1）。
- 所有数据访问经 common.db.repository 参数化查询，禁止拼接 SQL（规范 16）。
- 通知渠道类型枚举入数据字典 ``channel_type``，前端展示中文（规范 15 / 需求 18.x）。
- 通知 / 系统日志禁止物理删除；记录保留（规范 11 / 需求 19.5）。
- 失败记日志不中断主流程：渠道发送以 try/except 兜底，异常转为「失败」记录并
  写系统日志，绝不向上抛出打断推送主流程（需求 18.4 / 26）。
- 时间字段统一北京时间（规范 17 / 需求 24.8）；导入置顶（规范 51）；
  中文注释（规范 37）；单文件 ≤500 行（规范 35）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.core.business_codes import (
    CODE_EXTERNAL_ERROR,
    CODE_FORBIDDEN,
    CODE_NOT_FOUND,
    CODE_PARAM_ERROR,
    MSG_FORBIDDEN,
)
from app.core.data_scope import (
    build_data_scope,
    is_in_scope,
    paginate_shop_scoped,
)
from common.db.repository import Repository
from common.models.log_models import NotifyRecord, SystemLog
from common.models.setting_models import NotifyChannel
from common.models.shop_models import Shop
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response, success_response
from common.services.dict_service import DictService
from common.utils.time_utils import now_beijing_naive, safe_isoformat

logger = logging.getLogger(__name__)

# 通知渠道类型字典分组键（见 dict_seed_data 的 channel_type，需求 18.x）。
DICT_CHANNEL_TYPE: str = "channel_type"

# 合法的通知渠道类型枚举键（与 sys_dict 的 channel_type 字典一致）。
# feishu 为「飞书机器人」专用类型：按其官方 webhook 报文格式投递，并支持自定义
# 关键词 / 签名校验（安全设置）。历史以 webhook 类型填写飞书地址的记录，由目标
# 域名自动识别为飞书渠道，无需用户重新配置。
ALLOWED_CHANNEL_TYPES: tuple[str, ...] = ("email", "webhook", "wecom", "feishu")

# 飞书 / 企业微信机器人官方 webhook 域名（用于兼容历史 webhook 类型的记录）。
_FEISHU_HOSTS: tuple[str, ...] = ("open.feishu.cn", "open.larksuite.com")
_WECOM_HOSTS: tuple[str, ...] = ("qyapi.weixin.qq.com",)

# 飞书机器人未填写关键词时的兜底词：飞书「自定义关键词」安全设置要求消息文本
# 命中关键词，否则返回 code=19024 且 HTTP 仍为 200（收不到消息却看似成功）。
# 未配置时统一以「提醒」作为消息前缀，保证开箱可用；用户可在渠道里自行修改。
FEISHU_FALLBACK_KEYWORD: str = "提醒"

# 飞书常见业务错误码 -> 中文排查提示（HTTP 200 也会带业务失败，必须解析响应体）。
_FEISHU_ERROR_HINTS: Dict[int, str] = {
    19024: "消息未包含飞书机器人设置的自定义关键词，请在渠道配置里填写「飞书关键词」",
    19021: "飞书机器人签名校验失败，请检查「签名密钥」或在飞书后台关闭签名校验",
    19001: "飞书机器人请求参数不合法，请检查 Webhook 地址与消息格式",
    9499: "飞书机器人请求格式错误，请确认渠道类型为「飞书机器人」",
    11246: "飞书机器人频率超限，请稍后重试",
}

# 系统事件类型与其中文文案（需求 18.3：连接断开 / 登录态失效 / 风控触发）。
EVENT_CONNECTION_DISCONNECTED: str = "connection_disconnected"
EVENT_LOGIN_EXPIRED: str = "login_expired"
EVENT_RISK_TRIGGERED: str = "risk_triggered"
EVENT_TYPE_LABELS: Dict[str, str] = {
    EVENT_CONNECTION_DISCONNECTED: "连接断开",
    EVENT_LOGIN_EXPIRED: "登录态失效",
    EVENT_RISK_TRIGGERED: "风控触发",
}

# 通知发送结果取值（写入 notify_record.send_result）。
SEND_RESULT_SUCCESS: str = "success"
SEND_RESULT_FAILED: str = "failed"

# 系统日志来源模块标识（需求 18.4 记系统日志）。
_LOG_MODULE: str = "notify"

# 渠道推送 HTTP 超时（秒）：尽力发送，避免长时间阻塞推送主流程。
_SEND_TIMEOUT_SECONDS: float = 5.0


# ----------------------------------------------------------------------
# 序列化
# ----------------------------------------------------------------------
def serialize_channel(channel: NotifyChannel) -> Dict[str, Any]:
    """将通知渠道模型序列化为对外字典（时间为北京时间 ISO 串）。

    Args:
        channel: 通知渠道模型实例。

    Returns:
        通知渠道信息字典。
    """
    return {
        "id": channel.id,
        "shop_pk": channel.shop_pk,
        "channel_type": channel.channel_type,
        "target": channel.target,
        "keyword": getattr(channel, "keyword", None) or "",
        "secret": getattr(channel, "secret", None) or "",
        "enabled": bool(channel.enabled),
        "created_at": safe_isoformat(channel.created_at),
        "updated_at": safe_isoformat(channel.updated_at),
    }


def serialize_record(record: NotifyRecord) -> Dict[str, Any]:
    """将通知发送记录序列化为对外字典（时间为北京时间 ISO 串）。

    Args:
        record: 通知发送记录模型实例。

    Returns:
        通知记录信息字典。
    """
    return {
        "id": record.id,
        "shop_pk": record.shop_pk,
        "channel_id": record.channel_id,
        "event_type": record.event_type,
        "event_label": EVENT_TYPE_LABELS.get(record.event_type or "", record.event_type),
        "content": record.content,
        "send_result": record.send_result,
        "log_time": safe_isoformat(record.log_time),
    }


# ----------------------------------------------------------------------
# 内部辅助
# ----------------------------------------------------------------------
def _write_system_log(session: Session, level: str, content: str) -> None:
    """写入一条系统日志（北京时间，禁止 debug 级别，规范 38）。

    Args:
        session: 数据库会话。
        level: 日志级别（info/warning/error）。
        content: 日志内容（中文）。
    """
    Repository(SystemLog, session).create(
        level=level,
        module=_LOG_MODULE,
        content=content,
        log_time=now_beijing_naive(),
    )


def _resolve_channel_kind(channel_type: str, target: str) -> str:
    """识别真实投递协议（飞书 / 企业微信 / 通用 Webhook / 邮件）。

    历史数据常以「webhook」类型填写飞书机器人地址，故除渠道类型外，还按目标
    URL 的域名自动识别，保证老配置不改动也能按飞书官方报文格式投递。

    Args:
        channel_type: 渠道配置里的类型键。
        target: 目标地址。

    Returns:
        协议标识：feishu / wecom / webhook / email。
    """
    ctype = (channel_type or "").strip().lower()
    host = (urlparse(str(target or "")).hostname or "").lower()
    if ctype == "email":
        return "email"
    if ctype == "feishu" or any(host.endswith(h) for h in _FEISHU_HOSTS):
        return "feishu"
    if ctype == "wecom" or any(host.endswith(h) for h in _WECOM_HOSTS):
        return "wecom"
    return "webhook"


def _compose_text(content: str, keyword: Optional[str]) -> str:
    """飞书自定义关键词：文本未命中关键词时在开头补关键词前缀（幂等）。

    Args:
        content: 原通知内容。
        keyword: 飞书机器人后台设置的自定义关键词。

    Returns:
        保证命中关键词的文本。
    """
    text = str(content or "").strip()
    kw = str(keyword or "").strip()
    if kw and kw not in text:
        return f"【{kw}】{text}"
    return text


def effective_text(
    content: str,
    channel_type: str,
    target: Optional[str],
    keyword: Optional[str] = None,
) -> str:
    """计算真正投递出去的文本，供发送与通知记录保持一致。

    Args:
        content: 原通知内容。
        channel_type: 渠道类型。
        target: 目标地址。
        keyword: 渠道配置的飞书自定义关键词；为空时用兜底关键词「提醒」。

    Returns:
        实际投递文本。
    """
    if _resolve_channel_kind(channel_type, str(target or "")) == "feishu":
        kw = str(keyword or "").strip() or FEISHU_FALLBACK_KEYWORD
        return _compose_text(content, kw)
    return str(content or "").strip()


def _feishu_sign(secret: str, timestamp: int) -> str:
    """生成飞书机器人签名校验值（HMAC-SHA256 + base64）。

    Args:
        secret: 飞书机器人后台的签名密钥。
        timestamp: 当前 Unix 秒级时间戳。

    Returns:
        签名字符串。
    """
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _build_payload(kind: str, text: str, secret: Optional[str]) -> Dict[str, Any]:
    """按渠道协议构造官方要求的 JSON 报文。

    - 飞书：``{"msg_type":"text","content":{"text":"..."}}``，开启签名时附带
      ``timestamp`` / ``sign``；用字符串 ``content`` 会被飞书判为无关键词。
    - 企业微信：``{"msgtype":"text","text":{"content":"..."}}``。
    - 通用 Webhook：保持历史兼容的 ``{"content":"..."}``。

    Args:
        kind: 协议标识（feishu/wecom/webhook）。
        text: 实际投递文本。
        secret: 飞书签名密钥；为空时不加签。

    Returns:
        待序列化的报文字典。
    """
    if kind == "feishu":
        payload: Dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        sec = str(secret or "").strip()
        if sec:
            ts = int(time.time())
            payload["timestamp"] = str(ts)
            payload["sign"] = _feishu_sign(sec, ts)
        return payload
    if kind == "wecom":
        return {"msgtype": "text", "text": {"content": text}}
    return {"content": text}


def _parse_channel_response(raw: bytes, kind: str) -> tuple[bool, str]:
    """解析渠道响应体的业务码：HTTP 200 不等于投递成功。

    飞书 / 企业微信在关键词不匹配、签名错误等场景仍返回 HTTP 200，仅响应体里的
    ``code`` / ``StatusCode`` / ``errcode`` 非 0。旧实现只看 HTTP 状态码，导致
    「页面提示发送成功、群里却收不到」。这里统一翻译为中文排查提示。

    Args:
        raw: 响应体字节。
        kind: 协议标识。

    Returns:
        二元组 (是否成功, 结果描述中文)。
    """
    if not raw:
        return True, "发送成功"
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, TypeError, AttributeError):
        # 非 JSON 响应（自定义 Webhook）：HTTP 2xx 即视为成功，保持兼容。
        return True, "发送成功"
    if not isinstance(data, dict):
        return True, "发送成功"
    code = data.get("code", data.get("StatusCode", data.get("errcode", 0)))
    msg = str(data.get("msg") or data.get("StatusMessage") or data.get("errmsg") or "")
    try:
        code_num = int(code)
    except (TypeError, ValueError):
        code_num = 0 if code in (None, "", "0") else -1
    if code_num == 0:
        return True, "发送成功"
    if kind == "feishu":
        hint = _FEISHU_ERROR_HINTS.get(code_num)
        return False, (
            f"飞书机器人发送失败（code={code_num}）：{hint or msg or '未知错误'}"
        )
    return False, f"渠道返回业务失败（code={code_num}）：{msg or '未知错误'}"


def send_via_channel(
    channel_type: str,
    target: Optional[str],
    content: str,
    *,
    keyword: Optional[str] = None,
    secret: Optional[str] = None,
) -> tuple[bool, str]:
    """经指定渠道实际发送通知，返回 (是否成功, 结果描述)。

    本函数为「实际投递」的统一入口，便于单元测试以打桩替换外部 IO：
    - feishu：飞书官方机器人报文 + 自定义关键词前缀 + 可选签名校验；
    - wecom：企业微信官方机器人报文；
    - webhook：通用 JSON 文本 POST（兼容自定义 Webhook）；
    - email：SMTP 尚未接入，统一返回失败说明（需求 18.4）。

    投递结果同时校验 HTTP 状态码与响应体业务码（需求 18.4）。本函数自身不抛
    网络异常，捕获后转为失败返回值，保证调用方推送主流程不被打断。

    Args:
        channel_type: 渠道类型（email/webhook/wecom/feishu）。
        target: 目标地址（邮箱 / Webhook URL 等）。
        content: 通知内容（中文）。
        keyword: 飞书机器人自定义关键词（可选，为空用兜底词「提醒」）。
        secret: 飞书机器人签名密钥（可选）。

    Returns:
        二元组 (是否成功, 结果描述中文)。
    """
    if not target or not str(target).strip():
        return False, "通知目标地址未配置"

    url = str(target).strip()
    kind = _resolve_channel_kind(channel_type, url)
    if kind == "email":
        # SMTP 设置由系统设置模块（任务 8.2）提供，此处尚未接入。
        return False, "邮件渠道尚未配置 SMTP，暂无法发送"

    text = effective_text(content, channel_type, url, keyword)
    payload = _build_payload(kind, text, secret)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_SEND_TIMEOUT_SECONDS) as resp:
            status = int(getattr(resp, "status", None) or resp.getcode() or 0)
            raw = resp.read()
            if not 200 <= status < 300:
                detail = raw.decode("utf-8", errors="replace")[:200]
                return False, f"渠道返回非 2xx 状态码：{status} {detail}".strip()
            return _parse_channel_response(raw, kind)
    except urllib.error.HTTPError as exc:
        # HTTPError 属于 URLError 子类，需先单独捕获以读取错误响应体。
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 兜底：读不到响应体不影响失败返回
            raw = b""
        _, detail = _parse_channel_response(raw, kind)
        suffix = "" if detail == "发送成功" else f"：{detail}"
        return False, f"渠道返回 HTTP {exc.code}{suffix}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # 网络不可达 / 超时 / 解析错误：转为失败返回，绝不向上抛出。
        return False, f"渠道发送失败：{exc}"


def _record_send(
    session: Session,
    channel_id: Optional[int],
    event_type: Optional[str],
    content: str,
    ok: bool,
    detail: str,
    shop_pk: Optional[int] = None,
) -> NotifyRecord:
    """写入一条通知发送记录，并在失败时补记系统日志（需求 18.4）。

    Args:
        session: 数据库会话。
        channel_id: 通知渠道 ID（普通列，无外键）。
        event_type: 事件类型；测试发送时可为 None。
        content: 通知内容。
        ok: 是否发送成功。
        detail: 结果描述（失败原因或成功提示）。
        shop_pk: 归属店铺主键（店铺级渠道记录其归属，便于按店铺查询）。

    Returns:
        已持久化的通知发送记录实例。
    """
    record = Repository(NotifyRecord, session).create(
        shop_pk=shop_pk,
        channel_id=channel_id,
        event_type=event_type,
        content=content,
        send_result=SEND_RESULT_SUCCESS if ok else SEND_RESULT_FAILED,
        log_time=now_beijing_naive(),
    )
    if not ok:
        # 失败记日志不中断主流程（需求 18.4）。
        _write_system_log(
            session,
            level="warning",
            content=f"通知发送失败（渠道 {channel_id}）：{detail}",
        )
    return record


# ----------------------------------------------------------------------
# 数据范围隔离辅助（需求 3.7：非管理员仅可操作本人 / 被授权店铺）
# ----------------------------------------------------------------------
def _ensure_shop_in_scope(
    session: Session, shop_pk: Optional[int], operator_id: Optional[int]
) -> Optional[ApiResponse]:
    """校验店铺存在且在当前用户数据范围内，越权返回失败响应（需求 3.7）。

    管理员不受限；非管理员仅可操作本人创建或被授权的店铺。与 reply / filter /
    risk_control 等店铺级服务保持一致的隔离语义。

    Args:
        session: 数据库会话。
        shop_pk: 店铺主键 shop.id。
        operator_id: 操作人用户 ID；为空（如系统内部调用）时不做隔离校验。

    Returns:
        校验通过返回 None；店铺不存在或越权返回对应失败响应。
    """
    if operator_id is None:
        # 系统内部调用（如事件推送）无操作人上下文，按既有语义不做用户级隔离。
        return None
    if shop_pk is None:
        return error_response(CODE_PARAM_ERROR, "缺少归属店铺")
    shop = Repository(Shop, session).get(int(shop_pk))
    if shop is None:
        return error_response(CODE_NOT_FOUND, "店铺不存在")
    user = Repository(SysUser, session).get(operator_id)
    scope = build_data_scope(user, session=session)
    if not is_in_scope(scope, shop.owner_user_id):
        return error_response(CODE_FORBIDDEN, MSG_FORBIDDEN)
    return None


# ----------------------------------------------------------------------
# 通知渠道配置（需求 18.1）
# ----------------------------------------------------------------------
def create_notify_channel(
    session: Session,
    channel_type: str,
    target: Optional[str],
    *,
    shop_pk: Optional[int] = None,
    enabled: bool = True,
    operator_id: Optional[int] = None,
    keyword: Optional[str] = None,
    secret: Optional[str] = None,
) -> ApiResponse:
    """创建通知渠道并持久化（需求 18.1）。

    Args:
        session: 数据库会话。
        channel_type: 渠道类型（email/webhook/wecom）。
        target: 通知目标地址（邮箱 / Webhook URL 等）。
        shop_pk: 归属店铺主键（店铺级渠道必填）。
        enabled: 是否启用，默认启用。
        operator_id: 操作人用户 ID，作为创建人审计字段。
        keyword: 飞书机器人自定义关键词（选填，为空时兜底「提醒」）。
        secret: 飞书机器人签名密钥（选填）。

    Returns:
        统一响应体：成功返回 data=渠道信息；失败返回对应中文提示。
    """
    if channel_type not in ALLOWED_CHANNEL_TYPES:
        return error_response(
            CODE_PARAM_ERROR,
            f"渠道类型非法，可选值为 {list(ALLOWED_CHANNEL_TYPES)}",
        )
    if not target or not str(target).strip():
        return error_response(CODE_PARAM_ERROR, "通知目标地址不能为空")
    if shop_pk is None:
        return error_response(CODE_PARAM_ERROR, "缺少归属店铺")
    # 数据范围隔离：非管理员仅可为本人 / 被授权店铺配置渠道（需求 3.7）。
    denied = _ensure_shop_in_scope(session, shop_pk, operator_id)
    if denied is not None:
        return denied

    channel = Repository(NotifyChannel, session).create(
        shop_pk=int(shop_pk),
        channel_type=channel_type,
        target=str(target).strip(),
        keyword=(str(keyword).strip() or None) if keyword is not None else None,
        secret=(str(secret).strip() or None) if secret is not None else None,
        enabled=bool(enabled),
        created_by=operator_id,
    )
    return success_response(
        data=serialize_channel(channel),
        message="创建成功",
    )


def update_notify_channel(
    session: Session,
    channel_id: int,
    *,
    channel_type: Optional[str] = None,
    target: Optional[str] = None,
    enabled: Optional[bool] = None,
    operator_id: Optional[int] = None,
    keyword: Optional[str] = None,
    secret: Optional[str] = None,
) -> ApiResponse:
    """修改通知渠道字段（需求 18.1 配套）。

    仅更新显式提供（非 None）的字段。渠道类型显式提供时须为合法枚举。

    Args:
        session: 数据库会话。
        channel_id: 目标渠道 ID。
        channel_type: 新渠道类型；None 表示不修改。
        target: 新目标地址；None 表示不修改。
        enabled: 新启停用状态；None 表示不修改。
        operator_id: 操作人用户 ID，用于店铺归属数据范围隔离（需求 3.7）。
        keyword: 飞书自定义关键词；None 表示不修改，空串表示清空。
        secret: 飞书签名密钥；None 表示不修改，空串表示清空。

    Returns:
        统一响应体：成功返回更新后的渠道信息。
    """
    repo = Repository(NotifyChannel, session)
    channel = repo.get(channel_id)
    if channel is None:
        return error_response(CODE_NOT_FOUND, "目标通知渠道不存在")
    # 数据范围隔离：非管理员仅可操作本人 / 被授权店铺的渠道（需求 3.7）。
    denied = _ensure_shop_in_scope(session, channel.shop_pk, operator_id)
    if denied is not None:
        return denied

    values: Dict[str, Any] = {}
    if channel_type is not None:
        if channel_type not in ALLOWED_CHANNEL_TYPES:
            return error_response(
                CODE_PARAM_ERROR,
                f"渠道类型非法，可选值为 {list(ALLOWED_CHANNEL_TYPES)}",
            )
        values["channel_type"] = channel_type
    if target is not None:
        if not str(target).strip():
            return error_response(CODE_PARAM_ERROR, "通知目标地址不能为空")
        values["target"] = str(target).strip()
    if enabled is not None:
        values["enabled"] = bool(enabled)
    if keyword is not None:
        values["keyword"] = str(keyword).strip() or None
    if secret is not None:
        values["secret"] = str(secret).strip() or None

    if not values:
        return error_response(CODE_PARAM_ERROR, "未提供任何待更新字段")

    repo.update(channel_id, **values)
    return success_response(
        data=serialize_channel(channel),
        message="更新成功",
    )


def list_notify_channels(
    session: Session,
    page: Any = 1,
    page_size: Any = 20,
    enabled: Optional[bool] = None,
    shop_pk: Optional[int] = None,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """分页查询通知渠道列表（需求 18.5 配套，后端分页）。

    默认按创建时间倒序（仓储层自动探测时间字段）。可按店铺与启用状态筛选。

    Args:
        session: 数据库会话。
        page: 页码（从 1 开始，将被规整）。
        page_size: 每页条数（10/20/50/100，将被规整）。
        enabled: 按启用状态筛选；None 表示不筛选。
        shop_pk: 按归属店铺筛选；None 表示不筛选。
        operator_id: 操作人用户 ID，用于店铺归属数据范围隔离（需求 3.7）。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    # 数据范围隔离：按店铺查询时校验该店铺是否在当前用户可见范围内（需求 3.7）。
    if shop_pk is not None:
        denied = _ensure_shop_in_scope(session, shop_pk, operator_id)
        if denied is not None:
            return denied

    filters: Dict[str, Any] = {}
    if enabled is not None:
        filters["enabled"] = bool(enabled)
    if shop_pk is not None:
        filters["shop_pk"] = int(shop_pk)

    page_result = Repository(NotifyChannel, session).paginate(
        page=page, page_size=page_size, filters=filters or None
    )
    serialized: List[Dict[str, Any]] = [
        serialize_channel(channel) for channel in page_result.items
    ]
    data = {
        "list": serialized,
        "total": page_result.total,
        "page": page_result.page,
        "page_size": page_result.page_size,
    }
    return success_response(data=data, message="查询成功")


# ----------------------------------------------------------------------
# 测试发送（需求 18.2 / 18.4）
# ----------------------------------------------------------------------
def test_notify_channel(
    session: Session,
    channel_id: int,
    content: Optional[str] = None,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """对某通知渠道发起测试发送并返回发送结果（需求 18.2）。

    经渠道实际投递（``send_via_channel``）并写入通知记录；发送失败时记通知失败
    日志，但仍以统一响应体返回失败原因，不抛异常中断（需求 18.4）。

    Args:
        session: 数据库会话。
        channel_id: 目标渠道 ID。
        content: 测试通知内容；None 时使用默认测试文案。
        operator_id: 操作人用户 ID，用于店铺归属数据范围隔离（需求 3.7）。

    Returns:
        统一响应体：发送成功返回 success=true；失败返回失败原因（HTTP 恒 200）。
    """
    channel = Repository(NotifyChannel, session).get(channel_id)
    if channel is None:
        return error_response(CODE_NOT_FOUND, "目标通知渠道不存在")
    # 数据范围隔离：非管理员仅可操作本人 / 被授权店铺的渠道（需求 3.7）。
    denied = _ensure_shop_in_scope(session, channel.shop_pk, operator_id)
    if denied is not None:
        return denied

    test_content = (content or "").strip() or "这是一条测试通知"
    keyword = getattr(channel, "keyword", None)
    secret = getattr(channel, "secret", None)
    # 飞书渠道会自动补自定义关键词，通知记录里保存真正发出去的文本，便于核对。
    sent_text = effective_text(
        test_content, channel.channel_type, channel.target, keyword
    )
    ok, detail = send_via_channel(
        channel.channel_type,
        channel.target,
        test_content,
        keyword=keyword,
        secret=secret,
    )
    record = _record_send(
        session,
        channel_id=channel.id,
        event_type=None,
        content=sent_text,
        ok=ok,
        detail=detail,
        shop_pk=channel.shop_pk,
    )

    result_data = {
        "channel_id": channel.id,
        "send_result": record.send_result,
        "detail": detail,
        "content": sent_text,
    }
    if ok:
        return success_response(data=result_data, message="测试通知已发送")
    # 失败：返回失败原因，HTTP 恒 200，不中断（需求 18.4）。
    return error_response(CODE_EXTERNAL_ERROR, f"测试通知发送失败：{detail}")


# ----------------------------------------------------------------------
# 系统事件推送（需求 18.3 / 18.4）
# ----------------------------------------------------------------------
def push_system_event(
    session: Session,
    event_type: str,
    content: str,
    shop_pk: Optional[int] = None,
    *,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """发生需通知的系统事件时，经该店铺全部已启用渠道推送通知（需求 18.3）。

    逐个已启用渠道发送，单个渠道失败仅记通知失败日志与系统日志，不中断对其它
    渠道的推送与主流程（需求 18.4）。即便全部失败或无可用渠道，本函数仍返回
    成功响应（推送动作已完成，失败明细已逐条记录），保证不打断触发事件的主流程。

    Args:
        session: 数据库会话。
        event_type: 事件类型（connection_disconnected/login_expired/risk_triggered）。
        content: 通知内容（中文）。
        shop_pk: 事件归属店铺主键；仅推送该店铺的已启用渠道（店铺级通知，方案 A）。
        operator_id: 操作人用户 ID；经由接口触发时做店铺归属数据范围隔离（需求 3.7）；
            系统内部调用（websocket / scheduler）传 None 表示不做用户级隔离。

    Returns:
        统一响应体：data 为推送统计 {total, success, failed}。
    """
    if event_type not in EVENT_TYPE_LABELS:
        return error_response(
            CODE_PARAM_ERROR,
            f"事件类型非法，可选值为 {list(EVENT_TYPE_LABELS)}",
        )

    # 数据范围隔离：经接口触发（operator_id 非空）时，仅可向本人 / 被授权店铺
    # 推送，避免非管理员向任意店铺触发事件推送（需求 3.7 / 规范 42a）。
    if operator_id is not None:
        denied = _ensure_shop_in_scope(session, shop_pk, operator_id)
        if denied is not None:
            return denied

    # 仅经「该店铺 + 已启用」渠道推送（需求 18.3，方案 A 店铺级隔离）。
    channel_filters: Dict[str, Any] = {"enabled": True}
    if shop_pk is not None:
        channel_filters["shop_pk"] = int(shop_pk)
    channels = Repository(NotifyChannel, session).list(
        filters=channel_filters, order_by=False
    )

    total = len(channels)
    success_count = 0
    failed_count = 0
    for channel in channels:
        # 单渠道发送以兜底方式执行，异常转为失败记录，绝不中断其它渠道（需求 18.4）。
        keyword = getattr(channel, "keyword", None)
        secret = getattr(channel, "secret", None)
        sent_text = effective_text(
            content, channel.channel_type, channel.target, keyword
        )
        try:
            ok, detail = send_via_channel(
                channel.channel_type,
                channel.target,
                content,
                keyword=keyword,
                secret=secret,
            )
        except Exception as exc:  # noqa: BLE001 兜底：任何异常均不得中断推送主流程
            ok, detail = False, f"渠道发送异常：{exc}"
            logger.warning("通知渠道发送异常（渠道 %s）：%s", channel.id, exc)
        _record_send(
            session,
            channel_id=channel.id,
            event_type=event_type,
            content=sent_text,
            ok=ok,
            detail=detail,
            shop_pk=channel.shop_pk,
        )
        if ok:
            success_count += 1
        else:
            failed_count += 1

    data = {"total": total, "success": success_count, "failed": failed_count}
    return success_response(data=data, message="事件通知已推送")


# ----------------------------------------------------------------------
# 通知记录后端分页（需求 18.5）
# ----------------------------------------------------------------------
def list_notify_records(
    session: Session,
    page: Any = 1,
    page_size: Any = 20,
    channel_id: Optional[int] = None,
    event_type: Optional[str] = None,
    send_result: Optional[str] = None,
    shop_pk: Optional[int] = None,
    *,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """分页查询通知记录（需求 18.5，后端分页）。

    按北京时间（created_at）倒序、后端分页（默认 20，可选 10/20/50/100）返回通知
    发送记录。可按店铺、渠道、事件类型、发送结果筛选。非管理员仅可见其可访问
    店铺下的通知记录（数据范围隔离，需求 3.7 / 规范 42a）。

    Args:
        session: 数据库会话。
        page: 页码（从 1 开始，将被规整）。
        page_size: 每页条数（10/20/50/100，将被规整）。
        channel_id: 按通知渠道筛选；None 表示不筛选。
        event_type: 按事件类型筛选；None 表示不筛选。
        send_result: 按发送结果筛选（success/failed）；None 表示不筛选。
        shop_pk: 按归属店铺筛选；None 表示该用户可见范围内全部店铺。
        operator_id: 当前操作用户 ID（数据范围校验）。

    Returns:
        统一响应体：data 为分页结构 {list, total, page, page_size}。
    """
    # 组装附加等值筛选条件（渠道 / 事件类型 / 发送结果）。
    extra_filters: Dict[str, Any] = {}
    if channel_id is not None:
        extra_filters["channel_id"] = int(channel_id)
    if event_type is not None:
        extra_filters["event_type"] = event_type
    if send_result is not None:
        extra_filters["send_result"] = send_result

    # 经共享辅助做带数据范围隔离的后端分页（管理员可见全部，非管理员限可见店铺）。
    return paginate_shop_scoped(
        session,
        model=NotifyRecord,
        serializer=serialize_record,
        page=page,
        page_size=page_size,
        operator_id=operator_id,
        shop_pk=int(shop_pk) if shop_pk is not None else None,
        extra_filters=extra_filters or None,
    )


# ----------------------------------------------------------------------
# 通知渠道类型字典（需求 18.x / 24.7：枚举入字典并前端中文展示）
# ----------------------------------------------------------------------
def list_channel_types(session: Session) -> ApiResponse:
    """查询「通知渠道类型」枚举字典，供前端展示中文文案（需求 18.x / 24.7）。

    从数据字典 ``channel_type`` 查出启用项（按 order_no 升序），返回 key 与中文
    标签列表，前端据此展示渠道类型中文文案。

    Args:
        session: 数据库会话。

    Returns:
        统一响应体：data 为渠道类型列表 [{key, label}, ...]。
    """
    items = DictService(session).list_by_type(DICT_CHANNEL_TYPE)
    channel_types: List[Dict[str, Any]] = [
        {"key": item.dict_key, "label": item.dict_label} for item in items
    ]
    return success_response(data=channel_types, message="查询成功")


__all__ = [
    "DICT_CHANNEL_TYPE",
    "ALLOWED_CHANNEL_TYPES",
    "EVENT_CONNECTION_DISCONNECTED",
    "EVENT_LOGIN_EXPIRED",
    "EVENT_RISK_TRIGGERED",
    "EVENT_TYPE_LABELS",
    "SEND_RESULT_SUCCESS",
    "SEND_RESULT_FAILED",
    "FEISHU_FALLBACK_KEYWORD",
    "serialize_channel",
    "serialize_record",
    "effective_text",
    "send_via_channel",
    "create_notify_channel",
    "update_notify_channel",
    "list_notify_channels",
    "test_notify_channel",
    "push_system_event",
    "list_notify_records",
    "list_channel_types",
]
