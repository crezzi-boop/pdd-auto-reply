# -*- coding: utf-8 -*-
"""
common.db.init_database —— 启动自检迁移器（SchemaMigrator）
==========================================================
本文件用途：实现「拼多多自动回复」系统的数据库启动自检迁移（开发规范 14、
需求 24.5）。服务启动时自动完成：
- **缺表建表**：对比 ``Base.metadata`` 与数据库现有表，缺失的表自动创建；
- **缺字段补字段**：对比每张表的模型列与数据库实际列，缺失的列经
  ``ALTER TABLE ... ADD COLUMN`` 补齐（DDL 由 SQLAlchemy 编译生成，
  不做字符串拼接 SQL，类型映射借助列类型编译，兼容 MySQL）；
- **缺字典 / 初始数据补齐**：调用「已注册的字典初始化钩子」补齐枚举字典与
  初始数据（与任务 2.14 字典服务协作；钩子未注册时为空操作，框架仍可独立运行）。

核心约束（开发规范）：
- 规范 11 / 需求 24.5：**只增不改不删** —— 已存在的表 / 字段 / 数据一律保持
  不变，绝不删除或修改历史数据；建表用 ``checkfirst`` 仅建缺失表，补字段仅补
  缺失列，字典钩子须自身保证「存在则跳过」。
- **幂等**（Property 22 / 任务 2.13）：连续运行两次，第二次不产生额外结构
  变更 —— 建表 / 补字段均先经 Inspector 比对，已存在即跳过。
- 规范 13 / 需求 24.4：数据库连接失败需重试 —— 入口函数经 ``with_db_retry``
  包装；迁移操作幂等，重试安全。
- 规范 16：DDL 由 SQLAlchemy 的 ``CreateColumn`` 编译生成，表名 / 列名 / 类型
  均来自受信任的模型元数据，不引入用户输入拼接。

多服务约定：建表 / 迁移自检统一由 ``backend`` 在其 ``lifespan`` 中调用
``init_database()`` 执行（任务 4.1）；``websocket`` / ``scheduler`` 服务启动时
仅做连接检查，不重复执行迁移，避免多服务并发迁移冲突。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import Index, UniqueConstraint, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateColumn, CreateIndex, MetaData, Table

from common.db.retry import with_db_retry
from common.db.session import get_engine, session_scope
from common.models.base import Base

# 模块级日志记录器（统一使用 info/warning/error，禁用 debug —— 规范 38）
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 字典 / 初始数据初始化钩子注册表
# ----------------------------------------------------------------------
# 字典服务（任务 2.14）尚未就绪时，本注册表为空，迁移仅做建表 / 补字段；
# 任务 2.14 实现后，由字典服务模块在导入时调用 register_dict_initializer()
# 注册「字典 / 初始数据填充函数」，迁移末尾统一回调，完成枚举字典与初始数据补齐。
#
# 钩子签名约定：``def initializer(session: Session) -> int``，返回新插入的记录
# 条数（已存在则跳过、返回 0），钩子须自身保证幂等（只增不改），以满足
# 「不影响历史数据」与「启动迁移幂等」（需求 24.5 / Property 22）。
_DICT_INITIALIZERS: list[Callable[[Session], int]] = []


def register_dict_initializer(initializer: Callable[[Session], int]) -> None:
    """注册一个字典 / 初始数据初始化钩子，供启动迁移末尾回调。

    幂等注册：同一函数对象重复注册仅保留一次，避免重复执行。

    Args:
        initializer: 接收当前会话、补齐字典 / 初始数据并返回新增条数的可调用对象。
            钩子须自身保证「存在则跳过」（只增不改不删）。
    """
    if initializer not in _DICT_INITIALIZERS:
        _DICT_INITIALIZERS.append(initializer)


def clear_dict_initializers() -> None:
    """清空已注册的字典初始化钩子（主要供测试隔离使用）。"""
    _DICT_INITIALIZERS.clear()


@dataclass
class MigrationResult:
    """启动自检迁移结果，记录本次实际发生的结构 / 数据变更，便于日志与断言。

    - created_tables：本次新建的表名列表（已存在的表不在内）；
    - added_columns：本次补齐的列，元素为 ``"表名.列名"``；
    - upgraded_columns：本次扩容为大文本的列，元素为 ``"表名.列名"``；
    - seeded_rows：本次字典 / 初始数据钩子新增的记录总条数。

    幂等判定：``changed`` 为 False 表示本次运行未产生任何结构 / 数据变更
    （即第二次运行的预期结果）。
    """

    created_tables: list[str] = field(default_factory=list)
    added_columns: list[str] = field(default_factory=list)
    added_unique_indexes: list[str] = field(default_factory=list)
    upgraded_columns: list[str] = field(default_factory=list)
    seeded_rows: int = 0

    @property
    def changed(self) -> bool:
        """本次运行是否产生了任何变更（建表 / 补字段 / 扩容列 / 补唯一索引 / 补数据）。"""
        return bool(
            self.created_tables
            or self.added_columns
            or self.added_unique_indexes
            or self.upgraded_columns
            or self.seeded_rows
        )


class SchemaMigrator:
    """启动自检迁移器：建表 / 补字段 / 补字典初始数据，只增不改不删、幂等。

    迁移按顺序执行：
    1. 建缺失表（基于 ``metadata.create_all(checkfirst=True)``）；
    2. 补缺失列（Inspector 比对模型列与实际列，缺失者 ALTER TABLE ADD COLUMN）；
    3. 补缺失唯一索引（建前检测历史重复）；
    4. 历史 TEXT 列扩容为 LONGTEXT（仅 MySQL，默认回复上限 100 条所需）；
    5. 同步表注释（比对并补齐已存在表的表注释，仅 MySQL）；
    6. 补字典 / 初始数据（回调已注册的初始化钩子）。

    所有步骤均先比对、后变更，已存在的表 / 列 / 数据保持不变，连续运行幂等。
    """

    def __init__(self, engine: Engine | None = None, metadata: MetaData | None = None) -> None:
        """构造迁移器。

        Args:
            engine: 目标数据库引擎；None 时取进程内单例引擎 ``get_engine()``。
            metadata: 模型元数据；None 时取统一基类 ``Base.metadata``（含全部表）。
        """
        self.engine: Engine = engine if engine is not None else get_engine()
        self.metadata: MetaData = metadata if metadata is not None else Base.metadata

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def run(self) -> MigrationResult:
        """执行完整启动自检迁移，返回本次变更结果。

        Returns:
            ``MigrationResult``：记录新建表、补齐列与新增初始数据条数。
        """
        result = MigrationResult()
        # 1) 建缺失表
        result.created_tables = self._create_missing_tables()
        # 2) 补缺失列
        result.added_columns = self._add_missing_columns()
        # 3) 补缺失唯一索引（建前检测历史重复，有重复则跳过并告警，绝不删数据）
        result.added_unique_indexes = self._add_missing_unique_constraints()
        # 4) 历史 TEXT 列扩容为 LONGTEXT（默认回复上限 100 条，幂等）
        result.upgraded_columns = self._upgrade_longtext_columns()
        # 5) 同步表注释（幂等：仅当注释缺失 / 不一致时 ALTER）
        self._sync_table_comments()
        # 6) 补字典 / 初始数据
        result.seeded_rows = self._seed_initial_data()

        if result.changed:
            logger.info(
                "启动自检迁移完成：新建表 %d 张，补齐字段 %d 个，补唯一索引 %d 个，"
                "补充初始数据 %d 条",
                len(result.created_tables),
                len(result.added_columns),
                len(result.added_unique_indexes),
                result.seeded_rows,
            )
        else:
            logger.info("启动自检迁移完成：数据库结构与初始数据已是最新，无需变更")
        return result

    # ------------------------------------------------------------------
    # 步骤 1：建缺失表
    # ------------------------------------------------------------------
    def _create_missing_tables(self) -> list[str]:
        """对比元数据与数据库现有表，创建缺失的表（已存在的表不受影响）。

        使用 ``metadata.create_all(checkfirst=True)`` 仅创建数据库中尚不存在的
        表，已存在表不会被重建或修改，保证幂等与历史数据安全。

        Returns:
            本次新建的表名列表。
        """
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        # 元数据中定义、但数据库尚不存在的表即为待新建表
        missing = [
            name for name in self.metadata.tables.keys() if name not in existing_tables
        ]
        if missing:
            # checkfirst=True：仅创建缺失表；不会改动已存在表（只增不改）
            self.metadata.create_all(bind=self.engine, checkfirst=True)
            for name in missing:
                logger.info("启动自检：新建数据表 %s", name)
        return missing

    # ------------------------------------------------------------------
    # 步骤 2：补缺失列
    # ------------------------------------------------------------------
    def _add_missing_columns(self) -> list[str]:
        """对比每张表的模型列与数据库实际列，补齐缺失列（不改 / 不删已有列）。

        - 经 Inspector 读取数据库实际列名集合；
        - 模型中定义、数据库中缺失的列，逐列执行 ``ALTER TABLE ADD COLUMN``；
        - 列定义 DDL 由 SQLAlchemy ``CreateColumn`` 编译生成（含类型映射），
          兼容当前方言（MySQL / SQLite），不做字符串拼接 SQL（规范 16）。

        Returns:
            本次补齐的列列表，元素形如 ``"表名.列名"``。
        """
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        added: list[str] = []

        for table_name, table in self.metadata.tables.items():
            # 新建的表已含全部列，无需补字段；只处理「已存在的表」
            if table_name not in existing_tables:
                continue
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
            missing_columns = [
                column for column in table.columns if column.name not in existing_columns
            ]
            for column in missing_columns:
                self._add_column(table, column)
                added.append(f"{table_name}.{column.name}")
                logger.info("启动自检：为表 %s 补齐字段 %s", table_name, column.name)
        return added

    def _add_column(self, table: Table, column) -> None:
        """对单张已存在的表执行 ``ALTER TABLE ADD COLUMN``（DDL 由 SQLAlchemy 生成）。

        为保证「不影响历史数据」（需求 24.5），对「非空且无默认值」的新列降级为
        可空后再添加：历史行无法为新非空列提供取值，强行 NOT NULL 会导致
        ALTER 失败或破坏历史数据；降级为可空既能补齐字段又不损伤历史数据。

        Args:
            table: 目标表的 ``Table`` 对象（来自元数据）。
            column: 待添加的 ``Column`` 对象（来自元数据）。
        """
        ddl_column = column
        # 非空且无「服务端默认值」(server_default) 的列：对历史行不安全。
        # 注意 Python 端 default 仅在 ORM 插入时生效，不会编译进 DDL，无法为
        # 历史行补值；因此只要缺少 server_default，就降级为可空再添加，避免
        # 「为含历史数据的表添加 NOT NULL 列」失败、保护历史数据（需求 24.5）。
        if not column.nullable and column.server_default is None:
            ddl_column = column._copy()
            ddl_column.nullable = True

        # CreateColumn 编译出形如 "col_name TYPE [NOT NULL] [DEFAULT ...]" 的列定义
        column_spec = str(CreateColumn(ddl_column).compile(dialect=self.engine.dialect))
        # 表名由 SQLAlchemy 预处理器加引号，列定义由编译器生成，均非用户输入
        quoted_table = self.engine.dialect.identifier_preparer.format_table(table)
        ddl = text(f"ALTER TABLE {quoted_table} ADD COLUMN {column_spec}")
        with self.engine.begin() as connection:
            connection.execute(ddl)

    # ------------------------------------------------------------------
    # 步骤 4：历史 TEXT 列扩容为 LONGTEXT（默认回复上限放宽到 100 条）
    # ------------------------------------------------------------------
    # 需要扩容的列（表名, 列名）：仅当数据库实际类型仍是 TEXT / MEDIUMTEXT 时执行。
    _LONGTEXT_COLUMNS: tuple[tuple[str, str], ...] = (
        ("pdd_default_reply", "contents"),
    )

    def _upgrade_longtext_columns(self) -> list[str]:
        """把历史 TEXT 列扩容为模型声明的 LONGTEXT（仅 MySQL，幂等、不丢数据）。

        默认回复由最多 20 条放宽到 100 条后，多条语句的 JSON 文本可能超过 MySQL
        TEXT 的 64KB 上限。本方法在列实际类型仍为 TEXT / MEDIUMTEXT 时执行一次
        ``ALTER TABLE ... MODIFY COLUMN``；已是 LONGTEXT 或非 TEXT 类型则跳过。

        Returns:
            本次扩容的列列表，元素形如 ``"表名.列名"``。
        """
        if self.engine.dialect.name != "mysql":
            return []
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        upgraded: list[str] = []
        for table_name, column_name in self._LONGTEXT_COLUMNS:
            if table_name not in existing_tables:
                continue
            table = self.metadata.tables.get(table_name)
            if table is None or column_name not in table.columns:
                continue
            column = table.columns[column_name]
            column_info = next(
                (
                    col
                    for col in inspector.get_columns(table_name)
                    if col["name"] == column_name
                ),
                None,
            )
            if column_info is None:
                continue
            type_name = str(column_info["type"]).upper()
            if "LONGTEXT" in type_name or "TEXT" not in type_name:
                continue
            column_spec = str(
                CreateColumn(column).compile(dialect=self.engine.dialect)
            )
            # MODIFY COLUMN 会覆盖列注释，显式带回去（注释来自模型元数据，非用户输入）。
            comment = str(column.comment or "").replace("'", "''")
            if comment and " COMMENT " not in column_spec.upper():
                column_spec += f" COMMENT '{comment}'"
            quoted_table = self.engine.dialect.identifier_preparer.format_table(table)
            ddl = text(f"ALTER TABLE {quoted_table} MODIFY COLUMN {column_spec}")
            with self.engine.begin() as connection:
                connection.execute(ddl)
            upgraded.append(f"{table_name}.{column_name}")
            logger.info(
                "启动自检：将字段 %s.%s 扩容为 LONGTEXT", table_name, column_name
            )
        return upgraded

    # ------------------------------------------------------------------
    # 步骤 5：补缺失唯一索引（只增不改不删、建前检测历史重复 —— 需求 24.5）
    # ------------------------------------------------------------------
    def _add_missing_unique_constraints(self) -> list[str]:
        """为已存在的表补建模型中声明、但数据库缺失的唯一约束 / 唯一索引。

        处理逻辑（幂等、安全）：
        - 仅处理「已存在的表」（新建表 create_all 已带约束）；
        - 取模型中声明的 ``UniqueConstraint`` 列组合，与数据库现有唯一约束 / 唯一
          索引的列组合比对，已存在（同列组合）即跳过，保证幂等；
        - 建唯一索引前先检测该列组合是否存在重复数据：**若有重复则跳过并告警，
          绝不删除 / 修改历史数据**（规范 11 / 需求 24.5），由人工先行清理；
        - 唯一索引经 SQLAlchemy ``Index(..., unique=True)`` 的 ``CreateIndex`` 编译
          生成 DDL，列名 / 表名来自受信任的模型元数据，不拼接用户输入（规范 16）。

        Returns:
            本次新建的唯一索引列表，元素形如 ``"表名(列1,列2)"``。
        """
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        added: list[str] = []

        for table_name, table in self.metadata.tables.items():
            if table_name not in existing_tables:
                continue
            # 模型声明的唯一约束列组合集合（每个元素为有序列名元组）。
            desired = self._model_unique_columns(table)
            if not desired:
                continue
            # 数据库现有唯一约束 / 唯一索引的列组合集合。
            existing = self._db_unique_columns(inspector, table_name)
            for columns in desired:
                if columns in existing:
                    continue  # 已存在同列组合的唯一约束，幂等跳过
                # 建索引前检测历史重复数据：有重复则跳过并告警，绝不删数据。
                if self._has_duplicate_rows(table_name, columns):
                    logger.warning(
                        "启动自检：表 %s 存在重复数据 %s，跳过唯一索引创建，请先人工清理",
                        table_name,
                        columns,
                    )
                    continue
                self._create_unique_index(table, columns)
                existing.add(columns)
                added.append(f"{table_name}({','.join(columns)})")
                logger.info(
                    "启动自检：为表 %s 补建唯一索引 %s", table_name, columns
                )
        return added

    @staticmethod
    def _model_unique_columns(table: Table) -> list[tuple[str, ...]]:
        """提取模型表中声明的唯一约束 / 唯一索引的列组合（有序列名元组列表）。

        Args:
            table: 模型 ``Table`` 对象。

        Returns:
            每个唯一约束 / 唯一索引对应一个有序列名元组（已去重）。
        """
        result: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint) and constraint.columns:
                cols = tuple(col.name for col in constraint.columns)
                if cols not in seen:
                    seen.add(cols)
                    result.append(cols)
        for index in table.indexes:
            if index.unique:
                cols = tuple(col.name for col in index.columns)
                if cols not in seen:
                    seen.add(cols)
                    result.append(cols)
        return result

    @staticmethod
    def _db_unique_columns(inspector, table_name: str) -> set[tuple[str, ...]]:
        """读取数据库中某表现有唯一约束 / 唯一索引的列组合集合。

        Args:
            inspector: SQLAlchemy Inspector。
            table_name: 表名。

        Returns:
            列组合集合（每个元素为有序列名元组）。
        """
        existing: set[tuple[str, ...]] = set()
        # 唯一约束（部分方言经 get_unique_constraints 暴露）。
        try:
            for uc in inspector.get_unique_constraints(table_name):
                cols = uc.get("column_names") or []
                if cols:
                    existing.add(tuple(cols))
        except NotImplementedError:
            pass
        # 唯一索引（兼容以 unique index 形式存在的唯一约束）。
        for index in inspector.get_indexes(table_name):
            if index.get("unique") and index.get("column_names"):
                existing.add(tuple(c for c in index["column_names"] if c))
        return existing

    def _has_duplicate_rows(self, table_name: str, columns: tuple[str, ...]) -> bool:
        """检测某表在指定列组合上是否存在重复数据（建唯一索引前的安全校验）。

        Args:
            table_name: 表名。
            columns: 列名组合（有序元组）。

        Returns:
            存在重复返回 True；无重复返回 False。
        """
        preparer = self.engine.dialect.identifier_preparer
        quoted_table = preparer.quote(table_name)
        quoted_cols = ", ".join(preparer.quote(col) for col in columns)
        # 列名 / 表名均来自模型元数据（受信任），非用户输入，不存在注入风险（规范 16）。
        stmt = text(
            f"SELECT {quoted_cols} FROM {quoted_table} "
            f"GROUP BY {quoted_cols} HAVING COUNT(*) > 1 LIMIT 1"
        )
        with self.engine.connect() as connection:
            return connection.execute(stmt).first() is not None

    def _create_unique_index(self, table: Table, columns: tuple[str, ...]) -> None:
        """对已存在的表创建唯一索引（DDL 由 SQLAlchemy ``CreateIndex`` 生成）。

        索引名采用稳定命名 ``uix_<表名>_<列名拼接>``，并截断到方言允许的标识符
        长度上限，避免超长导致建索引失败。

        Args:
            table: 目标表的 ``Table`` 对象。
            columns: 唯一索引列名组合（有序元组）。
        """
        max_len = self.engine.dialect.max_identifier_length or 64
        raw_name = f"uix_{table.name}_{'_'.join(columns)}"
        index_name = raw_name[:max_len]
        index = Index(index_name, *[table.c[col] for col in columns], unique=True)
        create_index = CreateIndex(index)
        with self.engine.begin() as connection:
            connection.execute(create_index)

    # ------------------------------------------------------------------
    # 步骤 4：同步表注释（幂等补齐 —— 规范 37 注释完善）
    # ------------------------------------------------------------------
    def _sync_table_comments(self) -> None:
        """将模型中声明的表注释同步到数据库已存在的表（幂等）。

        对比 ``metadata`` 中各表的 ``comment`` 与数据库现有 ``table_comment``，
        仅当模型声明了注释、且与库内现有注释不一致时执行
        ``ALTER TABLE ... COMMENT=...``，避免无谓 DDL。仅 MySQL 方言执行
        （SQLite 无表注释概念，直接跳过，便于单元测试）；只改注释、不动结构与
        数据，安全幂等（规范 11 / 需求 24.5）。
        """
        # 非 MySQL（如测试用 SQLite）无表注释能力，跳过。
        if self.engine.dialect.name != "mysql":
            return

        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        preparer = self.engine.dialect.identifier_preparer

        for table_name, table in self.metadata.tables.items():
            # 仅处理已存在的表（新建表 create_all 已带注释）。
            if table_name not in existing_tables:
                continue
            desired = (table.comment or "").strip()
            if not desired:
                # 模型未声明表注释，不做处理（不清空已有注释）。
                continue
            # 读取数据库现有表注释，一致则跳过（幂等，避免无谓 DDL）。
            current = self._get_table_comment(table_name)
            if current == desired:
                continue
            # 注释内容经参数化绑定，表名经预处理器加引号，防注入（规范 16）。
            quoted_table = preparer.quote(table_name)
            ddl = text(f"ALTER TABLE {quoted_table} COMMENT = :comment")
            with self.engine.begin() as connection:
                connection.execute(ddl, {"comment": desired})
            logger.info("启动自检：同步表 %s 的注释", table_name)

    def _get_table_comment(self, table_name: str) -> str:
        """读取 MySQL 中指定表的现有表注释（不存在返回空字符串）。

        Args:
            table_name: 表名。

        Returns:
            表注释字符串；查询不到时返回空字符串。
        """
        stmt = text(
            "SELECT table_comment FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :name"
        )
        with self.engine.connect() as connection:
            row = connection.execute(stmt, {"name": table_name}).first()
        return (row[0] or "").strip() if row else ""

    # ------------------------------------------------------------------
    # 步骤 4：补字典 / 初始数据
    # ------------------------------------------------------------------
    def _seed_initial_data(self) -> int:
        """回调已注册的字典 / 初始数据初始化钩子，补齐枚举字典与初始数据。

        钩子由字典服务（任务 2.14）注册，须自身保证「存在则跳过」（只增不改），
        以满足幂等与历史数据安全。未注册任何钩子时为空操作，返回 0。

        Returns:
            本次所有钩子新增的记录总条数。
        """
        if not _DICT_INITIALIZERS:
            return 0
        total = 0
        # 在单一事务性会话中依次执行各钩子，整体提交 / 回滚
        with session_scope() as session:
            for initializer in _DICT_INITIALIZERS:
                count = initializer(session)
                total += int(count or 0)
        if total:
            logger.info("启动自检：补充字典 / 初始数据 %d 条", total)
        return total


# ----------------------------------------------------------------------
# 模块级入口函数（供 backend 的 lifespan 调用 —— 任务 4.1）
# ----------------------------------------------------------------------
@with_db_retry()
def run_migration(engine: Engine | None = None) -> MigrationResult:
    """执行启动自检迁移（建表 / 补字段 / 补字典），带连接失败重试。

    迁移操作幂等（先比对后变更），故重试安全。供 backend 的 ``lifespan``
    启动流程调用；其余服务仅连接数据库、不调用本函数（避免并发迁移冲突）。

    Args:
        engine: 目标数据库引擎；None 时取进程内单例引擎。

    Returns:
        ``MigrationResult``：本次迁移产生的变更结果。
    """
    return SchemaMigrator(engine=engine).run()


def init_database(engine: Engine | None = None) -> MigrationResult:
    """数据库启动自检初始化入口（``run_migration`` 的语义化别名）。

    backend 在 ``_bootstrap.py`` 的 ``lifespan`` 启动阶段调用本函数完成
    建表 / 补字段 / 补字典自检。

    Args:
        engine: 目标数据库引擎；None 时取进程内单例引擎。

    Returns:
        ``MigrationResult``：本次迁移产生的变更结果。
    """
    return run_migration(engine=engine)


__all__ = [
    "SchemaMigrator",
    "MigrationResult",
    "run_migration",
    "init_database",
    "register_dict_initializer",
    "clear_dict_initializers",
]
