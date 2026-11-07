# -*- coding: utf-8 -*-
"""
websocket.routes 路由子包
=========================
本文件为 websocket 服务对外 HTTP 路由的聚合子包。

websocket 服务在维护与拼多多长连接的同时，对外暴露供 backend / scheduler 经
**服务间 HTTP 调用**触发的运行时能力接口（地址经环境变量配置，禁止写死 localhost，
规范 21 / 需求 25.4）：

- ``connections``：连接启停（启动并装配消息处理全链路 / 断开，需求 5.1 / 5.3 / 3.5）。
- ``products``：商品拉取（供 backend 商品同步拉取拼多多商品，需求 15.2 / 15.3）。
- ``messages``：手动发送消息（供 backend 在线聊天手动收发，需求 14.3）。
- ``cookies``：Cookie 刷新（供 scheduler 定时刷新登录态，需求 4.6 / 21.2）。
- ``login``：店铺登录（供 backend 账号密码登录 / Cookie 导入并返回店铺信息，需求 4.1-4.4）。

各接口均返回项目统一响应体 ``{code, success, message, data}``、HTTP 恒 200（规范 1-3）。
``api_router`` 聚合全部子路由，由 ``_bootstrap.py`` 以统一前缀 ``/api/v1`` 挂载。
"""
from __future__ import annotations

from fastapi import APIRouter

from routes import connections, cookies, login, messages, products, qr_login

# websocket 服务对外路由聚合器（由 _bootstrap 以 /api/v1 前缀挂载）。
api_router = APIRouter()

# 连接断开接口（需求 3.5）。
api_router.include_router(connections.router)

# 商品拉取接口（需求 15.2 / 15.3）。
api_router.include_router(products.router)

# 手动发送消息接口（需求 14.3）。
api_router.include_router(messages.router)

# Cookie 刷新接口（需求 4.6 / 21.2）。
api_router.include_router(cookies.router)

# 店铺登录接口：账号密码登录 / Cookie 导入并返回店铺信息（需求 4.1 / 4.2 / 4.3 / 4.4）。
api_router.include_router(login.router)

# 扫码登录会话：网页自助获取二维码、手机扫码确认后自动建店。
api_router.include_router(qr_login.router)


__all__ = ["api_router"]
