# -*- coding: utf-8 -*-
"""
websocket.routes.cookies —— Cookie 刷新接口（供 scheduler 调用）
==============================================================
本文件用途：提供 websocket 服务的「Cookie 刷新」HTTP 接口，供 scheduler 定时任务
服务经服务间 HTTP 调用，定时刷新指定店铺账号的登录态 Cookie（需求 4.6 / 21.2）。

- ``POST /cookies/refresh``：按 (shop_id, owner_user_id) 定位账号名后，复用
  ``channel_pdd.pdd_login.refresh_pdd_cookies`` 无头刷新 Cookie 并维护登录态；
  刷新成功后将新 Cookie 以可逆加密回写 account 表（不返回明文，需求 3.6）。

接口约定（开发规范 1-3）：HTTP 恒返回 200，业务成败由统一响应体
``{code, success, message, data}`` 表达。地址经环境变量配置（禁止写死 localhost，
规范 21）。本路由不抛异常，失败统一规整为失败响应（健壮性兜底，需求 26）；
对外响应不包含 Cookie 等敏感字段（需求 3.6）。

实现约束（开发规范）：导入置顶（规范 51）、中文注释（规范 37）、文件名用下划线
（规范 40）、单文件 ≤500 行（规范 35）、日志禁用 debug（规范 38）、复用共通（规范 36/52）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from channel_pdd import pdd_login
from channel_pdd.api.get_user_info import GetUserInfo
from channel_pdd.core.credential_store import (
    load_account_cookies,
    load_account_credentials,
    update_account_cookies,
)
from channel_pdd.core.login_state_store import (
    LOGIN_STATE_ONLINE,
    LOGIN_STATE_RELOGIN,
    mark_login_state,
)
from common.schemas.common import ApiResponse, error_response, success_response

logger = logging.getLogger("websocket.routes.cookies")

# Cookie 刷新路由：标签便于 OpenAPI 分组；前缀由聚合层添加。
router = APIRouter(tags=["Cookie 刷新"])


def _verify_cookies_alive(cookies: dict, shop_id: str, user_id: int) -> bool:
    """用已存 Cookie 请求拼多多用户信息接口，验证登录态并顺带保活。

    扫码 / Cookie 导入的店铺没有 Playwright 用户数据目录，无法走浏览器刷新；
    此时用库中 Cookie 直接发一次请求，既验证有效性，也让会话保持活跃。

    Args:
        cookies: 已解密的 Cookie 字典。
        shop_id: 店铺业务标识。
        user_id: 归属用户 ID。

    Returns:
        Cookie 仍有效返回 True（并回写凭据、标记在线）；否则 False。
    """
    try:
        user_info = GetUserInfo(cookies).get_user_info()
    except Exception as exc:  # noqa: BLE001 - 校验异常一律视为失效
        logger.warning("店铺 shop_id=%s Cookie 校验异常：%s", shop_id, exc)
        return False
    if not user_info or not user_info[0]:
        return False
    update_account_cookies(shop_id, user_id, cookies)
    mark_login_state(shop_id, user_id, LOGIN_STATE_ONLINE)
    return True


def _safe_mark_relogin(shop_id: str, user_id: int) -> None:
    """标记店铺为「需重新登录」，失败只记日志不打断响应。"""
    try:
        mark_login_state(shop_id, user_id, LOGIN_STATE_RELOGIN)
    except Exception as exc:  # noqa: BLE001
        logger.warning("店铺 shop_id=%s 标记需重新登录失败：%s", shop_id, exc)


class RefreshCookieRequest(BaseModel):
    """Cookie 刷新请求体（与 scheduler service_client 约定一致）。"""

    shop_pk: int = Field(..., description="店铺主键 shop.id")
    shop_id: str = Field(..., description="拼多多店铺业务标识")
    owner_user_id: Optional[int] = Field(None, description="店铺归属用户 ID")


@router.post(
    "/cookies/refresh",
    response_model=ApiResponse,
    summary="刷新指定店铺账号的登录态 Cookie",
)
async def refresh_cookie(payload: RefreshCookieRequest) -> ApiResponse:
    """刷新指定店铺账号的登录态 Cookie（需求 4.6 / 21.2）。

    先按 (shop_id, owner_user_id) 定位账号名，再复用 ``refresh_pdd_cookies`` 无头
    刷新 Cookie 并维护登录态；刷新成功后将新 Cookie 加密回写 account 表。无论刷新
    成败本路由都不抛异常：成功返回 success；登录态失效 / 失败返回 error_response。
    对外响应不包含 Cookie 明文（需求 3.6）。

    Args:
        payload: 含 shop_pk / shop_id / owner_user_id 的刷新请求体。

    Returns:
        统一响应体：刷新成功返回 success；失败返回 error_response。
    """
    # owner_user_id 缺失无法定位账号凭据，直接返回失败（不抛异常）。
    if payload.owner_user_id is None:
        logger.warning("Cookie 刷新缺少归属用户 ID：shop_id=%s", payload.shop_id)
        return error_response(-1, "缺少归属用户信息，无法刷新 Cookie")

    # 按 (shop_id, owner_user_id) 定位账号名（复用共通凭据适配层）。
    credentials = load_account_credentials(payload.shop_id, payload.owner_user_id)
    if not credentials or not credentials[0]:
        logger.warning(
            "Cookie 刷新未找到账号凭据：shop_id=%s, user_id=%s",
            payload.shop_id,
            payload.owner_user_id,
        )
        return error_response(-1, "未找到账号凭据，无法刷新 Cookie")

    username = credentials[0]
    try:
        info = await pdd_login.refresh_pdd_cookies(username, payload.owner_user_id)
    except Exception as exc:  # noqa: BLE001 - 刷新异常不抛出，规整为失败响应
        logger.error("Cookie 刷新异常：shop_id=%s, %s", payload.shop_id, exc)
        return error_response(-1, "Cookie 刷新失败，请稍后重试")

    if info is None:
        # 回退：扫码 / Cookie 导入的店铺没有浏览器用户数据目录，
        # 直接用库中已存 Cookie 做一次校验请求（同时起到保活作用）。
        cookies = load_account_cookies(payload.shop_id, payload.owner_user_id)
        if cookies:
            alive = await asyncio.to_thread(
                _verify_cookies_alive,
                cookies,
                payload.shop_id,
                payload.owner_user_id,
            )
            if alive:
                logger.info("店铺 shop_id=%s 使用现有 Cookie 保活成功", payload.shop_id)
                return success_response(
                    data={"shop_id": payload.shop_id, "shop_pk": payload.shop_pk},
                    message="Cookie 保活成功",
                )
        _safe_mark_relogin(payload.shop_id, payload.owner_user_id)
        logger.warning("店铺 shop_id=%s 登录态已失效，需重新扫码登录", payload.shop_id)
        return error_response(-1, "登录态已失效，需重新扫码登录")

    # 刷新成功：将新 Cookie 加密回写 account 表（不返回明文，需求 3.6）。
    update_account_cookies(
        payload.shop_id, payload.owner_user_id, info.get("cookies")
    )
    logger.info("店铺 shop_id=%s Cookie 刷新成功", payload.shop_id)
    # 仅返回非敏感的店铺标识信息，绝不外泄 Cookie 明文。
    return success_response(
        data={"shop_id": payload.shop_id, "shop_pk": payload.shop_pk},
        message="Cookie 刷新成功",
    )


__all__ = ["router"]
