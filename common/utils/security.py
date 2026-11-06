# -*- coding: utf-8 -*-
"""
安全模块（common.utils.security）
================================
本文件用途：为「拼多多自动回复」系统提供统一的安全基础能力，供 backend /
websocket / scheduler 等各服务复用，满足需求 1（用户认证与会话）与需求 22.2
（个人设置修改密码校验）。主要包含两类能力：

1. 密码哈希与校验（基于 passlib[bcrypt]）：
   - hash_password(plain)            生成密码哈希（不可逆）；
   - verify_password(plain, hashed)  校验明文与哈希是否匹配。
   - 任何函数均不返回明文或哈希之外的敏感信息，密码哈希值不等于明文
     （需求 1.6：使用哈希存储且不在响应中返回明文/哈希）。
   - 采用 ``bcrypt_sha256`` 方案：先对明文做 SHA-256 预哈希再交给 bcrypt，
     规避原生 bcrypt 仅取前 72 字节的截断问题，保证「不同密码必然得到不同
     校验结果」对任意长度输入均成立。

2. JWT 令牌签发与校验（基于 PyJWT）：
   - create_access_token(subject, expires_delta, extra_claims)  签发访问令牌；
   - decode_access_token(token)      校验并解码令牌，无效/过期返回 None；
   - 密钥、算法、默认过期分钟数均来自 ``get_settings()``（common.core.config）。

时区一致性（开发规范 17、需求 24.8）：
- 令牌过期时间的计算以北京时间（Asia/Shanghai, UTC+8）为口径，使用带时区的
  ``datetime`` 进行运算；PyJWT 在编码时按带时区时间换算为标准 Unix 时间戳，
  校验时同样以 UTC 时间戳比较，时区一致、不会产生 ±8 小时偏差。

令牌失效机制（需求 1.5）：
- 本模块在令牌载荷中预留 ``jti``（令牌唯一标识）与 ``ver``（令牌版本号）字段，
  作为「主动失效 / 全局失效」的基础。完整的失效判定（如黑名单比对、用户令牌
  版本比对）由 backend 服务结合数据库实现，本模块仅负责签发与校验的基础。
"""
from __future__ import annotations
import bcrypt
if not hasattr(bcrypt, "__about__"):
    class _About:
        __version__ = getattr(bcrypt, "__version__", "4.0.1")
    bcrypt.__about__ = _About()
_orig_hashpw = bcrypt.hashpw
def _safe_hashpw(password, salt):
    if isinstance(password, bytes) and len(password) > 72:
        password = password[:72]
    return _orig_hashpw(password, salt)
bcrypt.hashpw = _safe_hashpw


import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Union

import jwt
from passlib.context import CryptContext

from common.core.config import Settings, get_settings
from common.utils.time_utils import now_beijing

# ----------------------------------------------------------------------
# 密码哈希上下文
# ----------------------------------------------------------------------
# 采用 bcrypt_sha256：底层依赖 bcrypt（即 passlib[bcrypt]），但先对明文做
# SHA-256 预哈希，从而：
#   1）规避原生 bcrypt 仅截取前 72 字节的限制，支持任意长度密码；
#   2）保证「同一密码校验通过、不同密码校验失败」对任意输入均成立。
# deprecated="auto" 允许未来平滑升级哈希算法。
_pwd_context = CryptContext(schemes=["bcrypt_sha256"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """生成密码哈希（不可逆）。

    Args:
        plain_password: 用户明文密码。

    Returns:
        密码哈希字符串（含算法标识与盐值），其值不等于明文，且无法由哈希反推明文。
    """
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验明文密码与已存储哈希是否匹配。

    Args:
        plain_password: 待校验的明文密码。
        hashed_password: 已存储的密码哈希。

    Returns:
        匹配返回 True，否则返回 False。当哈希格式非法时同样返回 False，
        不抛出异常，避免将内部细节暴露给调用方。
    """
    try:
        return _pwd_context.verify(plain_password, hashed_password)
    except (ValueError, TypeError):
        # 哈希格式非法或类型错误时一律视为校验不通过，保持调用方逻辑简单安全
        return False


# ----------------------------------------------------------------------
# JWT 令牌签发与校验
# ----------------------------------------------------------------------
def create_access_token(
    subject: Union[str, int, Dict[str, Any]],
    expires_delta: Optional[timedelta] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
    token_version: int = 0,
    settings: Optional[Settings] = None,
) -> str:
    """签发访问令牌（JWT）。

    令牌载荷固定包含：
    - ``sub``：令牌主体（通常为用户 ID 或用户名），统一序列化为字符串；
    - ``iat``：签发时间（北京时间口径换算的 Unix 时间戳，由 PyJWT 处理）；
    - ``exp``：过期时间（同上）；
    - ``jti``：令牌唯一标识（UUID），用于「主动失效」基础；
    - ``ver``：令牌版本号，用于「按用户全局失效」基础（需求 1.5）。

    Args:
        subject: 令牌主体。可为字符串/整数（作为 ``sub``），或包含 ``sub`` 的字典
            （字典内其余键将一并写入载荷作为自定义声明）。
        expires_delta: 过期时长。缺省时取配置的 ``access_token_expire_minutes`` 分钟。
        extra_claims: 额外自定义声明（如角色、店铺范围等），将合并进载荷。
        token_version: 令牌版本号，默认 0；配合用户表的版本号可实现全局失效。
        settings: 配置实例。缺省取 common 配置；backend 等服务可传入各自配置实例，
            以保证签名密钥/算法一致。

    Returns:
        编码后的 JWT 字符串。
    """
    settings = settings or get_settings()

    # 组装载荷：兼容「字符串/整数主体」与「字典主体」两种入参形式
    if isinstance(subject, dict):
        to_encode: Dict[str, Any] = dict(subject)
        # 主体字典里若已含 sub 则保留，否则置空字符串占位
        sub_value = str(subject.get("sub", ""))
    else:
        to_encode = {}
        sub_value = str(subject)

    # 合并额外自定义声明（在写入保留声明 sub 之前合并）
    if extra_claims:
        to_encode.update(extra_claims)

    # 写入保留声明 sub（置于 extra_claims 之后，确保主体不被 extra_claims 覆盖）
    to_encode["sub"] = sub_value

    # 过期时长缺省取配置值（分钟）
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.access_token_expire_minutes)

    # 以北京时间为口径计算签发/过期时间（带时区，PyJWT 会换算为 Unix 时间戳，
    # 校验时按 UTC 时间戳比较，时区一致、无 ±8 小时偏差）
    issued_at = now_beijing()
    expire_at = issued_at + expires_delta

    to_encode.update(
        {
            "iat": issued_at,
            "exp": expire_at,
            "jti": uuid.uuid4().hex,
            "ver": token_version,
        }
    )

    return jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(
    token: str,
    settings: Optional[Settings] = None,
) -> Optional[Dict[str, Any]]:
    """校验并解码访问令牌。

    令牌有效时返回其载荷（claims 字典，可还原 ``sub`` 等用户身份信息）；
    令牌过期、签名错误或格式非法时返回 ``None``（不抛出异常，调用方据此判定
    「未登录或登录已过期」——需求 1.4）。

    Args:
        token: 待校验的 JWT 字符串。
        settings: 配置实例。缺省取 common 配置；各服务可传入各自配置实例。

    Returns:
        校验通过返回载荷字典；无效或过期返回 ``None``。
    """
    settings = settings or get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError:
        # 过期（ExpiredSignatureError）、签名错误、格式非法等均归一为「无效令牌」
        return None
    return payload
