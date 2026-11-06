# -*- coding: utf-8 -*-
"""
backend.app.services.setting_service —— 系统设置业务服务
========================================================
本文件用途：实现 backend 服务的「系统设置」业务逻辑（任务 8.1），供 settings
路由复用，覆盖需求 21.1 / 21.6 / 21.9 / 21.10 / 21.11 / 21.12 / 21.13：

- 主题外观（主题色、明暗模式、字体）—— 需求 21.9；
- 分页默认值（默认每页条数，可选 10/20/50/100）—— 需求 21.1；
- 基础设置（允许注册、显示默认登录信息、启用登录验证码、日志保留天数
  1~365 天）—— 需求 21.6；
- 登录页品牌（系统名称、标题、描述）—— 需求 21.11；
- 免责声明（标题、正文、勾选文案、同意 / 不同意按钮文案）—— 需求 21.12；
- 联系二维码（微信、QQ、公众号、Telegram 等类型的图片地址）—— 需求 21.13。

存储模型说明（common.models.setting_models.SysSetting）：
- 以键值对存储系统级设置：``setting_key`` 为「设置分组键」（如 theme /
  pagination / basic / brand / disclaimer / qrcodes），
  ``setting_value`` 为该分组的 JSON 文本，``scope='global'``，``owner_user_id``
  为空（系统级，非用户维度）。
- 同一分组键按 (setting_key, scope, owner_user_id) 作为业务键 upsert：同一系统
  级设置分组恒为 1 条，重复保存覆盖更新（幂等，需求 21.1 持久化并生效）。

实现约束（开发规范）：
- 统一响应体由 common.schemas.common 构造，HTTP 恒 200（规范 1-3 / 需求 24.1）。
- 所有数据访问经 common.db.repository 的参数化查询，禁止拼接 SQL（规范 16）。
- 禁止物理删除业务数据；设置变更经 upsert 覆盖更新（规范 11 / 需求 24.6）。
- 导入置顶（规范 51）；中文注释（规范 37）；单文件 ≤500 行（规范 35）。
- 权限与「非管理员拒绝访问」由路由层经 permission 模块统一拦截（需求 21.17）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.business_codes import CODE_PARAM_ERROR
from app.services import setting_store
from common.schemas.common import ApiResponse, error_response, success_response

# 系统级设置的统一作用域标识（与 SysSetting.scope 约定一致）。
_SCOPE_GLOBAL: str = setting_store.SCOPE_GLOBAL

# 分页可选每页条数（规范 28 / 需求 21.1）。
_ALLOWED_PAGE_SIZES: tuple[int, ...] = (10, 20, 50, 100)

# 日志保留天数允许区间（含端点，需求 21.6）。
_LOG_RETENTION_MIN: int = 1
_LOG_RETENTION_MAX: int = 365

# 设置分组键常量（setting_key 取值，集中管理避免散落）。
KEY_THEME: str = "theme"
KEY_PAGINATION: str = "pagination"
KEY_BASIC: str = "basic"
KEY_BRAND: str = "brand"
KEY_DISCLAIMER: str = "disclaimer"
KEY_QRCODES: str = "qrcodes"
# 登录态自动维护（自动保活开关 + 每日维护时间）。
KEY_LOGIN_MAINTENANCE: str = "login_maintenance"

# 各分组的默认值：未配置时 GET 返回该默认结构，保证前端可直接渲染。
_DEFAULTS: Dict[str, Dict[str, Any]] = {
    KEY_THEME: {
        # 主题色（十六进制等前端可识别的色值字符串）。
        "theme_color": "#1677ff",
        # 明暗模式：light / dark / auto。
        "dark_mode": "light",
        # 字体族名称。
        "font_family": "default",
    },
    KEY_PAGINATION: {
        # 默认每页条数（须为 _ALLOWED_PAGE_SIZES 之一）。
        "default_page_size": 20,
    },
    KEY_BASIC: {
        # 是否允许用户注册。
        "allow_register": False,
        # 登录页是否显示默认登录信息。
        "show_default_login": False,
        # 是否启用登录验证码。
        "enable_captcha": False,
        # 文件日志保留天数（1~365）。
        "log_retention_days": 30,
    },
    KEY_BRAND: {
        # 系统名称。
        "system_name": "拼多多自动回复",
        # 登录页标题。
        "title": "拼多多自动回复管理系统",
        # 登录页描述。
        "description": "",
    },
    KEY_DISCLAIMER: {
        # 免责声明标题。
        "title": "免责声明",
        # 免责声明正文。
        "content": "",
        # 勾选文案。
        "checkbox_text": "我已阅读并同意上述免责声明",
        # 同意按钮文案。
        "agree_text": "同意",
        # 不同意按钮文案。
        "disagree_text": "不同意",
    },
    KEY_QRCODES: {
        # 二维码列表：每项 {type, image_url}，type 如 wechat/qq/mp/telegram。
        "items": [],
    },
    KEY_LOGIN_MAINTENANCE: {
        # 是否启用自动保活（每 10 分钟检查一次登录态）。
        "enabled": True,
        # 每天额外做一次完整维护的时间（北京时间 HH:MM）。
        "time": "08:00",
        # 最近一次每日维护日期（内部使用，避免同一天重复执行）。
        "last_run_date": "",
    },
}


def _save_value(
    session: Session,
    key: str,
    value: Dict[str, Any],
    operator_id: Optional[int],
) -> None:
    """按分组键 upsert 系统级设置值（委托公共存储层，幂等覆盖更新）。

    Args:
        session: 数据库会话。
        key: 设置分组键。
        value: 待持久化的设置字典（将序列化为 JSON 文本）。
        operator_id: 操作人用户 ID（仅新建时作为创建人审计字段）。
    """
    # 复用 setting_store 的统一 upsert，避免重复实现（规范 36）。
    setting_store.save_group(session, key, value, operator_id)


def _get_group(session: Session, key: str) -> Dict[str, Any]:
    """读取单个分组设置（合并默认值，委托公共存储层）。"""
    # 复用 setting_store 的统一读取与默认值合并逻辑（规范 36）。
    return setting_store.get_group(session, key, _DEFAULTS.get(key, {}))


# ----------------------------------------------------------------------
# 主题外观（需求 21.9）
# ----------------------------------------------------------------------
def get_theme(session: Session) -> ApiResponse:
    """查询主题外观设置（需求 21.9）。"""
    return success_response(data=_get_group(session, KEY_THEME), message="查询成功")


def update_theme(
    session: Session,
    theme_color: Optional[str] = None,
    dark_mode: Optional[str] = None,
    font_family: Optional[str] = None,
    *,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """持久化主题外观设置（主题色、明暗模式、字体）（需求 21.9）。"""
    value: Dict[str, Any] = dict(_DEFAULTS[KEY_THEME])
    # 仅覆盖显式提供的字段，未提供保持默认 / 历史值。
    current = _get_group(session, KEY_THEME)
    value.update(current)
    if theme_color is not None:
        value["theme_color"] = theme_color
    if dark_mode is not None:
        # 明暗模式仅允许 light / dark / auto。
        if dark_mode not in ("light", "dark", "auto"):
            return error_response(CODE_PARAM_ERROR, "明暗模式仅支持 light/dark/auto")
        value["dark_mode"] = dark_mode
    if font_family is not None:
        value["font_family"] = font_family
    _save_value(session, KEY_THEME, value, operator_id)
    return success_response(data=value, message="主题设置已保存")


# ----------------------------------------------------------------------
# 分页默认值（需求 21.1）
# ----------------------------------------------------------------------
def get_pagination(session: Session) -> ApiResponse:
    """查询分页默认值设置（需求 21.1）。"""
    return success_response(
        data=_get_group(session, KEY_PAGINATION), message="查询成功"
    )


def update_pagination(
    session: Session,
    default_page_size: int,
    *,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """持久化分页默认每页条数（须为 10/20/50/100 之一）（需求 21.1）。"""
    if default_page_size not in _ALLOWED_PAGE_SIZES:
        return error_response(
            CODE_PARAM_ERROR, "默认每页条数仅支持 10/20/50/100"
        )
    value = {"default_page_size": int(default_page_size)}
    _save_value(session, KEY_PAGINATION, value, operator_id)
    return success_response(data=value, message="分页设置已保存")


# ----------------------------------------------------------------------
# 基础设置（需求 21.6）
# ----------------------------------------------------------------------
def get_basic(session: Session) -> ApiResponse:
    """查询基础设置（需求 21.6）。"""
    return success_response(data=_get_group(session, KEY_BASIC), message="查询成功")


def update_basic(
    session: Session,
    allow_register: Optional[bool] = None,
    show_default_login: Optional[bool] = None,
    enable_captcha: Optional[bool] = None,
    log_retention_days: Optional[int] = None,
    *,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """持久化基础设置（注册开关 / 默认登录信息 / 验证码 / 日志保留天数）。

    日志保留天数须为 1~365 的整数（需求 21.6），否则返回中文提示。
    """
    value = dict(_DEFAULTS[KEY_BASIC])
    value.update(_get_group(session, KEY_BASIC))
    if allow_register is not None:
        value["allow_register"] = bool(allow_register)
    if show_default_login is not None:
        value["show_default_login"] = bool(show_default_login)
    if enable_captcha is not None:
        value["enable_captcha"] = bool(enable_captcha)
    if log_retention_days is not None:
        if (
            not isinstance(log_retention_days, int)
            or isinstance(log_retention_days, bool)
            or log_retention_days < _LOG_RETENTION_MIN
            or log_retention_days > _LOG_RETENTION_MAX
        ):
            return error_response(
                CODE_PARAM_ERROR, "日志保留天数须为 1~365 之间的整数"
            )
        value["log_retention_days"] = int(log_retention_days)
    _save_value(session, KEY_BASIC, value, operator_id)
    return success_response(data=value, message="基础设置已保存")


# ----------------------------------------------------------------------
# 登录页品牌（需求 21.11）
# ----------------------------------------------------------------------
def get_brand(session: Session) -> ApiResponse:
    """查询登录页品牌信息（需求 21.11）。"""
    return success_response(data=_get_group(session, KEY_BRAND), message="查询成功")


def update_brand(
    session: Session,
    system_name: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    *,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """持久化登录页品牌信息（系统名称、标题、描述）（需求 21.11）。"""
    value = dict(_DEFAULTS[KEY_BRAND])
    value.update(_get_group(session, KEY_BRAND))
    if system_name is not None:
        value["system_name"] = system_name
    if title is not None:
        value["title"] = title
    if description is not None:
        value["description"] = description
    _save_value(session, KEY_BRAND, value, operator_id)
    return success_response(data=value, message="品牌信息已保存")


# ----------------------------------------------------------------------
# 免责声明（需求 21.12）
# ----------------------------------------------------------------------
def get_disclaimer(session: Session) -> ApiResponse:
    """查询免责声明设置（需求 21.12）。"""
    return success_response(
        data=_get_group(session, KEY_DISCLAIMER), message="查询成功"
    )


def update_disclaimer(
    session: Session,
    title: Optional[str] = None,
    content: Optional[str] = None,
    checkbox_text: Optional[str] = None,
    agree_text: Optional[str] = None,
    disagree_text: Optional[str] = None,
    *,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """持久化免责声明（标题、正文、勾选文案、同意/不同意按钮文案）（需求 21.12）。"""
    value = dict(_DEFAULTS[KEY_DISCLAIMER])
    value.update(_get_group(session, KEY_DISCLAIMER))
    if title is not None:
        value["title"] = title
    if content is not None:
        value["content"] = content
    if checkbox_text is not None:
        value["checkbox_text"] = checkbox_text
    if agree_text is not None:
        value["agree_text"] = agree_text
    if disagree_text is not None:
        value["disagree_text"] = disagree_text
    _save_value(session, KEY_DISCLAIMER, value, operator_id)
    return success_response(data=value, message="免责声明已保存")


# ----------------------------------------------------------------------
# 联系二维码（需求 21.13）
# ----------------------------------------------------------------------
def get_qrcodes(session: Session) -> ApiResponse:
    """查询联系二维码设置（需求 21.13）。"""
    return success_response(data=_get_group(session, KEY_QRCODES), message="查询成功")


def update_qrcodes(
    session: Session,
    items: List[Dict[str, Any]],
    *,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """持久化联系二维码列表（微信/QQ/公众号/Telegram 等）（需求 21.13）。

    每项需包含 ``type``（二维码类型）与 ``image_url``（图片地址）。
    """
    if not isinstance(items, list):
        return error_response(CODE_PARAM_ERROR, "二维码列表格式无效")
    normalized: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return error_response(CODE_PARAM_ERROR, "二维码项格式无效")
        qr_type = item.get("type")
        image_url = item.get("image_url")
        if not qr_type or not image_url:
            return error_response(
                CODE_PARAM_ERROR, "二维码项须包含类型与图片地址"
            )
        normalized.append({"type": str(qr_type), "image_url": str(image_url)})
    value = {"items": normalized}
    _save_value(session, KEY_QRCODES, value, operator_id)
    return success_response(data=value, message="二维码已保存")


def is_login_captcha_enabled(session: Session) -> bool:
    """读取「是否启用登录滑块验证码」开关（供登录与公开开关接口复用）。

    复用基础设置分组中的 ``enable_captcha`` 字段（需求 21.6），登录前的公开
    查询与登录校验均以此为准，确保前后端开关口径一致。

    Args:
        session: 数据库会话。

    Returns:
        启用返回 True，否则 False（无配置时按默认值 False）。
    """
    basic = _get_group(session, KEY_BASIC)
    return bool(basic.get("enable_captcha", False))


def is_register_allowed(session: Session) -> bool:
    """读取「是否允许用户注册」开关（供注册校验与公开注册状态接口复用）。

    复用基础设置分组中的 ``allow_register`` 字段（需求 21.6），注册页登录前的
    公开查询与后端注册校验均以此为准，确保前后端开关口径一致；关闭时后端拒绝
    注册、前端展示「注册功能已关闭」。

    Args:
        session: 数据库会话。

    Returns:
        允许注册返回 True，否则 False（无配置时按默认值 False）。
    """
    basic = _get_group(session, KEY_BASIC)
    return bool(basic.get("allow_register", False))


# ----------------------------------------------------------------------
# 汇总查询（系统设置总览，需求 21.1）
# ----------------------------------------------------------------------
def get_all_settings(session: Session) -> ApiResponse:
    """查询全部系统设置分组（合并默认值），便于前端一次性加载并生效。"""
    data = {
        KEY_THEME: _get_group(session, KEY_THEME),
        KEY_PAGINATION: _get_group(session, KEY_PAGINATION),
        KEY_BASIC: _get_group(session, KEY_BASIC),
        KEY_BRAND: _get_group(session, KEY_BRAND),
        KEY_DISCLAIMER: _get_group(session, KEY_DISCLAIMER),
        KEY_QRCODES: _get_group(session, KEY_QRCODES),
    }
    return success_response(data=data, message="查询成功")


__all__ = [
    "KEY_THEME",
    "KEY_PAGINATION",
    "KEY_BASIC",
    "KEY_BRAND",
    "KEY_DISCLAIMER",
    "KEY_QRCODES",
    "get_theme",
    "update_theme",
    "get_pagination",
    "update_pagination",
    "get_basic",
    "update_basic",
    "get_brand",
    "update_brand",
    "get_disclaimer",
    "update_disclaimer",
    "get_qrcodes",
    "update_qrcodes",
    "is_login_captcha_enabled",
    "get_all_settings",
]


# ----------------------------------------------------------------------
# 登录态自动维护（自动保活 + 每日维护时间）
# ----------------------------------------------------------------------
def get_login_maintenance(session: Session) -> ApiResponse:
    """查询登录态自动维护设置。"""
    return success_response(
        data=_get_group(session, KEY_LOGIN_MAINTENANCE), message="查询成功"
    )


def update_login_maintenance(
    session: Session,
    enabled: Optional[bool] = None,
    time: Optional[str] = None,
    *,
    operator_id: Optional[int] = None,
) -> ApiResponse:
    """持久化登录态自动维护设置（时间须为 24 小时制 HH:MM）。"""
    value: Dict[str, Any] = dict(_DEFAULTS[KEY_LOGIN_MAINTENANCE])
    value.update(_get_group(session, KEY_LOGIN_MAINTENANCE))
    if enabled is not None:
        value["enabled"] = bool(enabled)
    if time is not None:
        text = str(time).strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
            return error_response(CODE_PARAM_ERROR, "时间格式须为 HH:MM（24 小时制）")
        value["time"] = text
    _save_value(session, KEY_LOGIN_MAINTENANCE, value, operator_id)
    return success_response(data=value, message="登录态维护设置已保存")
