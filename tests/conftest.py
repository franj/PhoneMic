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

import base64
import json
import socket
from hashlib import blake2b

from nacl.bindings import crypto_scalarmult
from nacl.public import PrivateKey, PublicKey, SealedBox

from phonemic.tunnel.crypto import create_provider
from phonemic.tunnel.frame import decode as frame_decode
from phonemic.tunnel.frame import encode as frame_encode

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
