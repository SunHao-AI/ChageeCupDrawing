"""服务启动入口：uvicorn 单进程，端口与被监听地址由 CUPDRAW_BIND/CUPDRAW_PORT 控制。

局域网模式（CUPDRAW_BIND=0.0.0.0）下打印本机局域网地址，方便手机/平板直接访问；
写接口仍需会话 Cookie + X-Requested-With 头，令牌 Cookie 由首次 GET 自动下发。
"""

from __future__ import annotations

import socket

import uvicorn

from app.config import Settings
from app.main import app


def lan_addresses() -> list[str]:
    """尽力枚举本机 IPv4。UDP connect 不发包，只用来选默认路由对应的地址。"""
    addresses: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        addresses.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in addresses and not ip.startswith("127."):
                addresses.append(ip)
    except OSError:
        pass
    return addresses


def main() -> None:
    settings = Settings()
    host, port = settings.bind_host, settings.port
    print(f"CupDraw 服务启动: http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}")
    if host == "0.0.0.0":
        print("局域网模式已开启，同一 Wi-Fi 下的手机/平板可访问：")
        for ip in lan_addresses() or ["<本机 IP>"]:
            print(f"  http://{ip}:{port}")
        print("提示：仅在可信网络开启；写接口需要会话 Cookie + X-Requested-With 头。")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
