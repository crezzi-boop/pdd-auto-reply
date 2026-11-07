# -*- coding: utf-8 -*-
"""
scheduler.tasks.constants —— 定时任务常量集中管理
================================================
本文件用途：集中定义 scheduler 服务定时任务相关的常量（任务键、调度方式、
执行结果），避免在多个文件中散落硬编码字符串（规范 12 / 36：统一管理、不重复）。

各常量取值与 common 数据字典 ``schedule_type`` / ``run_result`` 及业务模型
``ScheduledTask`` / ``TaskRunLog`` 的枚举约定保持一致（需求 21.2）：
- 任务键 task_key：cookie_refresh / product_sync / log_file_cleanup；
- 调度方式 schedule_type：cron（Cron 表达式）/ interval（固定间隔秒数）；
- 执行结果 run_result：success / failed。
"""
from __future__ import annotations

# ----------------------------------------------------------------------
# 任务键 task_key（与 scheduled_task.task_key 取值一致，需求 21.2）
# ----------------------------------------------------------------------
# Cookie 刷新：跨服务经 HTTP 调用 websocket 服务刷新各店铺登录态 Cookie。
TASK_COOKIE_REFRESH: str = "cookie_refresh"
# 商品同步：跨服务经 HTTP 调用 backend 服务触发商品同步。
TASK_PRODUCT_SYNC: str = "product_sync"
# 文件日志清理：按保留天数仅清理磁盘日志文件（数据库业务日志表禁止物理删除）。
TASK_LOG_FILE_CLEANUP: str = "log_file_cleanup"
# 客户消息回复保底扫描：硬约束任务，保证任何客户消息 90 秒内有商家回复。
# 该任务由 SchedulerService 启动时「内置注册」，不写入 scheduled_task 表、不开放
# 配置与启停（保底不可关闭），因此不加入下方 SUPPORTED_TASK_KEYS。
TASK_REPLY_GUARD: str = "reply_guard"

# 全部受支持的任务键集合（用于校验配置中的 task_key 是否可调度）。
# 仅包含「由 scheduled_task 表配置驱动」的任务；内置任务 reply_guard 不在此列。
SUPPORTED_TASK_KEYS: frozenset[str] = frozenset(
    {TASK_COOKIE_REFRESH, TASK_PRODUCT_SYNC, TASK_LOG_FILE_CLEANUP}
)

# ----------------------------------------------------------------------
# 调度方式 schedule_type（与 sys_dict 的 schedule_type 一致，需求 21.2）
# ----------------------------------------------------------------------
# Cron 表达式调度：schedule_config 为标准 5 段 crontab 表达式。
SCHEDULE_TYPE_CRON: str = "cron"
# 固定间隔调度：schedule_config 为间隔秒数（正整数字符串）。
SCHEDULE_TYPE_INTERVAL: str = "interval"

# ----------------------------------------------------------------------
# 执行结果 run_result（与 sys_dict 的 run_result 一致，需求 21.2）
# ----------------------------------------------------------------------
# 执行成功。
RESULT_SUCCESS: str = "success"
# 执行失败。
RESULT_FAILED: str = "failed"


__all__ = [
    "TASK_COOKIE_REFRESH",
    "TASK_PRODUCT_SYNC",
    "TASK_LOG_FILE_CLEANUP",
    "TASK_REPLY_GUARD",
    "SUPPORTED_TASK_KEYS",
    "SCHEDULE_TYPE_CRON",
    "SCHEDULE_TYPE_INTERVAL",
    "RESULT_SUCCESS",
    "RESULT_FAILED",
]
