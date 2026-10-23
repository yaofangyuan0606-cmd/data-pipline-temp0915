"""内网共享：服务监听在哪、同事用什么地址打开、现在的登录方式安不安全。

默认只听 127.0.0.1（大家经 SSH 隧道进来）。要让内网里的同事直接打开，监听 0.0.0.0：
    .venv/bin/python scripts/serve.py 8793 --lan          （或 EMQC_API_HOST=0.0.0.0）
对内网开放时必须开登录：EMQC_AUTH_DISABLED=1（不登录）会被拒绝启动；试用模式（EMQC_AUTH_OPEN=1，填名字就进、不校验密码）
只给警告，建议改成 EMQC_AUTH_OPEN=0 并用 `python -m emqc create-user` 给每个人发账号。
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess

from emqc.config import settings

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def is_lan(host: str | None = None) -> bool:
    return (host or settings.api_host) not in LOOPBACK


def local_ips() -> list[str]:
    """This machine's non-loopback IPv4 addresses (private / tailscale first)."""
    ips: set[str] = set()
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout
        ips.update(x for x in out.split() if ":" not in x)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    ok = []
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not a.is_loopback and not a.is_link_local:
            ok.append(ip)
    # 100.64/10 是 tailscale；10/8、172.16/12、192.168/16 是常见内网段
    rank = lambda ip: (0 if ipaddress.ip_address(ip).is_private else (1 if ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10") else 2), ip)
    return sorted(ok, key=rank)


def auth_mode() -> str:
    if settings.auth_disabled:
        return "none"
    return "open" if settings.auth_open else "password"


AUTH_ZH = {"none": "不登录（任何人都能改）", "open": "试用模式：填名字就进，不校验密码", "password": "账号密码登录"}


def info(port: int | None = None) -> dict:
    port = port or settings.api_port
    lan = is_lan()
    return {"host": settings.api_host, "port": port, "lan": lan, "urls": [f"http://{ip}:{port}" for ip in local_ips()] if lan else [],
            "local_url": f"http://127.0.0.1:{port}", "auth": auth_mode(), "auth_zh": AUTH_ZH[auth_mode()],
            "ssh_tunnel": f"ssh -N -L 1{port}:127.0.0.1:{port} <用户>@<服务器>" if not lan else None}


def check_before_listen(host: str) -> list[str]:
    """Problems with listening on host. Raises SystemExit for the unsafe combination (LAN without login)."""
    if not is_lan(host):
        return []
    if settings.auth_disabled and not settings.allow_lan_without_login:
        raise SystemExit("拒绝启动：要对内网开放（监听 %s），但登录被关掉了（EMQC_AUTH_DISABLED=1），内网里任何人都能改数据、删东西。\n"
                         "去掉 EMQC_AUTH_DISABLED，或者确实要这样就设 EMQC_ALLOW_LAN_WITHOUT_LOGIN=1。" % host)
    warns = []
    if settings.auth_open:
        warns.append("当前是试用登录（填名字就进、不校验密码）。对内网开放建议设 EMQC_AUTH_OPEN=0，再用 `.venv/bin/python -m emqc create-user --username <名字> --role reviewer` 给每人发账号。")
    return warns
