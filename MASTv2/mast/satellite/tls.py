"""证书公钥钉值：SubjectPublicKeyInfo 的 SHA-256（契约 §5.1）。

卫星钉的是 MAST LAN 证书的**公钥**，不是证书本身：IP 变化时证书会重签，只要
复用原私钥，钉值就不变。``mast.net.tls`` 提供同名函数时优先用它，两边算出来必须一致
（测试里核对）。
"""

from __future__ import annotations

import hashlib
import os
import socket
import ssl
from pathlib import Path


def _load_cert(data: bytes):
    from cryptography import x509

    if b"-----BEGIN" in data:
        return x509.load_pem_x509_certificate(data)
    return x509.load_der_x509_certificate(data)


def spki_sha256(cert: str | os.PathLike | bytes) -> str:
    """证书（PEM/DER 字节，或 PEM/DER 文件路径）→ 小写 hex，64 个字符。

    优先用 ``mast.net.tls.spki_sha256_of``（与 LAN 证书的签发方同一份实现）；没有就自己算。
    """
    data = cert if isinstance(cert, (bytes, bytearray)) else Path(cert).read_bytes()
    try:
        from mast.net.tls import spki_sha256_of as _ext
    except Exception:  # noqa: BLE001 — 冻结包或旧树里没有就自己算
        _ext = None
    if _ext is not None:
        try:
            return str(_ext(bytes(data))).strip().lower()
        except Exception:  # noqa: BLE001
            pass
    return spki_sha256_local(bytes(data))


def spki_sha256_local(data: bytes) -> str:
    from cryptography.hazmat.primitives import serialization

    pub = _load_cert(data).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(pub).hexdigest()


def probe_server_cert(host: str, port: int, *, timeout: float = 10.0) -> bytes:
    """不校验地拿到服务端证书（DER）。只用来算钉值，拿到之后再用它建一个**只信任这张证书**
    的 TLS 上下文去真正连接 —— 令牌只在第二次（已校验的）连接里发出。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        with ctx.wrap_socket(s, server_hostname=host) as ss:
            der = ss.getpeercert(binary_form=True)
    if not der:
        raise ssl.SSLError("服务端没有给证书")
    return der


def pinned_context(der_cert: bytes) -> ssl.SSLContext:
    """只信任这一张（自签）证书的客户端上下文；不校验主机名（契约 §5.1）。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=der_cert)
    return ctx


__all__ = ["spki_sha256", "spki_sha256_local", "probe_server_cert", "pinned_context"]
