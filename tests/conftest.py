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

# ---------------------------------------------------------------------------
# 写测试的准入条件：新增用例前先过这三问，三条都答不上来就别写。
#
# 要防的那种测试有个名字——**change detector test**：只在你改代码时失败，而不是
# 在行为改变时失败。它描述的不是"系统应该怎样"，而是"我刚才把代码改成了这样"。
# 半年后没人说得清它在保护什么，只能一条条回溯，最后变成不敢动的垃圾。
#
#   1. 这个断言能翻译成一句用户或协议可感知的话吗？
#      反例："设置窗口里没有 input_mode_combo" —— 翻译不出来，删。
#   2. 换一种实现写法、行为完全不变，它会变红吗？
#      反例："下拉框第 0 项的数据是空串" —— 重排一下选项就红，用户看不出区别，删。
#   3. 删掉它，会有哪一类 bug 静默溜过？说不出具体场景就删。
#
# 正例对照：配置里的空串必须活着穿过校验（`if not value` 会把它吃掉）；
# 空值配置不得覆盖用户在手机端的临时选择。这两条都能翻译成行为，也都真会踩。
#
# 视觉/外观类改动（图标形状、间距、颜色）不要用 DOM 或 CSS 断言去"守护"——
# 那种断言换个画法就会红，而真正画歪了它又拦不住。老老实实截图人工核对。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 服务器类 fixture 的两条约定（2026-11 全量从 207s 砍到 103s 时立的，别倒退回去）
#
#   1. **保持函数作用域，不要为了省时间改成 module/session 级。**
#      test_backend / test_dispatcher / test_e2ee_server 里的服务器是进程级单例
#      （set_bridge / set_secure_channel / push_config / 审批注册表全是模块全局），
#      用例之间正是靠"每条重启一次"拿到干净状态。改成 module 级等于用隔离性换速度，
#      一旦某条用例漏清队列，失败会以"后面某条毫不相干的用例红了"的形式出现——
#      那是拿最贵的调试成本去换最便宜的墙钟时间。真要合并，先把状态显式化
#      （把 bridge / sc / registry 都变成 fixture 显式重置），顺序反了必踩。
#
#   2. **收尾不要用 time.sleep() 兜底，用 shutdown_server()。**
#      曾经的 sleep(0.3) 是"端口来不及释放"的保险，实测 40 轮连续启停去掉它 0 失败
#      （端口本来就每轮换新，见 get_test_port）；而它让 80 条用例白赔 24 秒。
#      真正需要确认的是"线程退了没有"，那是 shutdown_server() 干的事。
#
# 全量就这么跑：``uv run pytest``（并行度等默认值配在 pyproject 的 [tool.pytest.ini_options]，
# 22 核机器实测 103s → 25s）。端口由 _worker_port_offset() 按 worker 分段，不用手工改代码。
# 只有三种情况加 ``-n0``：跑单个文件、要看真实执行顺序、要下断点（xdist 下断点不命中）。
# ---------------------------------------------------------------------------


import base64
import json
import os
import socket
import time
import urllib.error
import urllib.request
from hashlib import blake2b

import pytest

# Qt 测试一律跑在 offscreen 平台：窗口只在内存里，屏幕上什么都不弹。
#
# 起因：test_dashboard_mode.py 有一批用例必须 show() 之后才有真实几何（isVisible /
# 尺寸断言），而 show() 会真的在桌面弹出主窗口——一个文件三十来个用例，跑一遍就像
# 有人反复开关窗口。offscreen 平台下 isVisible()、布局几何、字体度量语义与真平台
# 一致，断言照常成立，但不会创建任何可见窗口。
#
# 必须早于 QApplication 构造：Qt 只在那一刻读这个变量，而 conftest 在收集阶段就被
# 导入，早于任何 fixture 创建 QApplication。
#
# 用 setdefault 而非直接赋值：偶尔要肉眼看一眼界面时，先设 QT_QPA_PLATFORM=windows
# 覆盖即可，这里不会把你的选择盖掉。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nacl.bindings import crypto_scalarmult
from nacl.public import PrivateKey, PublicKey, SealedBox

from phonemic.tunnel.crypto import create_provider
from phonemic.tunnel.frame import decode as frame_decode
from phonemic.tunnel.frame import encode as frame_encode

_HOST = "127.0.0.1"


def _worker_port_offset() -> int:
    """并行跑（pytest-xdist）时给每个 worker 分一段独立端口。

    每个 worker 是独立进程，各自的 ``_next_port`` 都从同一个数开始往上涨 ⇒
    不打散的话四个 worker 会同时从 20000 发起探测，抢同一批端口。
    ``PYTEST_XDIST_WORKER`` 是 gw0 / gw1 / gw2 / gw3，每段留 1000 个够用；
    不在 xdist 下时环境变量不存在，偏移为 0，行为与从前完全一致。
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    if worker.startswith("gw") and worker[2:].isdigit():
        return int(worker[2:]) * 1000
    return 0


# 起始端口选在动态端口池（1024–14999）之外，避免被系统临时分配
_START_PORT = 20000 + _worker_port_offset()

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


def wait_for_server_ready(
    host: str,
    port: int,
    secret_path: str = "",
    timeout: float = 5.0,
    ok_statuses: tuple = (200, 404),
) -> bool:
    """等服务器真的能应答 HTTP，返回是否就绪。

    为什么不是「一条 urlopen 失败就 sleep(0.1) 再重试」：
    ``start_server()`` 返回后，端口 67ms 就已经能连上（实测），但 uvicorn 开始
    accept 之前还有一个几毫秒的缝。第一枪落在这条缝里时，请求会**一直挂着**
    直到客户端超时——旧实现给的是 ``timeout=0.5``，于是每条用例的 setup 稳定
    吃掉 0.61s，而服务器其实 67ms 就起好了。80 条用例走这条路，纯白赔 40 秒。

    这里改成「先探 TCP（50ms 超时）→ 再发一次 HTTP（50ms 超时）→ 间隙只歇
    5ms」，同一环境实测 66~104ms 就绪，不再有 0.5s 的悬崖。
    """
    prefix = f"/{secret_path}" if secret_path else ""
    url = f"http://{host}:{port}{prefix}/"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.05)
            if sock.connect_ex((host, port)) != 0:
                time.sleep(0.005)
                continue
        try:
            with urllib.request.urlopen(url, timeout=0.05) as resp:
                if resp.status in ok_statuses:
                    return True
        except urllib.error.HTTPError as e:
            if e.code in ok_statuses:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.005)
    return False


def shutdown_server() -> None:
    """停服，并确认后台线程真的退出了。

    ``stop_server()`` 里的 join 有 5s 上限，超时只是"放生"——线程还活着、端口
    还占着，后面的用例就会莫名其妙变慢或撞端口（本文件开头那段 10048 的血泪
    就是这么攒出来的）。把这件事变成一条明确失败，比让它悄悄拖垮后面几百条
    用例划算得多。
    """
    from phonemic.server import api as api_mod

    thread = api_mod._server_thread
    api_mod.stop_server()
    if thread is not None and thread.is_alive():
        pytest.fail("服务端线程未在 join 超时内退出：有连接卡住了 graceful shutdown")


class PhoneSimulator:
    """模拟手机端（URL fragment 认证 / TOFU 重连路径），用 PyNaCl 代替 libsodium.js。

    与 JS 端 SecureClient / CryptoProvider 行为一致：
    - auth：`SealedBox(pc_public)` 密封 `{"algo","pk"}`（algo 不明文传输）
    - 会话密钥：ECDH + blake2b(32) 派生，交给 CryptoProvider
    - 防重放 seq 由 Provider 在加密层承载，不进应用层 JSON

    加密永远开启，因此没有明文分支（e2ee-always-on-design.md）。
    TOFU **首次**连接（明文 auth + 审批）的模拟见 `test_e2ee_server.py`。
    """

    def __init__(self, pc_public_key_b64: str, algo: str = "xsalsa20"):
        self._algo = algo
        pc_pub_bytes = base64.urlsafe_b64decode(pc_public_key_b64 + "==")
        self._pc_public = PublicKey(pc_pub_bytes)
        self._phone_private = PrivateKey.generate()
        self._phone_public = self._phone_private.public_key
        shared = crypto_scalarmult(bytes(self._phone_private), pc_pub_bytes)
        session_key = blake2b(shared, digest_size=32).digest()
        self._provider = create_provider(algo, session_key)

    def make_auth(self, algo: str = None) -> dict:
        """密封 {"algo","pk"}。

        每次握手（= 新连接/新会话）开始时归零 seq 计数器，与服务端对齐。
        """
        algo = algo or self._algo
        self._provider.reset()
        inner = json.dumps({
            "algo": algo,
            "pk": base64.urlsafe_b64encode(bytes(self._phone_public)).decode().rstrip("="),
        }).encode("utf-8")
        # msgpack 的 bin 类型承载密封 blob，不再 base64 包裹
        return {"type": "auth", "data": SealedBox(self._pc_public).encrypt(inner)}

    def encrypt(self, msg: dict) -> bytes:
        # 产出线上字节：整帧加密，无外层信封（seq 由 Provider 自动打上）
        return bytes(self._provider.encrypt(frame_encode(msg)))

    def decrypt(self, raw: bytes) -> dict:
        # 还原应用层报文：整帧解密（重放/乱序/篡改由 Provider 抛错）
        return frame_decode(self._provider.decrypt(raw))

    def handshake(self, ws) -> dict:
        """在一条连接上跑完三步握手，返回挑战帧内容。

        auth_challenge 是下行首帧（服务端 tx_seq=0，整帧加密），必须在此解密消费，
        手机端 rx 计数才能与服务端后续下行帧（config 等）对齐。
        """
        ws.send(frame_encode(self.make_auth()))
        challenge = self.decrypt(ws.recv(timeout=5))
        assert challenge["type"] == "auth_challenge", challenge
        ws.send(self.encrypt({"type": "auth_proof", "nonce": challenge["nonce"]}))
        return challenge
