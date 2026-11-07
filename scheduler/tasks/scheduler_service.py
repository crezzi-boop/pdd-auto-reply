# -*- coding: utf-8 -*-
"""
scheduler.tasks.scheduler_service —— 定时任务调度服务 SchedulerService
======================================================================
本文件用途：实现 scheduler 服务的核心调度器 ``SchedulerService``（设计「核心组件
清单」中 SchedulerService，需求 21.2 / 21.4）。其职责：

1. 启动时从数据库 ``scheduled_task`` 表读取全部「启用」任务配置；
2. 按各任务的调度方式（cron / interval）将对应执行体（tasks.task_runners 中的
   Cookie 刷新 / 商品同步 / 文件日志清理）注册到 APScheduler；
3. 周期触发任务执行体，执行体内部各自写 ``task_run_log`` 执行日志；
4. 提供启动 / 停止接口，供服务 lifespan 调用。

调度方式说明（与 sys_dict 的 schedule_type 一致）：
- cron：``schedule_config`` 为标准 5 段 crontab 表达式（分 时 日 月 周）；
- interval：``schedule_config`` 为间隔秒数（正整数字符串）。

关键约束（开发规范）：
- 规范 12 / 16：读取配置经 common.db.repository 参数化查询，禁止拼接 SQL。
- 规范 13：读取配置经 run_with_retry 在连接失败时自动重试。
- 规范 21：跨服务地址经环境变量（由 service_client 负责），本文件不写死地址。
- 规范 11 / 需求 19.5：文件日志清理仅清磁盘文件，数据库业务日志禁止物理删除。
- 导入置顶（规范 51）、中文注释（规范 37）、单文件 ≤500 行（规范 35）。

依赖说明：调度库使用 APScheduler（pyproject 已声明 apscheduler 依赖）。
"""
from __future__ import annotations

import logging
from typing import List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from common.db.repository import Repository, run_with_retry
from common.models.task_models import ScheduledTask
from common.services.reply_guard_settings import GUARD_SCAN_INTERVAL_SECONDS

from tasks.constants import (
    SCHEDULE_TYPE_CRON,
    SCHEDULE_TYPE_INTERVAL,
    SUPPORTED_TASK_KEYS,
    TASK_REPLY_GUARD,
)
from tasks.task_runners import TASK_RUNNERS

# 模块级日志记录器（禁用 debug 级别 —— 规范 38）。
logger = logging.getLogger("scheduler.scheduler_service")


class SchedulerService:
    """定时任务调度服务：按 scheduled_task 配置注册并触发任务。

    本服务持有一个 APScheduler 后台调度器实例，启动时加载启用任务配置并注册，
    停止时优雅关闭调度器。每个任务的执行体内部负责写 task_run_log 执行日志。
    """

    def __init__(self) -> None:
        """构造调度服务（不立即启动，由 start 显式启动）。"""
        # 后台调度器：在独立线程触发任务，不阻塞服务主线程。
        # job_defaults 说明（修复默认 misfire_grace_time=1 秒导致任务被静默跳过）：
        # - misfire_grace_time：触发时刻因系统繁忙 / 上一轮超时而延后时的容忍秒数。
        #   默认仅 1 秒，意味着轻微延迟就会让该次执行被判为 misfire 直接跳过（如每天
        #   3 点的清理可能整天不执行）。这里放宽到 1 小时，保证延迟后仍会补跑。
        # - coalesce=True：服务停机期间堆积的多次触发合并为一次，避免追赶式风暴。
        # - max_instances=1：同一任务不重叠执行（上一轮未结束则跳过本轮，防并发冲突）。
        self._scheduler: BackgroundScheduler = BackgroundScheduler(
            job_defaults={
                "misfire_grace_time": 3600,
                "coalesce": True,
                "max_instances": 1,
            }
        )
        # 是否已启动标记，避免重复启动 / 停止。
        self._started: bool = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> int:
        """启动调度器并加载注册全部启用任务。

        Returns:
            成功注册的任务数量。
        """
        if self._started:
            logger.warning("调度器已启动，跳过重复启动")
            return len(self._scheduler.get_jobs())

        registered = self._load_and_register()

        # 内置硬约束任务：客户消息回复保底扫描。不读 scheduled_task 表、不开放
        # 配置与启停，保证「90 秒内必有一条默认回复」不会被任何设置关掉。
        # 扫描计时跑在 scheduler 独立进程，不依赖 websocket 的消息消费队列；
        # 实际发送经 HTTP 由 websocket 服务完成。
        self._scheduler.add_job(
            TASK_RUNNERS[TASK_REPLY_GUARD],
            trigger=IntervalTrigger(
                seconds=GUARD_SCAN_INTERVAL_SECONDS, timezone="Asia/Shanghai"
            ),
            id=TASK_REPLY_GUARD,
            name="客户消息回复保底扫描（内置，不可关闭）",
            replace_existing=True,
        )
        registered += 1

        self._scheduler.start()
        self._started = True
        logger.info("定时任务调度器已启动，注册任务 %d 个", registered)
        return registered

    def shutdown(self) -> None:
        """停止调度器并释放资源（优雅关闭，不等待运行中任务无限阻塞）。"""
        if not self._started:
            return
        # wait=False：不阻塞等待运行中的任务，立即关闭调度线程。
        self._scheduler.shutdown(wait=False)
        self._started = False
        logger.info("定时任务调度器已停止")

    # ------------------------------------------------------------------
    # 配置加载与注册
    # ------------------------------------------------------------------
    def _load_and_register(self) -> int:
        """从数据库读取启用任务并逐个注册到调度器。

        读取失败（如数据库不可用）不抛出，仅记录错误并返回 0，保证服务可启动。

        Returns:
            成功注册的任务数量。
        """
        try:
            tasks = run_with_retry(self._fetch_enabled_tasks)
        except Exception as exc:  # noqa: BLE001 —— 读取失败不阻断服务启动
            logger.error("读取定时任务配置失败，调度器空载启动：%s", exc)
            return 0

        registered = 0
        for task in tasks:
            if self._register_task(task):
                registered += 1
        return registered

    @staticmethod
    def _fetch_enabled_tasks(session) -> List[ScheduledTask]:
        """查询全部启用状态的定时任务配置（参数化查询）。

        Args:
            session: 事务性会话（由 run_with_retry 管理）。

        Returns:
            启用的 ScheduledTask 列表（与会话解绑后仍可读取已加载属性）。
        """
        repo = Repository(ScheduledTask, session)
        tasks = repo.list(filters={"enabled": True})
        # 触发属性加载，避免会话关闭后访问惰性属性（expire_on_commit=False 已配置）。
        for task in tasks:
            _ = (task.task_key, task.schedule_type, task.schedule_config)
        return tasks

    def _register_task(self, task: ScheduledTask) -> bool:
        """将单个任务配置注册到调度器。

        校验任务键受支持、调度方式合法、调度配置可解析后注册；任一不满足则跳过
        该任务并记录警告，不影响其它任务注册。

        Args:
            task: 定时任务配置记录。

        Returns:
            True 表示注册成功；False 表示跳过。
        """
        task_key = task.task_key
        if task_key not in SUPPORTED_TASK_KEYS:
            logger.warning("跳过未知任务键：task_key=%s", task_key)
            return False

        runner = TASK_RUNNERS.get(task_key)
        if runner is None:
            logger.warning("跳过无执行体的任务：task_key=%s", task_key)
            return False

        trigger = self._build_trigger(task)
        if trigger is None:
            logger.warning(
                "跳过调度配置无效的任务：task_key=%s schedule_type=%s config=%s",
                task_key,
                task.schedule_type,
                task.schedule_config,
            )
            return False

        # 以 task_key 作为 job_id，保证同一任务幂等注册（重复加载时替换）。
        self._scheduler.add_job(
            runner,
            trigger=trigger,
            id=task_key,
            name=task.task_name or task_key,
            replace_existing=True,
        )
        logger.info("已注册定时任务：task_key=%s name=%s", task_key, task.task_name)
        return True

    @staticmethod
    def _build_trigger(task: ScheduledTask):
        """根据任务调度方式与配置构造 APScheduler 触发器。

        Args:
            task: 定时任务配置记录。

        Returns:
            CronTrigger / IntervalTrigger 实例；配置非法时返回 None。
        """
        schedule_type = task.schedule_type
        config = (task.schedule_config or "").strip()

        if schedule_type == SCHEDULE_TYPE_CRON:
            return SchedulerService._build_cron_trigger(config)
        if schedule_type == SCHEDULE_TYPE_INTERVAL:
            return SchedulerService._build_interval_trigger(config)
        # 未知调度方式。
        return None

    @staticmethod
    def _build_cron_trigger(config: str) -> Optional[CronTrigger]:
        """构造 Cron 触发器（config 为标准 5 段 crontab 表达式）。

        Args:
            config: crontab 表达式，如 "0 3 * * *"（每天 3 点）。

        Returns:
            CronTrigger 实例；解析失败返回 None。
        """
        if not config:
            return None
        try:
            # 北京时间口径（规范 17）：触发器时区统一设为 Asia/Shanghai。
            return CronTrigger.from_crontab(config, timezone="Asia/Shanghai")
        except (ValueError, TypeError) as exc:
            logger.warning("解析 cron 表达式失败：config=%s err=%s", config, exc)
            return None

    @staticmethod
    def _build_interval_trigger(config: str) -> Optional[IntervalTrigger]:
        """构造固定间隔触发器（config 为间隔秒数的正整数字符串）。

        Args:
            config: 间隔秒数字符串，如 "3600"（每小时）。

        Returns:
            IntervalTrigger 实例；非正整数返回 None。
        """
        try:
            seconds = int(config)
        except (ValueError, TypeError):
            logger.warning("间隔秒数非法：config=%s", config)
            return None
        if seconds <= 0:
            logger.warning("间隔秒数须为正整数：config=%s", config)
            return None
        return IntervalTrigger(seconds=seconds, timezone="Asia/Shanghai")


__all__ = [
    "SchedulerService",
]
