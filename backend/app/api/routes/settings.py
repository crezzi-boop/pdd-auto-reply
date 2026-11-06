# -*- coding: utf-8 -*-
"""
backend.app.api.routes.settings —— 系统设置接口路由
==================================================
本文件用途：提供 backend 服务的「系统设置」REST 接口，覆盖任务 8.1（主题/分页/
基础/品牌/免责声明/二维码）与任务 8.2（SMTP 邮件/测试邮件/代理设置），
满足需求 21.1/21.6/21.7/21.8/21.9/21.11/21.12/21.13/21.14/21.15/21.17：

- ``GET  /settings``                  系统设置总览（需求 21.1）。
- ``GET/PUT /settings/theme``         主题外观（需求 21.9）。
- ``GET/PUT /settings/pagination``    分页默认值（需求 21.1）。
- ``GET/PUT /settings/basic``         基础设置（需求 21.6）。
- ``GET/PUT /settings/brand``         登录页品牌（需求 21.11）。
- ``GET/PUT /settings/disclaimer``    免责声明（需求 21.12）。
- ``GET/PUT /settings/qrcodes``       联系二维码（需求 21.13）。
- ``GET/PUT /settings/smtp``          SMTP 邮件参数（密码不返回明文，需求 21.7）。
- ``POST /settings/smtp/test``        测试邮件发送（需求 21.8）。
- ``GET/PUT /settings/proxy``         代理设置（开启时地址为空返回固定提示，需求 21.14/21.15）。

权限控制（需求 21.17）：系统设置为管理员专属。所有接口先经统一权限模块判断当前
用户是否管理员（``permission.load_auth_context(...).is_admin``）；非管理员一律返回
``success=false``、``message``「无访问权限」的统一响应体（HTTP 恒 200）。
例外：``GET /settings/brand`` 为公开接口，登录页需在登录前展示品牌文案，故读取不鉴权
（保存 ``PUT /settings/brand`` 仍为管理员专属）。

接口约定（开发规范 1-3）：
- 所有接口 HTTP 恒返回 200，业务成败由统一响应体 {code, success, message, data}
  表达。
- 业务逻辑委托 app.services.setting_service 与 app.services.smtp_proxy_service，
  路由层仅负责入参解析、依赖注入与权限判断；数据库会话经 get_db 注入。
- SMTP 查询为管理员专属接口，应用户要求反显解密后的明文密码供页面查看/编辑。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core import permission
from app.core.business_codes import CODE_FORBIDDEN, MSG_FORBIDDEN
from app.services import setting_service, smtp_proxy_service
from common.db.session import get_db
from common.models.user_models import SysUser
from common.schemas.common import ApiResponse, error_response

# 系统设置路由：标签「系统设置」便于 OpenAPI 文档分组；前缀由聚合层统一添加。
router = APIRouter(tags=["系统设置"])


def _ensure_admin(user: SysUser, session: Session) -> Optional[ApiResponse]:
    """统一权限校验：系统设置仅管理员可访问（需求 21.17）。

    经统一权限模块装配授权上下文判断是否管理员；非管理员返回「无访问权限」
    统一响应体，由调用方直接作为接口返回。

    Args:
        user: 当前登录用户。
        session: 数据库会话。

    Returns:
        非管理员返回失败响应体；管理员返回 None。
    """
    context = permission.load_auth_context(user, session)
    if context.is_admin:
        return None
    return error_response(CODE_FORBIDDEN, MSG_FORBIDDEN)


# ----------------------------------------------------------------------
# 请求体模型
# ----------------------------------------------------------------------
class ThemeRequest(BaseModel):
    """主题外观请求体（字段可选，None 表示不修改）。"""

    theme_color: Optional[str] = Field(None, description="主题色（十六进制色值）")
    dark_mode: Optional[str] = Field(None, description="明暗模式：light/dark/auto")
    font_family: Optional[str] = Field(None, description="字体族名称")


class PaginationRequest(BaseModel):
    """分页默认值请求体。"""

    default_page_size: int = Field(..., description="默认每页条数（10/20/50/100）")


class BasicRequest(BaseModel):
    """基础设置请求体（字段可选，None 表示不修改）。"""

    allow_register: Optional[bool] = Field(None, description="是否允许用户注册")
    show_default_login: Optional[bool] = Field(None, description="是否显示默认登录信息")
    enable_captcha: Optional[bool] = Field(None, description="是否启用登录验证码")
    log_retention_days: Optional[int] = Field(None, description="日志保留天数（1~365）")


class LoginMaintenanceRequest(BaseModel):
    """登录态自动维护请求体（字段可选，None 表示不修改）。"""

    enabled: Optional[bool] = Field(None, description="是否启用自动保活")
    time: Optional[str] = Field(None, description="每日维护时间 HH:MM（24 小时制）")


class BrandRequest(BaseModel):
    """登录页品牌请求体（字段可选，None 表示不修改）。"""

    system_name: Optional[str] = Field(None, description="系统名称")
    title: Optional[str] = Field(None, description="登录页标题")
    description: Optional[str] = Field(None, description="登录页描述")


class DisclaimerRequest(BaseModel):
    """免责声明请求体（字段可选，None 表示不修改）。"""

    title: Optional[str] = Field(None, description="免责声明标题")
    content: Optional[str] = Field(None, description="免责声明正文")
    checkbox_text: Optional[str] = Field(None, description="勾选文案")
    agree_text: Optional[str] = Field(None, description="同意按钮文案")
    disagree_text: Optional[str] = Field(None, description="不同意按钮文案")


class QrcodesRequest(BaseModel):
    """联系二维码请求体。"""

    items: List[Dict[str, Any]] = Field(..., description="二维码列表 [{type, image_url}]")


class SmtpRequest(BaseModel):
    """SMTP 邮件参数请求体（字段可选，None 表示不修改）。"""

    host: Optional[str] = Field(None, description="SMTP 服务器地址")
    port: Optional[int] = Field(None, description="SMTP 端口（1~65535）")
    sender_email: Optional[str] = Field(None, description="发件邮箱")
    sender_name: Optional[str] = Field(None, description="发件人显示名")
    password: Optional[str] = Field(None, description="密码/授权码（None=不改，空串=清空）")
    use_ssl: Optional[bool] = Field(None, description="是否使用 SSL")


class TestEmailRequest(BaseModel):
    """测试邮件请求体。"""

    to_email: str = Field(..., description="收件地址")
    subject: Optional[str] = Field(None, description="邮件主题")
    content: Optional[str] = Field(None, description="邮件正文")


class ProxyRequest(BaseModel):
    """代理设置请求体。"""

    enabled: bool = Field(..., description="代理开关")
    api_url: Optional[str] = Field(None, description="代理 API 地址（开启时不得为空）")


# ----------------------------------------------------------------------
# 系统设置总览（需求 21.1）
# ----------------------------------------------------------------------
@router.get("/settings", response_model=ApiResponse, summary="系统设置总览")
def get_all_settings(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询全部系统设置分组（需求 21.1）。仅管理员可访问（需求 21.17）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.get_all_settings(db)


# ----------------------------------------------------------------------
# 主题外观（需求 21.9）
# ----------------------------------------------------------------------
@router.get("/settings/theme", response_model=ApiResponse, summary="查询主题外观")
def get_theme(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询主题外观设置（需求 21.9）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.get_theme(db)


@router.put("/settings/theme", response_model=ApiResponse, summary="保存主题外观")
def update_theme(
    payload: ThemeRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """持久化主题外观设置（需求 21.9）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.update_theme(
        db,
        theme_color=payload.theme_color,
        dark_mode=payload.dark_mode,
        font_family=payload.font_family,
        operator_id=current_user.id,
    )


# ----------------------------------------------------------------------
# 分页默认值（需求 21.1）
# ----------------------------------------------------------------------
@router.get("/settings/pagination", response_model=ApiResponse, summary="查询分页默认值")
def get_pagination(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询分页默认值设置（需求 21.1）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.get_pagination(db)


@router.put("/settings/pagination", response_model=ApiResponse, summary="保存分页默认值")
def update_pagination(
    payload: PaginationRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """持久化分页默认每页条数（需求 21.1）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.update_pagination(
        db, default_page_size=payload.default_page_size, operator_id=current_user.id
    )


# ----------------------------------------------------------------------
# 基础设置（需求 21.6）
# ----------------------------------------------------------------------
@router.get("/settings/basic", response_model=ApiResponse, summary="查询基础设置")
def get_basic(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询基础设置（需求 21.6）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.get_basic(db)


@router.put("/settings/basic", response_model=ApiResponse, summary="保存基础设置")
def update_basic(
    payload: BasicRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """持久化基础设置（需求 21.6）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.update_basic(
        db,
        allow_register=payload.allow_register,
        show_default_login=payload.show_default_login,
        enable_captcha=payload.enable_captcha,
        log_retention_days=payload.log_retention_days,
        operator_id=current_user.id,
    )


# ----------------------------------------------------------------------
# 登录态自动维护（自动保活 + 每日维护时间）
# ----------------------------------------------------------------------
@router.get("/settings/login-maintenance", response_model=ApiResponse, summary="查询登录态维护设置")
def get_login_maintenance(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询登录态自动维护设置（管理员专属）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.get_login_maintenance(db)


@router.put("/settings/login-maintenance", response_model=ApiResponse, summary="保存登录态维护设置")
def update_login_maintenance(
    payload: LoginMaintenanceRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """持久化登录态自动维护设置（管理员专属）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.update_login_maintenance(
        db,
        enabled=payload.enabled,
        time=payload.time,
        operator_id=current_user.id,
    )


# ----------------------------------------------------------------------
# 登录页品牌（需求 21.11）
# ----------------------------------------------------------------------
@router.get("/settings/brand", response_model=ApiResponse, summary="查询登录页品牌")
def get_brand(
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询登录页品牌信息（需求 21.11）。

    公开接口：登录页需在「登录前」展示系统名称/标题/描述，故读取不鉴权；
    仅暴露非敏感的品牌展示文案。保存（PUT）仍为管理员专属。
    """
    return setting_service.get_brand(db)


@router.put("/settings/brand", response_model=ApiResponse, summary="保存登录页品牌")
def update_brand(
    payload: BrandRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """持久化登录页品牌信息（需求 21.11）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.update_brand(
        db,
        system_name=payload.system_name,
        title=payload.title,
        description=payload.description,
        operator_id=current_user.id,
    )


# ----------------------------------------------------------------------
# 免责声明（需求 21.12）
# ----------------------------------------------------------------------
@router.get("/settings/disclaimer", response_model=ApiResponse, summary="查询免责声明")
def get_disclaimer(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询免责声明设置（需求 21.12）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.get_disclaimer(db)


@router.put("/settings/disclaimer", response_model=ApiResponse, summary="保存免责声明")
def update_disclaimer(
    payload: DisclaimerRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """持久化免责声明（需求 21.12）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.update_disclaimer(
        db,
        title=payload.title,
        content=payload.content,
        checkbox_text=payload.checkbox_text,
        agree_text=payload.agree_text,
        disagree_text=payload.disagree_text,
        operator_id=current_user.id,
    )


# ----------------------------------------------------------------------
# 联系二维码（需求 21.13）
# ----------------------------------------------------------------------
@router.get("/settings/qrcodes", response_model=ApiResponse, summary="查询联系二维码")
def get_qrcodes(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询联系二维码设置（需求 21.13）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.get_qrcodes(db)


@router.put("/settings/qrcodes", response_model=ApiResponse, summary="保存联系二维码")
def update_qrcodes(
    payload: QrcodesRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """持久化联系二维码列表（需求 21.13）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return setting_service.update_qrcodes(
        db, items=payload.items, operator_id=current_user.id
    )


# ----------------------------------------------------------------------
# SMTP 邮件设置（需求 21.7 / 21.8）
# ----------------------------------------------------------------------
@router.get("/settings/smtp", response_model=ApiResponse, summary="查询 SMTP 设置")
def get_smtp(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询 SMTP 邮件设置（管理员专属，含反显明文密码供页面查看/编辑）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return smtp_proxy_service.get_smtp(db)


@router.put("/settings/smtp", response_model=ApiResponse, summary="保存 SMTP 设置")
def update_smtp(
    payload: SmtpRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """持久化 SMTP 邮件参数（密码可逆加密存储；管理员页面反显明文便于核对）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return smtp_proxy_service.update_smtp(
        db,
        host=payload.host,
        port=payload.port,
        sender_email=payload.sender_email,
        sender_name=payload.sender_name,
        password=payload.password,
        use_ssl=payload.use_ssl,
        operator_id=current_user.id,
    )


@router.post("/settings/smtp/test", response_model=ApiResponse, summary="发送测试邮件")
def send_test_email(
    payload: TestEmailRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """经已配置 SMTP 发送测试邮件并返回发送结果（需求 21.8）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return smtp_proxy_service.send_test_email(
        db, to_email=payload.to_email, subject=payload.subject, content=payload.content
    )


# ----------------------------------------------------------------------
# 代理设置（需求 21.14 / 21.15）
# ----------------------------------------------------------------------
@router.get("/settings/proxy", response_model=ApiResponse, summary="查询代理设置")
def get_proxy(
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """查询代理设置（需求 21.14 配套）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return smtp_proxy_service.get_proxy(db)


@router.put("/settings/proxy", response_model=ApiResponse, summary="保存代理设置")
def update_proxy(
    payload: ProxyRequest,
    current_user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """持久化代理设置；开启但地址为空返回固定中文提示（需求 21.14）。"""
    denied = _ensure_admin(current_user, db)
    if denied is not None:
        return denied
    return smtp_proxy_service.update_proxy(
        db, enabled=payload.enabled, api_url=payload.api_url, operator_id=current_user.id
    )


__all__ = ["router"]
