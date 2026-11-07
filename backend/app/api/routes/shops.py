# -*- coding: utf-8 -*-
"""
backend.app.api.routes.shops —— 拼多多店铺与账号管理接口路由
============================================================
本文件用途：提供 backend 服务的「拼多多账号与店铺管理」REST 接口，满足需求 3
（拼多多账号与店铺管理）：

- ``POST   /shops``                新增 / 更新店铺（upsert 幂等，需求 3.1 / 3.2）。
- ``GET    /shops``                店铺列表（北京时间倒序、后端分页、数据范围
  隔离，需求 3.3 / 3.7）。
- ``GET    /shops/{shop_pk}``      查询单个店铺详情（脱敏、数据范围隔离）。
- ``PUT    /shops/{shop_pk}``      修改备注 / 启用状态 / 关联配置（需求 3.4）。
- ``PUT    /shops/{shop_pk}/disable`` 停用店铺并断开连接（逻辑删除，需求 3.5）。

权限控制（需求 2.4）：所有接口先经统一权限模块 ``permission.check`` 判断当前用户
对资源 ``shop`` 的对应操作是否被授权；未授权返回 success=false、message「无访问
权限」的统一响应体（HTTP 恒 200）。数据范围隔离（需求 3.7）在 service 层依据当前
用户身份进一步约束「仅可见本人 / 被授权店铺」。

接口约定（开发规范 1-3）：
- 所有接口 HTTP 恒返回 200，业务成败由统一响应体 {code, success, message, data}
  表达。
- 业务逻辑委托 app.services.account_service，路由层仅负责入参解析、鉴权依赖与
  权限判断；数据库会话经 common.db.session.get_db 注入。
- 列表返回的店铺信息均经脱敏不含凭据；单店详情应用户要求反显账号 / 密码 /
  Cookie 明文，供店铺管理「编辑」回显与修改（需求 3.6 配套）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core import permission
from app.core.business_codes import CODE_FORBIDDEN, MSG_FORBIDDEN
from app.services import account_service
from common.db.session import get_db
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response, success_response
from common.services import service_client

logger = logging.getLogger("backend.api.routes.shops")

# 店铺与账号管理路由：标签「店铺与账号」便于 OpenAPI 文档分组；前缀由聚合层添加。
router = APIRouter(tags=["店铺与账号"])

# 受保护资源键约定（与 sys_permission.resource_key 对齐）。
RESOURCE_SHOP: str = "shop"


def _ensure_permission(
    user: SysUser,
    action: str,
    session: Session,
) -> Optional[ApiResponse]:
    """统一权限校验：未授权时返回「无访问权限」响应，授权时返回 None。

    依据需求 2.4，集中经 ``permission.check`` 判断当前用户对资源 ``shop`` 的指定
    操作是否被授权；未授权返回统一失败响应体，由调用方直接作为接口返回。

    Args:
        user: 当前登录用户。
        action: 操作（view / create / update / disable）。
        session: 数据库会话。

    Returns:
        未授权时返回失败响应体；已授权返回 None。
    """
    if permission.check(user, RESOURCE_SHOP, action, session=session):
        return None
    return error_response(CODE_FORBIDDEN, MSG_FORBIDDEN)


# ----------------------------------------------------------------------
# 请求体模型
# ----------------------------------------------------------------------
class UpsertShopRequest(BaseModel):
    """新增 / 更新店铺请求体（upsert 幂等，需求 3.1 / 3.2）。"""

    shop_id: str = Field(..., description="拼多多店铺业务标识（业务键）")
    shop_name: Optional[str] = Field(None, description="店铺名称")
    shop_logo: Optional[str] = Field(None, description="店铺 Logo URL")
    channel_id: Optional[int] = Field(None, description="所属渠道 ID")
    remark: Optional[str] = Field(None, description="备注")
    cookies: Optional[str] = Field(None, description="登录 Cookie 明文（加密存储）")
    username: Optional[str] = Field(None, description="拼多多登录账号")
    password: Optional[str] = Field(None, description="账号密码明文（加密存储）")


class UpdateShopRequest(BaseModel):
    """修改店铺请求体（备注 / 启用状态 / 关联配置 / 账号凭据，需求 3.4 / 3.6）。"""

    remark: Optional[str] = Field(None, description="新备注")
    shop_name: Optional[str] = Field(None, description="新店铺名称")
    shop_logo: Optional[str] = Field(None, description="新 Logo URL")
    channel_id: Optional[int] = Field(None, description="新渠道 ID")
    enabled: Optional[bool] = Field(
        None, description="启用状态：True=启用，False=停用（停用将断连）"
    )
    username: Optional[str] = Field(None, description="新登录账号（反显后可编辑）")
    cookies: Optional[str] = Field(None, description="新 Cookie 明文（反显后可编辑，加密存储）")
    password: Optional[str] = Field(None, description="新账号密码明文（反显后可编辑，加密存储）")


class PasswordLoginShopRequest(BaseModel):
    """账号密码登录新增店铺请求体（需求 4.1 / 4.2）。

    用户仅填账号密码与可选备注，shop_id / shop_name / shop_logo 由登录后从
    拼多多接口自动获取，无需手填。
    """

    username: str = Field(..., description="拼多多商家后台登录账号")
    password: str = Field(..., description="拼多多商家后台登录密码")
    remark: Optional[str] = Field(None, description="备注")


class CookieImportShopRequest(BaseModel):
    """Cookie 导入新增店铺请求体（需求 4.3 / 4.4）。

    用户仅粘贴 Cookie 文本与可选备注，店铺信息由校验后自动获取，无需手填 shop_id。
    """

    cookies: str = Field(..., description="用户粘贴的 Cookie 文本")
    remark: Optional[str] = Field(None, description="备注")


# ----------------------------------------------------------------------
# 店铺接口
# ----------------------------------------------------------------------
@router.post("/shops", response_model=ApiResponse, summary="新增 / 更新店铺（upsert 幂等）")
def upsert_shop(
    payload: UpsertShopRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """新增或更新店铺（需求 3.1 / 3.2 / 3.6）。

    按业务键（当前用户 + shop_id）upsert 幂等；Cookie / 密码加密存储，响应脱敏。
    店铺归属用户固定为当前登录用户，保证数据范围隔离正确（需求 3.7）。
    """
    denied = _ensure_permission(current_user, "create", db)
    if denied is not None:
        return denied
    return account_service.upsert_shop(
        db,
        shop_id=payload.shop_id,
        owner_user_id=current_user.id,
        shop_name=payload.shop_name,
        shop_logo=payload.shop_logo,
        channel_id=payload.channel_id,
        remark=payload.remark,
        cookies=payload.cookies,
        username=payload.username,
        password=payload.password,
        operator_id=current_user.id,
    )


@router.post(
    "/shops/login-by-password",
    response_model=ApiResponse,
    summary="账号密码登录新增店铺（自动获取店铺信息）",
)
def login_shop_by_password(
    payload: PasswordLoginShopRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """账号密码登录并自动获取店铺信息后新增店铺（需求 4.1 / 4.2）。

    用户仅填账号密码：后端经 websocket 服务用 Playwright 登录拼多多，登录成功后
    自动获取真实 shop_id / shop_name / shop_logo 并落库，无需手填店铺标识。
    Cookie 与密码加密存储，响应脱敏（需求 3.6）。
    """
    denied = _ensure_permission(current_user, "create", db)
    if denied is not None:
        return denied
    return account_service.login_shop_by_password(
        db,
        username=payload.username,
        password=payload.password,
        owner_user_id=current_user.id,
        remark=payload.remark,
        operator_id=current_user.id,
    )


@router.post(
    "/shops/import-by-cookie",
    response_model=ApiResponse,
    summary="Cookie 导入新增店铺（自动获取店铺信息）",
)
def import_shop_by_cookie(
    payload: CookieImportShopRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """校验 Cookie 并自动获取店铺信息后新增店铺（需求 4.3 / 4.4）。

    用户仅粘贴 Cookie 文本：后端经 websocket 服务校验有效性并自动获取真实店铺
    信息后落库，无需手填店铺标识。Cookie 加密存储，响应脱敏（需求 3.6）。
    """
    denied = _ensure_permission(current_user, "create", db)
    if denied is not None:
        return denied
    return account_service.import_shop_by_cookie(
        db,
        cookies=payload.cookies,
        owner_user_id=current_user.id,
        remark=payload.remark,
        operator_id=current_user.id,
    )


@router.get("/shops", response_model=ApiResponse, summary="店铺列表（北京时间倒序、后端分页）")
def list_shops(
    page: int = Query(1, description="页码（从 1 开始）"),
    page_size: int = Query(20, description="每页条数（10/20/50/100）"),
    status: Optional[int] = Query(None, description="按状态筛选：1=启用，0=停用"),
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """分页查询店铺列表（需求 3.3 / 3.6 / 3.7）。

    按北京时间倒序、后端分页返回当前用户有权查看的店铺；非管理员仅见本人 / 被
    授权店铺；列表脱敏不返回 Cookie 明文。
    """
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return account_service.list_shops(
        db,
        current_user=current_user,
        page=page,
        page_size=page_size,
        status=status,
    )


@router.get("/shops/{shop_pk}", response_model=ApiResponse, summary="查询单个店铺详情")
def get_shop(
    shop_pk: int,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询单个店铺详情（含反显账号 / 密码 / Cookie，受数据范围隔离约束，需求 3.6 / 3.7）。

    详情应用户要求反显凭据明文供「编辑」回显与修改；前端以隐藏查看（密码框 +
    显隐切换）展示。受 ``shop:view`` 权限与数据范围隔离双重约束。
    """
    denied = _ensure_permission(current_user, "view", db)
    if denied is not None:
        return denied
    return account_service.get_shop(db, shop_pk, current_user=current_user)


@router.put("/shops/{shop_pk}", response_model=ApiResponse, summary="修改店铺")
def update_shop(
    shop_pk: int,
    payload: UpdateShopRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """修改店铺备注 / 启用状态 / 关联配置（需求 3.4）。

    停用（enabled=False）将触发逻辑删除与断连（需求 3.5）；受数据范围隔离约束。
    """
    denied = _ensure_permission(current_user, "update", db)
    if denied is not None:
        return denied
    return account_service.update_shop(
        db,
        shop_pk,
        current_user=current_user,
        remark=payload.remark,
        shop_name=payload.shop_name,
        shop_logo=payload.shop_logo,
        channel_id=payload.channel_id,
        enabled=payload.enabled,
        username=payload.username,
        cookies=payload.cookies,
        password=payload.password,
    )


@router.put(
    "/shops/{shop_pk}/disable",
    response_model=ApiResponse,
    summary="停用店铺并断开连接",
)
def disable_shop(
    shop_pk: int,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """停用店铺并断开其拼多多连接（需求 3.5）。

    经状态字段逻辑删除（禁止物理删除），并经 HTTP 通知 websocket 服务断开连接；
    受数据范围隔离约束（需求 3.7）。
    """
    denied = _ensure_permission(current_user, "disable", db)
    if denied is not None:
        return denied
    return account_service.disable_shop(db, shop_pk, current_user=current_user)



# ----------------------------------------------------------------------
# 扫码登录自助会话（系统内置，无需人工发送二维码）
# ----------------------------------------------------------------------
_QR_START_PATH: str = "/api/v1/qr-login/start"
_QR_STATUS_PATH: str = "/api/v1/qr-login/status"
_QR_CANCEL_PATH: str = "/api/v1/qr-login/cancel"
# 已完成建店的会话（避免轮询重复落库）与正在建店的会话（避免并发重复落库）。
_qr_imported_sessions: set[str] = set()
_qr_importing_sessions: set[str] = set()


class QrSessionRequest(BaseModel):
    """扫码会话请求体。"""

    session_id: str = Field(..., description="扫码会话 ID")


@router.post("/qr-login/start", response_model=ApiResponse, summary="生成拼多多扫码登录二维码")
def start_qr_login(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """启动 websocket 侧扫码会话，把二维码图片回传给前端展示。"""
    denied = _ensure_permission(current_user, "create", db)
    if denied is not None:
        return denied
    try:
        resp = service_client.post_json(
            service_client.websocket_base_url(), _QR_START_PATH, {}, timeout=40.0
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动扫码会话异常：%s", exc)
        return error_response(-1, "二维码生成失败，请稍后重试")
    if not resp.ok or not resp.success:
        return error_response(-1, resp.message or "二维码生成失败，请稍后重试")
    data = resp.body.get("data") or {}
    return success_response(
        data={
            "session_id": data.get("session_id", ""),
            "qr_image": data.get("qr_image", ""),
            "expires_in": data.get("expires_in", 900),
        },
        message="二维码已生成",
    )


@router.get("/qr-login/status", response_model=ApiResponse, summary="查询扫码登录状态并自动建店")
def get_qr_login_status(
    session_id: str = Query(..., description="扫码会话 ID"),
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """轮询扫码状态；登录成功时用 Cookie 自动新增 / 更新店铺。

    Cookie 仅在 backend 与 websocket 之间传输，不下发浏览器；同一会话只落库一次。
    """
    denied = _ensure_permission(current_user, "create", db)
    if denied is not None:
        return denied
    try:
        resp = service_client.get_json(
            service_client.websocket_base_url(),
            _QR_STATUS_PATH,
            {"session_id": session_id},
            timeout=20.0,
        )
    except Exception as exc:  # noqa: BLE001
        return error_response(-1, "查询扫码状态失败：%s" % str(exc)[:100])
    if not resp.ok or not resp.success:
        return error_response(-1, resp.message or "会话不存在或已过期，请重新获取二维码")
    data = resp.body.get("data") or {}
    state = data.get("state") or "waiting"
    out = {
        "state": state,
        "qr_image": data.get("qr_image", ""),
        "message": data.get("message", ""),
    }
    if state == "success" and session_id not in _qr_imported_sessions:
        if session_id in _qr_importing_sessions:
            out.update({"state": "importing", "message": "登录成功，正在添加店铺…"})
            return success_response(data=out)
        _qr_importing_sessions.add(session_id)
        try:
            result = account_service.import_shop_by_cookie(
                db,
                cookies=data.get("cookies") or "",
                owner_user_id=current_user.id,
                remark="扫码登录",
                operator_id=current_user.id,
            )
            if result.success:
                _qr_imported_sessions.add(session_id)
                out.update({"state": "success", "message": "登录成功，店铺已自动添加"})
            else:
                out.update(
                    {
                        "state": "failed",
                        "message": "登录成功但店铺保存失败：%s" % (result.message or "请重试"),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("扫码登录后自动建店异常：%s", exc)
            out.update({"state": "failed", "message": "店铺保存异常：%s" % str(exc)[:100]})
        finally:
            _qr_importing_sessions.discard(session_id)
    return success_response(data=out)


@router.post("/qr-login/cancel", response_model=ApiResponse, summary="取消扫码登录会话")
def cancel_qr_login(
    payload: QrSessionRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """关闭弹窗时取消会话，及时释放服务器浏览器资源。"""
    denied = _ensure_permission(current_user, "create", db)
    if denied is not None:
        return denied
    try:
        service_client.post_json(
            service_client.websocket_base_url(),
            _QR_CANCEL_PATH,
            {"session_id": payload.session_id},
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("取消扫码会话失败：%s", exc)
    return success_response(message="已取消")


__all__ = ["router"]
