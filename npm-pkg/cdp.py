#!/usr/bin/env python3
"""
纯 Python 实现的最小 CDP（Chrome DevTools Protocol）客户端。

只依赖标准库：通过 ADB 端口转发把手机 WebView 的 devtools socket 暴露到
本机 127.0.0.1:9222，然后用裸 socket 完成 WebSocket 握手与收发，
用于 Page.navigate（导航）与 Runtime.evaluate（执行 JS）。

不使用 node / ws，也不需要 SSL（CDP 走 ws:// 明文）。
"""

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request

CDP_PORT = 9222


def _run(cmd, timeout=10):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def run_adb(cmd, timeout=10):
    return _run(f"adb {cmd}", timeout=timeout)


def find_webview(timeout=5):
    """从 /proc/net/unix 中找到当前 WebView 的 devtools socket PID。"""
    for _ in range(timeout):
        r = run_adb("shell cat /proc/net/unix", timeout=5)
        for line in r.stdout.split("\n"):
            if "@webview_devtools_remote_" in line:
                pid = line.strip().split("_")[-1]
                if pid.isdigit():
                    return pid
        time.sleep(1)
    return None


def _http_get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


class CDPError(Exception):
    pass


class WebSocketClient:
    """极简 WebSocket 客户端，仅支持 CDP 需要的文本帧收发。"""

    def __init__(self, ws_url, timeout=15):
        # ws_url 形如 ws://localhost:9222/devtools/page/XXXX
        assert ws_url.startswith("ws://")
        rest = ws_url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.host = host or "localhost"
        self.port = int(port or "80")
        self.path = "/" + path
        self.timeout = timeout
        self.sock = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        # 读取握手响应头（以 \r\n\r\n 结束）
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("WebSocket 握手失败：连接被关闭")
            data += chunk
        if b"101" not in data.split(b"\r\n", 1)[0]:
            raise CDPError(f"WebSocket 握手失败：{data.split(chr(13).encode())[0]!r}")

    def send_text(self, text):
        payload = text.encode("utf-8")
        header = bytearray()
        header.append(0x81)  # FIN + text opcode
        mask_bit = 0x80
        length = len(payload)
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack(">Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise CDPError("连接在读取过程中被关闭")
            buf += chunk
        return buf

    def recv_message(self):
        """读取一条完整消息（处理分片、忽略 ping/pong），返回文本。"""
        message = b""
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # close
                raise CDPError("服务端关闭了 WebSocket")
            if opcode == 0x9:  # ping -> 回 pong
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            # 0x1 text / 0x2 binary / 0x0 continuation
            message += payload
            if fin:
                return message.decode("utf-8", "replace")

    def _send_control(self, opcode, payload=b""):
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        header.append(0x80 | len(payload))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def close(self):
        try:
            if self.sock:
                self._send_control(0x8)
                self.sock.close()
        except Exception:
            pass
        finally:
            self.sock = None


class CDPSession:
    """管理 ADB 转发 + WebSocket，提供 navigate / evaluate。"""

    def __init__(self):
        self.ws_url = None
        self.current_url = None

    def setup(self):
        """建立到当前 WebView 的 CDP 连接信息（幂等，可重复调用刷新）。"""
        r = run_adb("devices")
        devices = [l for l in r.stdout.strip().split("\n")[1:] if l.strip() and "device" in l]
        if not devices:
            raise CDPError("未检测到已连接的设备（adb devices 为空）")

        pid = find_webview(timeout=5)
        if not pid:
            raise CDPError("未找到 WebView（请确保 App 已打开 H5 页面并开启了 WebView 调试）")

        run_adb(f"forward tcp:{CDP_PORT} localabstract:webview_devtools_remote_{pid}")
        time.sleep(0.3)

        try:
            pages = _http_get_json(f"http://127.0.0.1:{CDP_PORT}/json")
        except Exception as e:
            raise CDPError(f"读取 CDP 页面列表失败：{e}")
        if not pages:
            raise CDPError("CDP 未返回任何页面")

        # 优先选择类型为 page 的目标
        page_targets = [p for p in pages if p.get("type") == "page"] or pages
        target = page_targets[0]
        self.ws_url = target.get("webSocketDebuggerUrl")
        self.current_url = target.get("url", "")
        if not self.ws_url:
            raise CDPError("目标页面没有 webSocketDebuggerUrl")
        return self.ws_url

    def _command(self, method, params=None):
        if not self.ws_url:
            self.setup()
        ws = WebSocketClient(self.ws_url)
        ws.connect()
        try:
            ws.send_text(json.dumps({"id": 1, "method": method, "params": params or {}}))
            deadline = time.time() + 15
            while time.time() < deadline:
                msg = json.loads(ws.recv_message())
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise CDPError(msg["error"].get("message", "CDP error"))
                    return msg.get("result", {})
            raise CDPError("CDP 命令超时")
        finally:
            ws.close()

    def command(self, method, params=None):
        """执行命令，连接失效时自动重建一次。"""
        try:
            return self._command(method, params)
        except (OSError, CDPError):
            # WebView 可能已重建（PID 变化），刷新后重试一次
            self.setup()
            return self._command(method, params)

    def navigate(self, url):
        return self.command("Page.navigate", {"url": url})

    def evaluate(self, expression):
        return self.command("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "allowUnsafeEvalBlocklistBypass": True,
        })
