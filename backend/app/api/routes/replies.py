# -*- coding: utf-8 -*-
"""
backend.app.api.routes.replies —— 默认回复与商品专属回复接口路由
================================================================
本文件用途：提供 backend 服务的「默认回复」与「商品专属回复」REST 接口，满足
需求 7（默认回复与商品专属回复）：

默认回复（按店铺维度一条配置，可含多条兜底语句）：
- ``GET    /default-replies``        查询某店铺默认回复配置（需求 7.1 配套）。
- ``PUT    /default-replies``        保存（upsert）默认回复配置（需求 7.1）；
  ``contents`` 为多条语句数组，``rotate_minutes`` 为不重复轮换窗口（默认 10 分钟）。
- ``PUT    /default-replies/status`` 启用 / 停用默认回复。

商品专属回复（绑定 goods_id，优先级高于默认，需求 7.2/7.4）：
- ``POST   /goods-replies``               新增 / upsert 商品专属回复（需求 7.3）。
- ``GET    /goods-replies``               商品专属回复列表（后端分页，需求 7.5）。
- ``PUT    /goods-replies/{reply_id}``        更新商品专属回复。
- ``PUT    /goods-replies/{reply_id}/status`` 启用 / 停用商品专属回复。
- ``DELETE /goods-replies/{reply_id}``        逻辑删除商品专属回复（需求 24.6）。

权限控制（需求 2.4）：所有接口先经统一权限模块 ``permission.check`` 判断当前
用户对资源 ``reply`` 的对应操作是否被授权；未授权返回 success=false、message
「无访问权限」的统一响应体（HTTP 恒 200）。数据范围隔离在服务层按店铺归属
统一处理（需求 3.7）。

接口约定（开发规范 1-3）：所有接口 HTTP 恒返回 200，业务成败由统一响应体
{code, success, message, data} 表达；业务逻辑委托 app.services.reply_service。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core import permission
from app.core.business_codes import CODE_FORBIDDEN, MSG_FORBIDDEN
from app.services import reply_service
from common.db.session import get_db
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response
from common.services.default_reply_variants import DEFAULT_ROTATE_MINUTES

# 默认回复与商品专属回复路由：标签便于 OpenAPI 文档分组；前缀由聚合层添加。
router = APIRouter(tags=["默认回复与商品专属回复"])

# 受保护资源键：默认/商品回复为「店铺级设置」，统一归属店铺管理（shop）资源判权——
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
class SaveDefaultReplyRequest(BaseModel):
    """保存默认回复请求体（支持多条语句 + 不重复轮换）。"""

    shop_pk: int = Field(..., description="店铺主键 shop.id")
    content: str = Field("", description="兼容旧版的单条默认回复内容（也支持一条一行的多行文本）")
    contents: Optional[List[str]] = Field(
        None,
        description="多条默认回复语句（最多 100 条）；允许把「一条一行」的整段文本作为单个元素传入，后端按行拆分",
    )
    rotate_minutes: int = Field(
        DEFAULT_ROTATE_MINUTES, description="不重复轮换窗口（分钟，1-120，默认 10）"
    )
    enabled: bool = Field(True, description="是否启用")
    reply_once: bool = Field(
        False, description="是否只回复一次（同一客户仅发送一次默认回复）"
    )


class StatusRequest(BaseModel):
    """启停用请求体。"""

    enabled: bool = Field(..., description="True=启用，False=停用")


class DefaultReplyStatusRequest(StatusRequest):
    """默认回复启停用请求体（附带店铺主键）。"""

    shop_pk: int = Field(..., description="店铺主键 shop.id")


class CreateGoodsReplyRequest(BaseModel):
    """新增 / upsert 商品专属回复请求体。"""

    shop_pk: int = Field(..., description="店铺主键 shop.id")
    goods_id: str = Field(..., description="拼多多商品业务标识 goods_id")
    reply_content: str = Field(..., description="回复内容")
    reply_type: str = Field("text", description="回复类型：text/image")
    enabled: bool = Field(True, description="是否启用")


class UpdateGoodsReplyRequest(BaseModel):
    """更新商品专属回复请求体（仅更新传入的非空字段）。"""

    reply_content: Optional[str] = Field(None, description="回复内容")
    reply_type: Optional[str] = Field(None, description="回复类型：text/image")
    enabled: Optional[bool] = Field(None, description="是否启用")


# ----------------------------------------------------------------------
# 默认回复接口
# ----------------------------------------------------------------------
@router.get("/default-replies", response_model=ApiResponse, summary="查询默认回复配置")
def get_default_reply(
    shop_pk: int = Query(..., description="店铺主键 shop.id"),
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询某店铺的默认回复配置（需求 7.1 配套）。"""
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return reply_service.get_default_reply(db, current_user, shop_pk=shop_pk)


@router.put("/default-replies", response_model=ApiResponse, summary="保存默认回复配置")
def save_default_reply(
    payload: SaveDefaultReplyRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """保存（upsert）某店铺的默认回复配置（需求 7.1）。"""
    denied = _ensure_permission(current_user, "update", db)
    if denied is not None:
        return denied
    return reply_service.save_default_reply(
        db,
        current_user,
        shop_pk=payload.shop_pk,
        content=payload.content,
        enabled=payload.enabled,
        reply_once=payload.reply_once,
        contents=payload.contents,
        rotate_minutes=payload.rotate_minutes,
    )


@router.put(
    "/default-replies/status",
    response_model=ApiResponse,
    summary="启用 / 停用默认回复",
)
def set_default_reply_status(
    payload: DefaultReplyStatusRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """启用或停用某店铺的默认回复配置。"""
    action = "update" if payload.enabled else "disable"
    denied = _ensure_permission(current_user, action, db)
    if denied is not None:
        return denied
    return reply_service.set_default_reply_status(
        db, current_user, shop_pk=payload.shop_pk, enabled=payload.enabled
    )


# ----------------------------------------------------------------------
# 商品专属回复接口
# ----------------------------------------------------------------------
@router.post("/goods-replies", response_model=ApiResponse, summary="新增商品专属回复")
def create_goods_reply(
    payload: CreateGoodsReplyRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """新增 / upsert 商品专属回复（绑定 goods_id，需求 7.3）。"""
    denied = _ensure_permission(current_user, "create", db)
    if denied is not None:
        return denied
    return reply_service.create_goods_reply(
        db,
        current_user,
        shop_pk=payload.shop_pk,
        goods_id=payload.goods_id,
        reply_content=payload.reply_content,
        reply_type=payload.reply_type,
        enabled=payload.enabled,
    )


@router.get(
    "/goods-replies",
    response_model=ApiResponse,
    summary="商品专属回复列表（后端分页）",
)
def list_goods_replies(
    shop_pk: int = Query(..., description="店铺主键 shop.id"),
    page: int = Query(1, description="页码（从 1 开始）"),
    page_size: int = Query(20, description="每页条数（10/20/50/100）"),
    enabled: Optional[bool] = Query(None, description="按启停用筛选"),
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """分页查询某店铺的商品专属回复列表（需求 7.5）。"""
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return reply_service.list_goods_replies(
        db,
        current_user,
        shop_pk=shop_pk,
        page=page,
        page_size=page_size,
        enabled=enabled,
    )


@router.put(
    "/goods-replies/{reply_id}",
    response_model=ApiResponse,
    summary="更新商品专属回复",
)
def update_goods_reply(
    reply_id: int,
    payload: UpdateGoodsReplyRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """按主键更新商品专属回复（需求 7.3 配套）。"""
    denied = _ensure_permission(current_user, "update", db)
    if denied is not None:
        return denied
    return reply_service.update_goods_reply(
        db,
        current_user,
        reply_id=reply_id,
        reply_content=payload.reply_content,
        reply_type=payload.reply_type,
        enabled=payload.enabled,
    )


@router.put(
    "/goods-replies/{reply_id}/status",
    response_model=ApiResponse,
    summary="启用 / 停用商品专属回复",
)
def set_goods_reply_status(
    reply_id: int,
    payload: StatusRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """启用或停用商品专属回复。"""
    action = "update" if payload.enabled else "disable"
    denied = _ensure_permission(current_user, action, db)
    if denied is not None:
        return denied
    return reply_service.set_goods_reply_status(
        db, current_user, reply_id=reply_id, enabled=payload.enabled
    )


@router.delete(
    "/goods-replies/{reply_id}",
    response_model=ApiResponse,
    summary="逻辑删除商品专属回复",
)
def delete_goods_reply(
    reply_id: int,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """逻辑删除商品专属回复（禁止物理删除，需求 24.6）。"""
    denied = _ensure_permission(current_user, "disable", db)
    if denied is not None:
        return denied
    return reply_service.delete_goods_reply(db, current_user, reply_id=reply_id)


__all__ = ["router"]
