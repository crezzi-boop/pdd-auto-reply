# -*- coding: utf-8 -*-
"""
websocket.routes.qr_login —— 系统内置扫码登录会话（供 backend 调用）
==================================================================
在 websocket 服务内维护一个 Playwright 扫码会话：打开拼多多商家后台扫码登录页，
把二维码以 base64 返回给 backend（最终展示在系统网页里），用户在手机 App 上
扫码确认后，会话自动导出 Cookie 并置为 success，由 backend 落库建店。

设计要点：
- 全异步、单活跃会话（避免多个 Chromium 抢占内存）；
- 二维码每分钟自动刷新并以 base64 回传，前端无需人工换图；
- Cookie 仅在服务间内网传输，不直接暴露给浏览器；
- 会话 15 分钟超时，异常统一规整为失败响应，不抛异常打断 websocket 服务。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright

from common.schemas.common import ApiResponse, error_response, success_response

logger = logging.getLogger("websocket.routes.qr_login")

router = APIRouter(tags=["扫码登录"])

_PDD_LOGIN_URL = "https://mms.pinduoduo.com/login"
_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_BROWSER_DATA_ROOT = "/app/websocket/browser_data"
_CLIP = {"x": 860, "y": 140, "width": 340, "height": 340}
_SESSION_TTL = 900.0
_QR_REFRESH_SECONDS = 60.0


@dataclass
class _Session:
    """一次扫码登录会话的运行时状态（仅内存，服务重启即失效）。"""

    id: str
    state: str = "starting"
    qr_image: str = ""
    cookies: str = ""
    message: str = ""
    created: float = field(default_factory=time.monotonic)
    task: Optional[asyncio.Task] = None


_sessions: Dict[str, _Session] = {}
_lock = asyncio.Lock()


def _cleanup_stale_profiles() -> None:
    """清理一小时前遗留的扫码浏览器 profile，避免磁盘被逐步占满。"""
    try:
        root = Path(_BROWSER_DATA_ROOT)
        if not root.exists():
            return
        now = time.time()
        for child in root.glob("qr_*"):
            if child.is_dir() and now - child.stat().st_mtime > 3600:
                shutil.rmtree(child, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响主流程
        logger.warning("清理旧扫码浏览器目录失败：%s", exc)


async def _capture_qr(session: _Session, page: Any) -> None:
    """截取当前二维码并写成 data URI；优先截 canvas，失败回退区域截图。"""
    shot: Optional[bytes] = None
    try:
        qr = page.locator("canvas").first
        if await qr.count() > 0:
            box = await qr.bounding_box()
            if box and box["width"] >= 80 and box["height"] >= 80:
                shot = await qr.screenshot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("截取二维码画布失败，回退区域截图：%s", exc)
    if not shot:
        shot = await page.screenshot(clip=_CLIP)
    session.qr_image = "data:image/png;base64," + base64.b64encode(shot).decode("ascii")


async def _run_session(session: _Session) -> None:
    """后台保持浏览器会话，等待用户扫码并自动导出 Cookie。"""
    ctx = None
    try:
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                "%s/qr_%s" % (_BROWSER_DATA_ROOT, session.id[:8]),
                headless=True,
                user_agent=_CHROME_UA,
                viewport={"width": 1280, "height": 720},
                device_scale_factor=3,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(_PDD_LOGIN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(4500)
            await _capture_qr(session, page)
            session.state = "waiting"
            session.message = "二维码已生成，请用手机扫码"
            started = time.monotonic()
            last_shot = started
            while time.monotonic() - started < _SESSION_TTL:
                if session.state == "cancelled":
                    return
                url = page.url or ""
                if "/login" not in url.lower():
                    cookies = await ctx.cookies()
                    session.cookies = "; ".join(
                        "%s=%s" % (c["name"], c["value"])
                        for c in cookies
                        if c.get("name") and c.get("value")
                    )
                    session.state = "success"
                    session.message = "登录成功，正在自动添加店铺…"
                    logger.info(
                        "扫码会话 %s 登录成功，Cookie 条目 %d",
                        session.id,
                        len(cookies),
                    )
                    return
                if time.monotonic() - last_shot > _QR_REFRESH_SECONDS:
                    try:
                        await _capture_qr(session, page)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("刷新二维码失败：%s", exc)
                    last_shot = time.monotonic()
                await asyncio.sleep(2)
            session.state = "expired"
            session.message = "二维码已超时，请重新获取"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("扫码会话 %s 异常：%s", session.id, exc)
        session.state = "failed"
        session.message = "扫码会话异常：%s" % str(exc)[:120]
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass


class _SessionRequest(BaseModel):
    """扫码会话请求体。"""

    session_id: str = Field(..., description="会话 ID")


@router.post("/qr-login/start", response_model=ApiResponse, summary="启动扫码登录会话")
async def start_qr_login() -> ApiResponse:
    """启动（或替换）扫码会话，返回二维码 data URI 与会话 ID。"""
    _cleanup_stale_profiles()
    async with _lock:
        for old in list(_sessions.values()):
            if old.state in ("starting", "waiting"):
                old.state = "cancelled"
                if old.task and not old.task.done():
                    old.task.cancel()
        now = time.monotonic()
        for key in [k for k, v in _sessions.items() if now - v.created > 3600]:
            _sessions.pop(key, None)
        session = _Session(id=uuid.uuid4().hex)
        _sessions[session.id] = session
        session.task = asyncio.create_task(_run_session(session))
    for _ in range(120):
        if session.qr_image or session.state in ("failed", "expired"):
            break
        await asyncio.sleep(0.25)
    if not session.qr_image:
        return error_response(-1, session.message or "二维码生成超时，请重试")
    return success_response(
        data={
            "session_id": session.id,
            "qr_image": session.qr_image,
            "expires_in": int(_SESSION_TTL),
        },
        message="二维码已生成",
    )


@router.get("/qr-login/status", response_model=ApiResponse, summary="查询扫码登录会话状态")
async def get_qr_login_status(
    session_id: str = Query(..., description="会话 ID"),
) -> ApiResponse:
    """查询会话状态；成功时附带 Cookie（仅服务间内网调用）。"""
    session = _sessions.get(session_id)
    if session is None:
        return error_response(-1, "会话不存在或已过期，请重新获取二维码")
    return success_response(
        data={
            "state": session.state,
            "qr_image": session.qr_image,
            "message": session.message,
            "cookies": session.cookies,
        }
    )


@router.post("/qr-login/cancel", response_model=ApiResponse, summary="取消扫码登录会话")
async def cancel_qr_login(payload: _SessionRequest) -> ApiResponse:
    """取消会话并释放浏览器资源。"""
    session = _sessions.get(payload.session_id)
    if session is None:
        return success_response(message="会话已结束")
    if session.state in ("starting", "waiting"):
        session.state = "cancelled"
        session.message = "已取消"
        if session.task and not session.task.done():
            session.task.cancel()
    return success_response(message="已取消扫码登录")


__all__ = ["router"]
