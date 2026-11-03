"""
netguard.py — 出站地址安全校验（防 SSRF）

背景：`/api/configure` 允许客户端自带 `base_url`（自定义模型）。服务端随后会用
它发起真实的出站请求 —— 如果不对目标做校验，任何能访问本服务的人都能让服务器
去请求内网地址（云元数据 `169.254.169.254`、`127.0.0.1` 上的管理接口、
`10.0.0.0/8` 的内网服务等），即经典 SSRF。

默认策略（安全优先）：
  - 只允许 `http` / `https`；
  - 目标主机解析出的**任意**地址若属于回环 / 私网 / 链路本地 / 保留 / 组播 /
    未指定地址段，一律拒绝；
  - `localhost` 等本机主机名直接拒绝；
  - 域名解析失败按"拒绝"处理（fail-closed），避免"现在解析不了、连接时才解析到内网"。

本地部署（Ollama / vLLM / LM Studio 等跑在 `127.0.0.1`）确实需要放开：
在 `.env` 里设置 `CUSTOM_BASE_URL_ALLOW_PRIVATE=1` 即可整体豁免本模块。

限制说明：
  - 域名解析失败按"放行 + warning"处理（配置阶段端点可能暂时不可达，fail-closed 会
    让用户连保存配置都做不到）；但**单标签主机名**（不含点号，如 `http://intranet`、
    `http://metadata`）一律拒绝 —— 这类名字靠本地 search domain 解析，是 SSRF 的常见入口。
  - 本模块在**配置时**校验，运行期由 OpenAI SDK 重新解析域名。若攻击者控制 DNS 并做
    DNS rebinding，仍可能绕过 —— 生产部署应叠加网络层出站策略（只允许访问白名单域名 /
    禁止访问元数据网段）。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from muti_llm_agent.config import settings
from muti_llm_agent.logger import log

# 本机主机名（IP 字面量（含 0.0.0.0 / ::）由 _resolve + _blocked 判定，无需列在这里）
BLOCKED_HOSTNAMES = {"localhost", "localhost.", "localhost.localdomain",
                     "ip6-localhost", "ip6-loopback"}


class UnsafeUrlError(ValueError):
    """出站地址被安全策略拒绝（可安全地展示给用户）。"""


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """是否为不可出站的目标地址。"""
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def _resolve(host: str) -> list:
    """解析主机名到地址列表。

    - IP 字面量：直接返回，不做 DNS；
    - 单标签主机名（无点号）：直接拒绝（依赖本地 search domain，是 SSRF 常见入口）；
    - 解析失败：返回空列表（调用方按"放行 + warning"处理，见模块 docstring）。
    """
    # 直接是 IP 字面量时无需 DNS
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    if "." not in host:
        raise UnsafeUrlError(
            f"Base URL 的主机「{host}」不是合法域名（缺少点号）。"
            "请填写完整域名或公网 IP。")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        log("warning", "netguard.unresolved",
            "Base URL 主机暂时无法解析，按放行处理（配置阶段可能离线）",
            host=host, error=str(exc))
        return []
    addrs = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return addrs


def check_outbound_url(url: str) -> str:
    """校验出站 base_url；通过返回原值，不通过抛 UnsafeUrlError。

    豁免：`.env` 设置 `CUSTOM_BASE_URL_ALLOW_PRIVATE=1` 时直接放行
    （本地部署自建模型服务需要访问 127.0.0.1 / 内网地址）。
    """
    raw = (url or "").strip()
    if not raw:
        raise UnsafeUrlError("Base URL 不能为空。")
    if settings.CUSTOM_BASE_URL_ALLOW_PRIVATE:
        return raw

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(
            f"Base URL 协议不受支持：{parsed.scheme or '（空）'}（只允许 http / https）。")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise UnsafeUrlError("Base URL 缺少主机名。")
    if host in BLOCKED_HOSTNAMES:
        raise UnsafeUrlError(
            f"出于安全考虑，禁止把 Base URL 指向本机地址「{host}」。"
            "若确需访问本机模型服务，请在 .env 设置 CUSTOM_BASE_URL_ALLOW_PRIVATE=1。")

    for ip in _resolve(host):
        if _blocked(ip):
            log("warning", "netguard.blocked",
                "拒绝指向内网 / 保留地址的 Base URL",
                host=host, ip=str(ip), allow_private=False)
            raise UnsafeUrlError(
                f"出于安全考虑，禁止把 Base URL 指向内网或保留地址（{host} → {ip}）。"
                "若确需访问内网模型服务，请在 .env 设置 CUSTOM_BASE_URL_ALLOW_PRIVATE=1。")
    return raw
