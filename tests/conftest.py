"""
测试共享辅助（端口分配）。

为什么要做这件事：
- 本机 Windows 动态端口（临时端口）范围是 **1024–14999**（`netsh int ipv4 show
  dynamicport tcp` 实测：启动端口 1024，端口数 13977）。原先三个测试文件各自从
  8880 / 9500 / 9900 递增分配端口，全都落在这个池子里。
- 单跑某个测试时只连一两次，看不出问题；全量跑会产生几百条 WebSocket/HTTP 出向
  连接，操作系统随时可能把池内某个端口分配给客户端 socket，等服务端再去 bind 就
  报 `[WinError 10048] 通常每个套接字地址只允许使用一次`。
- 另外递增计数器是"盲发"的：只要测试数量涨到某一点，就会撞上常驻进程监听的端口
  （例如本机 9910 被 Code.exe 占用），表现为"单独跑通过、全量跑失败"。
"""

import socket

_HOST = "127.0.0.1"

# 起始端口选在动态端口池（1024–14999）之外，避免被系统临时分配
_START_PORT = 20000

_next_port = _START_PORT


def _is_port_free(port: int) -> bool:
    """尝试独占绑定该端口，成功即视为可用。

    刻意**不设置** SO_REUSEADDR：Windows 上该选项允许绑定到已被监听或处于
    TIME_WAIT 的端口，会让探测误判为可用，问题推迟到服务端启动时才以
    [WinError 10048] 暴露。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((_HOST, port))
        except OSError:
            return False
    return True


def get_test_port(max_tries: int = 500) -> int:
    """返回一个当前可绑定的空闲端口。

    在起始端口之上递增，并逐个实测绑定，跳过被其他进程监听或处于 TIME_WAIT 的
    端口。分配过的端口不再复用，避免相邻测试互相干扰。
    """
    global _next_port
    for _ in range(max_tries):
        port = _next_port
        _next_port += 1
        if _is_port_free(port):
            return port
    raise RuntimeError(
        f"在 {max_tries} 次尝试内未找到空闲端口（起始端口 {_START_PORT}）"
    )
