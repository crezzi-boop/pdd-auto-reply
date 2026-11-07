# -*- coding: utf-8 -*-
"""
backend.app.api.routes.transfer —— 转人工设置接口路由（需求 16.1）
================================================================
本文件用途：提供 backend 服务的「转人工设置」REST 接口，满足需求 16.1：

- ``GET  /shops/{shop_pk}/cs-list``        查询某店铺可分配的人工客服列表。
- ``GET  /transfer-keywords``              转人工关键词列表（后端分页）。
- ``POST /transfer-keywords``              新增转人工关键词。
- ``PUT  /transfer-keywords/{id}/status``  启用 / 停用转人工关键词。

权限控制（需求 2.4）：转人工设置归属「自动回复」业务，与前端菜单一致使用资源键
``reply`` 判权；未授权返回 success=false、message「无访问权限」的统一响应体
（HTTP 恒 200）。

接口约定（开发规范 1-3）：所有接口 HTTP 恒返回 200，业务成败由统一响应体
{code, success, message, data} 表达；业务逻辑委托 app.services.transfer_service。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core import permission
from app.core.business_codes import (
    CODE_FORBIDDEN,
    CODE_PARAM_ERROR,
    MSG_FORBIDDEN,
)
from app.services import transfer_service
from common.db.session import get_db
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response, success_response
from common.services import reply_guard_settings

# 转人工设置路由：标签便于 OpenAPI 文档分组；前缀由聚合层添加。
router = APIRouter(tags=["转人工设置"])

# 受保护资源键：转人工为「店铺级设置」，统一归属店铺管理（shop）资源判权——
# 入口收敛到店铺管理页后，有店铺管理权限即可操作店铺级设置（与前端入口一致）。
RESOURCE_REPLY: str = "shop"


def _ensure_permission(
    user: SysUser, action: str, session: Session
) -> Optional[ApiResponse]:
    """统一权限校验：未授权时返回「无访问权限」响应，授权时返回 None。

    Args:
        user: 当前登录用户。
        action: 操作（view / create / update / disable）。
        session: 数据库会话。

    Returns:
        未授权时返回失败响应体；已授权返回 None。
    """
    if permission.check(user, RESOURCE_REPLY, action, session=session):
        return None
    return error_response(CODE_FORBIDDEN, MSG_FORBIDDEN)


# ----------------------------------------------------------------------
# 请求体模型
# ----------------------------------------------------------------------
class CreateTransferKeywordRequest(BaseModel):
    """新增转人工关键词请求体。"""

    shop_pk: int = Field(..., description="关联店铺主键 shop.id")
    keyword: str = Field(..., description="转人工关键词")
    enabled: bool = Field(True, description="是否启用")


class UpdateStatusRequest(BaseModel):
    """启停用请求体。"""

    enabled: bool = Field(..., description="True=启用，False=停用（下一条消息生效）")


class ReplyGuardRequest(BaseModel):
    """人工介入与回复保底设置请求体（两个秒值，按店铺保存）。"""

    manual_delay_seconds: int = Field(
        ..., description="人工介入后机器延迟回复秒数（0~60）"
    )
    manual_recover_seconds: int = Field(
        ..., description="人工介入自动恢复秒数（30~3600，0=不自动恢复）"
    )


# ----------------------------------------------------------------------
# 客服列表（需求 16.1）
# ----------------------------------------------------------------------
@router.get(
    "/shops/{shop_pk}/cs-list",
    response_model=ApiResponse,
    summary="查询某店铺可分配的人工客服列表",
)
def list_cs_list(
    shop_pk: int,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询某店铺可分配的人工客服列表（需求 16.1）。"""
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return transfer_service.list_cs_list(
        db, shop_pk=shop_pk, operator_id=current_user.id
    )


# ----------------------------------------------------------------------
# 人工介入与回复保底设置（按店铺的两个秒值）
# ----------------------------------------------------------------------
@router.get(
    "/shops/{shop_pk}/reply-guard",
    response_model=ApiResponse,
    summary="查询某店铺的人工介入延迟与自动恢复秒数",
)
def get_reply_guard(
    shop_pk: int,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询回复保底设置；未配置时返回默认值（延迟 30 秒 / 自动恢复 120 秒）。"""
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    data = reply_guard_settings.load_config(db, shop_pk)
    data["guard_deadline_seconds"] = reply_guard_settings.GUARD_DEADLINE_SECONDS
    return success_response(data=data, message="查询成功")


@router.put(
    "/shops/{shop_pk}/reply-guard",
    response_model=ApiResponse,
    summary="保存某店铺的人工介入延迟与自动恢复秒数",
)
def save_reply_guard(
    shop_pk: int,
    payload: ReplyGuardRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """保存两个秒值；前后端均校验，越界返回中文错误提示。"""
    denied = _ensure_permission(current_user, "update", db)
    if denied is not None:
        return denied
    try:
        data = reply_guard_settings.save_config(
            db,
            shop_pk,
            payload.manual_delay_seconds,
            payload.manual_recover_seconds,
            operator_id=current_user.id,
        )
    except ValueError as exc:
        return error_response(CODE_PARAM_ERROR, str(exc))
    data["guard_deadline_seconds"] = reply_guard_settings.GUARD_DEADLINE_SECONDS
    return success_response(data=data, message="回复保底设置已保存")


# ----------------------------------------------------------------------
# 转人工关键词列表（后端分页）
# ----------------------------------------------------------------------
@router.get(
    "/transfer-keywords",
    response_model=ApiResponse,
    summary="转人工关键词列表（后端分页）",
)
def list_transfer_keywords(
    shop_pk: Optional[int] = Query(None, description="按店铺主键筛选"),
    page: int = Query(1, description="页码（从 1 开始）"),
    page_size: int = Query(20, description="每页条数（10/20/50/100）"),
    enabled: Optional[bool] = Query(None, description="按启用状态筛选"),
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """分页查询转人工关键词列表（需求 16.1，后端分页）。"""
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return transfer_service.list_transfer_keywords(
        db,
        page=page,
        page_size=page_size,
        shop_pk=shop_pk,
        enabled=enabled,
        operator_id=current_user.id,
    )


# ----------------------------------------------------------------------
# 新增转人工关键词
# ----------------------------------------------------------------------
@router.post(
    "/transfer-keywords",
    response_model=ApiResponse,
    summary="新增转人工关键词",
)
def create_transfer_keyword(
    payload: CreateTransferKeywordRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """新增转人工关键词（需求 16.1 / 16.3）。"""
    denied = _ensure_permission(current_user, "create", db)
    if denied is not None:
        return denied
    return transfer_service.create_transfer_keyword(
        db,
        shop_pk=payload.shop_pk,
        keyword=payload.keyword,
        enabled=payload.enabled,
        operator_id=current_user.id,
    )


# ----------------------------------------------------------------------
# 启停用转人工关键词
# ----------------------------------------------------------------------
@router.put(
    "/transfer-keywords/{keyword_id}/status",
    response_model=ApiResponse,
    summary="启用 / 停用转人工关键词",
)
def update_transfer_keyword_status(
    keyword_id: int,
    payload: UpdateStatusRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """启用或停用转人工关键词（需求 16.1）。

    停用关键词不参与转人工判定；该变更在下一条消息处理时由 websocket 引擎实时读取生效。
    """
    action = "update" if payload.enabled else "disable"
    denied = _ensure_permission(current_user, action, db)
    if denied is not None:
        return denied
    return transfer_service.set_transfer_keyword_status(
        db, keyword_id=keyword_id, enabled=payload.enabled, operator_id=current_user.id
    )


__all__ = ["router"]
