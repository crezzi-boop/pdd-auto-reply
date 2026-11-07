# -*- coding: utf-8 -*-
"""
backend.app.api.routes.notify —— 通知渠道与消息通知接口路由
==========================================================
本文件用途：提供 backend 服务的「通知渠道与消息通知」REST 接口，满足需求 18
（通知渠道与消息通知）：

- ``POST   /notify/channels``                通知渠道配置（创建，需求 18.1）。
- ``PUT    /notify/channels/{channel_id}``   通知渠道修改（需求 18.1 配套）。
- ``GET    /notify/channels``                通知渠道列表（后端分页，需求 18.5 配套）。
- ``POST   /notify/channels/{channel_id}/test``  测试发送（需求 18.2）。
- ``POST   /notify/events``                  系统事件推送（连接断开 / 登录态失效 /
  风控触发，需求 18.3 / 18.4）。
- ``GET    /notify/records``                 通知记录（后端分页，需求 18.5）。
- ``GET    /notify/channel-types``           通知渠道类型枚举字典（中文文案，需求 18.x）。

权限控制（需求 2.4）：所有接口先经统一权限模块 ``permission.check`` 判断当前
用户对资源 ``notify`` 的对应操作（view / create / update）是否被授权；未授权
返回 ``success=false``、``message``「无访问权限」的统一响应体（HTTP 恒 200）。

接口约定（开发规范 1-3）：
- 所有接口 HTTP 恒返回 200，业务成败由统一响应体 {code, success, message,
  data} 表达。
- 业务逻辑委托 app.services.notify_service，路由层仅负责入参解析、鉴权依赖
  与权限判断；数据库会话经 common.db.session.get_db 注入。
- 通知发送失败记日志不中断主流程（需求 18.4）；通知记录后端分页（需求 18.5）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core import permission
from app.core.business_codes import CODE_FORBIDDEN, MSG_FORBIDDEN
from app.services import notify_service
from common.db.session import get_db
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response

# 通知路由：标签「通知」便于 OpenAPI 文档分组；前缀由聚合层添加。
router = APIRouter(tags=["通知"])

# 受保护资源键约定（与 sys_permission.resource_key 对齐）。
# 通知记录 / 渠道类型字典 / 事件推送沿用 notify 资源（消息通知为独立菜单）；
# 通知「渠道」配置为店铺级设置，入口收敛到店铺管理页，统一用 shop 资源判权
# （有店铺管理权限即可配置本店铺通知渠道，与其它店铺级设置一致，规范 42a）。
RESOURCE_NOTIFY: str = "notify"
RESOURCE_SHOP: str = "shop"


def _ensure_permission(
    user: SysUser, action: str, session: Session, resource: str = RESOURCE_NOTIFY
) -> Optional[ApiResponse]:
    """统一权限校验：未授权时返回「无访问权限」响应，授权时返回 None。

    依据需求 2.4，集中经 ``permission.check`` 判断对指定资源的操作权限；未授权
    返回 success=false、message「无访问权限」的统一响应体。

    Args:
        user: 当前登录用户。
        action: 操作（view / create / update）。
        session: 数据库会话。
        resource: 受保护资源键（默认 notify；店铺级渠道配置传 shop）。

    Returns:
        未授权时返回失败响应体；已授权返回 None。
    """
    if permission.check(user, resource, action, session=session):
        return None
    return error_response(CODE_FORBIDDEN, MSG_FORBIDDEN)


# ----------------------------------------------------------------------
# 请求体模型
# ----------------------------------------------------------------------
class CreateChannelRequest(BaseModel):
    """创建通知渠道请求体（需求 18.1）。"""

    shop_pk: int = Field(..., description="归属店铺主键 shop.id（店铺级通知渠道）")
    channel_type: str = Field(..., description="渠道类型：email/webhook/wecom/feishu")
    target: str = Field(..., description="通知目标地址（邮箱 / Webhook URL 等）")
    enabled: bool = Field(True, description="是否启用")
    keyword: Optional[str] = Field(
        None, description="飞书机器人自定义关键词（选填，为空时兜底「提醒」）"
    )
    secret: Optional[str] = Field(None, description="飞书机器人签名密钥（选填）")


class UpdateChannelRequest(BaseModel):
    """修改通知渠道请求体（需求 18.1 配套；仅更新显式提供字段）。"""

    channel_type: Optional[str] = Field(
        None, description="渠道类型：email/webhook/wecom/feishu"
    )
    target: Optional[str] = Field(None, description="通知目标地址")
    enabled: Optional[bool] = Field(None, description="是否启用")
    keyword: Optional[str] = Field(None, description="飞书自定义关键词（空串表示清空）")
    secret: Optional[str] = Field(None, description="飞书签名密钥（空串表示清空）")


class TestChannelRequest(BaseModel):
    """测试发送请求体（需求 18.2）。"""

    content: Optional[str] = Field(None, description="测试通知内容（为空使用默认文案）")


class PushEventRequest(BaseModel):
    """系统事件推送请求体（需求 18.3）。"""

    event_type: str = Field(
        ...,
        description="事件类型：connection_disconnected/login_expired/risk_triggered",
    )
    content: str = Field(..., description="通知内容（中文）")
    shop_pk: Optional[int] = Field(
        None, description="事件归属店铺主键；仅推送该店铺的已启用渠道（店铺级通知）"
    )


# ----------------------------------------------------------------------
# 通知渠道配置接口（需求 18.1）
# ----------------------------------------------------------------------
@router.post(
    "/notify/channels", response_model=ApiResponse, summary="创建通知渠道"
)
def create_channel(
    payload: CreateChannelRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """配置并持久化通知渠道（需求 18.1）。"""
    denied = _ensure_permission(current_user, "create", db, RESOURCE_SHOP)
    if denied is not None:
        return denied
    return notify_service.create_notify_channel(
        db,
        channel_type=payload.channel_type,
        target=payload.target,
        shop_pk=payload.shop_pk,
        enabled=payload.enabled,
        operator_id=current_user.id,
        keyword=payload.keyword,
        secret=payload.secret,
    )


@router.put(
    "/notify/channels/{channel_id}",
    response_model=ApiResponse,
    summary="修改通知渠道",
)
def update_channel(
    channel_id: int,
    payload: UpdateChannelRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """修改通知渠道字段（需求 18.1 配套）。"""
    denied = _ensure_permission(current_user, "update", db, RESOURCE_SHOP)
    if denied is not None:
        return denied
    return notify_service.update_notify_channel(
        db,
        channel_id,
        channel_type=payload.channel_type,
        target=payload.target,
        enabled=payload.enabled,
        operator_id=current_user.id,
        keyword=payload.keyword,
        secret=payload.secret,
    )


@router.get(
    "/notify/channels",
    response_model=ApiResponse,
    summary="通知渠道列表（后端分页）",
)
def list_channels(
    page: int = Query(1, description="页码（从 1 开始）"),
    page_size: int = Query(20, description="每页条数（10/20/50/100）"),
    enabled: Optional[bool] = Query(None, description="按启用状态筛选"),
    shop_pk: Optional[int] = Query(None, description="按归属店铺筛选（店铺级通知渠道）"),
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """分页查询通知渠道列表（需求 18.5 配套）。"""
    denied = _ensure_permission(current_user, "view", db, RESOURCE_SHOP)
    if denied is not None:
        return denied
    return notify_service.list_notify_channels(
        db, page=page, page_size=page_size, enabled=enabled, shop_pk=shop_pk,
        operator_id=current_user.id,
    )


# ----------------------------------------------------------------------
# 测试发送接口（需求 18.2）
# ----------------------------------------------------------------------
@router.post(
    "/notify/channels/{channel_id}/test",
    response_model=ApiResponse,
    summary="测试发送通知",
)
def test_channel(
    channel_id: int,
    payload: TestChannelRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """对某通知渠道发起测试发送并返回发送结果（需求 18.2 / 18.4）。"""
    denied = _ensure_permission(current_user, "update", db, RESOURCE_SHOP)
    if denied is not None:
        return denied
    return notify_service.test_notify_channel(
        db, channel_id, content=payload.content, operator_id=current_user.id
    )


# ----------------------------------------------------------------------
# 系统事件推送接口（需求 18.3 / 18.4）
# ----------------------------------------------------------------------
@router.post(
    "/notify/events", response_model=ApiResponse, summary="推送系统事件通知"
)
def push_event(
    payload: PushEventRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """推送系统事件通知（连接断开 / 登录态失效 / 风控触发，需求 18.3 / 18.4）。"""
    denied = _ensure_permission(current_user, "create", db)
    if denied is not None:
        return denied
    return notify_service.push_system_event(
        db,
        event_type=payload.event_type,
        content=payload.content,
        shop_pk=payload.shop_pk,
        operator_id=current_user.id,
    )


# ----------------------------------------------------------------------
# 通知记录接口（后端分页，需求 18.5）
# ----------------------------------------------------------------------
@router.get(
    "/notify/records",
    response_model=ApiResponse,
    summary="通知记录列表（后端分页）",
)
def list_records(
    page: int = Query(1, description="页码（从 1 开始）"),
    page_size: int = Query(20, description="每页条数（10/20/50/100）"),
    channel_id: Optional[int] = Query(None, description="按通知渠道筛选"),
    event_type: Optional[str] = Query(None, description="按事件类型筛选"),
    send_result: Optional[str] = Query(None, description="按发送结果筛选：success/failed"),
    shop_pk: Optional[int] = Query(None, description="按归属店铺筛选"),
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """分页查询通知记录（需求 18.5）。"""
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return notify_service.list_notify_records(
        db,
        page=page,
        page_size=page_size,
        channel_id=channel_id,
        event_type=event_type,
        send_result=send_result,
        shop_pk=shop_pk,
        operator_id=current_user.id,
    )


# ----------------------------------------------------------------------
# 通知渠道类型枚举字典接口（需求 18.x / 24.7）
# ----------------------------------------------------------------------
@router.get(
    "/notify/channel-types",
    response_model=ApiResponse,
    summary="查询通知渠道类型枚举（中文文案）",
)
def list_channel_types(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询通知渠道类型枚举字典，供前端中文展示（需求 18.x / 24.7）。"""
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return notify_service.list_channel_types(db)


__all__ = ["router"]
