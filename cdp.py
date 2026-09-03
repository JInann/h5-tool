#!/usr/bin/env python3
"""
纯 Python 实现的最小 CDP（Chrome DevTools Protocol）客户端。

只依赖标准库：通过 ADB 端口转发把手机 WebView 的 devtools socket 暴露到
本机 127.0.0.1:9222，然后用裸 socket 完成 WebSocket 握手与收发，
用于 Page.navigate（导航）与 Runtime.evaluate（执行 JS）。

不使用 node / ws，也不需要 SSL（CDP 走 ws:// 明文）。
"""

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.request

CDP_PORT = 9222
CDP_SLOTS = 64           # 多设备时 CDP 端口槽位数量（9222 ~ 9222+63）
DEVICE_SLOT_SEED = "h5tool-device-slot"


def _run(cmd, timeout=10):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def run_adb(cmd, timeout=10, serial=None):
    """执行 adb 命令。serial 非空时指定设备（adb -s <serial>），否则走默认设备。"""
    if serial:
        return _run(f"adb -s {serial} {cmd}", timeout=timeout)
    return _run(f"adb {cmd}", timeout=timeout)


def adb_available():
    """检测 adb 是否安装且可执行（启动/状态接口用，本机一次 30ms 级开销）。

    返回 {"installed": bool, "path": str|None, "version": str|None, "error": str|None}
    installed=False 时 error 给出原因与安装指引要点。
    """
    exe = shutil.which("adb")
    if not exe:
        return {
            "installed": False, "path": None, "version": None,
            "error": "adb 不在 PATH 中（Android 平台工具未安装）。macOS: brew install android-platform-tools",
        }
    try:
        r = _run(f"adb version", timeout=5)
    except Exception as e:
        return {"installed": False, "path": exe, "version": None,
                "error": f"adb version 执行失败：{e}"}
    if r.returncode != 0:
        return {"installed": False, "path": exe, "version": None,
                "error": (r.stderr or r.stdout or "adb version 异常").strip()[:200]}
    first = (r.stdout or "").strip().splitlines()[0] if r.stdout else ""
    return {"installed": True, "path": exe, "version": first[:120], "error": None}


def device_slot(serial, used_slots=()):
    """按 serial 计算稳定端口槽位（0 ~ CDP_SLOTS-1）。

    用 hash 固定序列，避免 adb devices 顺序抖动导致端口漂移；
    used_slots 里已有槽位（其它在线设备占用的）会被线性探测跳过，保证无冲突。
    """
    digest = hashlib.md5((DEVICE_SLOT_SEED + serial).encode()).hexdigest()
    slot = int(digest[:4], 16) % CDP_SLOTS
    used = set(used_slots or ())
    while slot in used:
        slot = (slot + 1) % CDP_SLOTS
    return slot


def cdp_port_for(serial, used_slots=()):
    """该设备对应的 CDP 转发端口（9222 + 槽位）。"""
    return CDP_PORT + device_slot(serial, used_slots=used_slots)


def resolve_port(serial, base, serials):
    """给 serial 分配一个端口（base + 槽位），避开其它在线设备已占用的槽位。

    serials: 当前所有在线设备的 serial 列表（含自己）。
    返回端口号。冲突时线性探测顺延，保证同机多设备端口不重叠。
    """
    others = [s for s in serials if s != serial]
    used = {device_slot(s) for s in others}
    return base + device_slot(serial, used_slots=used)


def list_devices():
    """解析 `adb devices -l`，返回 [{serial, model, product, state}]（按连接顺序）。"""
    r = run_adb("devices -l")
    out = []
    for line in r.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        info = {"serial": serial, "state": state, "model": None, "product": None}
        for kv in parts[2:]:
            if ":" in kv:
                k, v = kv.split(":", 1)
                if k in ("model", "product"):
                    info[k] = v
        out.append(info)
    return out


def default_serial():
    """返回默认设备 serial（adb devices 第一个处于 device 状态的），没有则 None。"""
    for d in list_devices():
        if d["state"] == "device":
            return d["serial"]
    return None


# WebView socket 名缓存：WiFi adb 下 cat /proc/net/unix 走网络很慢（2s+），
# 而 socket 名在 WebView 存活期间基本不变，缓存可让 status 轮询秒回。
# 仅在找到时缓存；找不到不缓存，避免误判。
_sock_cache = {}          # serial -> (sock_name, ts)
_SOCK_TTL = 8.0


def find_webview(serial, timeout=5):
    """从指定设备的 /proc/net/unix 中找到当前 WebView 的 devtools socket 名。

    兼容两种命名（部分厂商浏览器会加前缀）：
      - @webview_devtools_remote_<pid>         标准 WebView
      - @browser_webview_devtools_remote_<pid> 小米浏览器等
    返回去掉 @ 的完整 socket 名（如 webview_devtools_remote_17241），
    供 adb forward localabstract 使用；找不到返回 None。
    """
    now = time.time()
    hit = _sock_cache.get(serial)
    if hit and now - hit[1] < _SOCK_TTL:
        return hit[0]
    for _ in range(timeout):
        try:
            r = run_adb("shell cat /proc/net/unix", timeout=5, serial=serial)
        except Exception:
            # 设备/ADB 临时不可用（如超时），继续轮询
            time.sleep(0.5)
            continue
        for line in r.stdout.split("\n"):
            if "webview_devtools_remote_" in line and "@" in line:
                name = line.split("@", 1)[-1].strip()
                # 形如 webview_devtools_remote_17241 或 browser_webview_devtools_remote_15802
                if name.split("_")[-1].isdigit():
                    _sock_cache[serial] = (name, now)
                    return name
        time.sleep(0.5)
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
    """按设备管理 ADB 转发 + WebSocket，提供 navigate / evaluate。

    多设备支持：setup(serial) 为每台设备建立独立端口转发（9222+槽位），
    会话状态按 serial 缓存。
    """

    def __init__(self):
        self._sessions = {}   # serial -> {"ws_url":..., "current_url":...}

    def setup(self, serial=None):
        """建立指定设备（默认第一台）到其 WebView 的 CDP 连接信息（幂等，可重复调用刷新）。

        返回 (serial, ws_url)。若传入的 serial 未连接，自动回退到默认设备。
        """
        if serial is None:
            serial = default_serial()
        if serial is None:
            raise CDPError("未检测到已连接的设备（adb devices 为空）")

        sock_name = find_webview(serial, timeout=5)
        if not sock_name:
            raise CDPError("未找到 WebView（请确保 App 已打开 H5 页面并开启了 WebView 调试）")

        serials = [d["serial"] for d in list_devices()]
        port = resolve_port(serial, CDP_PORT, serials)
        run_adb(f"forward tcp:{port} localabstract:{sock_name}",
                serial=serial)
        time.sleep(0.3)

        try:
            pages = _http_get_json(f"http://127.0.0.1:{port}/json")
        except Exception as e:
            raise CDPError(f"读取 CDP 页面列表失败：{e}")
        if not pages:
            raise CDPError("CDP 未返回任何页面")

        # 优先选择类型为 page 的目标
        page_targets = [p for p in pages if p.get("type") == "page"] or pages
        target = page_targets[0]
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CDPError("目标页面没有 webSocketDebuggerUrl")
        self._sessions[serial] = {
            "ws_url": ws_url,
            "current_url": target.get("url", ""),
        }
        return serial, ws_url

    def _session(self, serial):
        """取已建立的会话；未建立则先 setup。serial 为空用默认设备。"""
        if serial is None:
            serial = default_serial()
        if serial is None:
            raise CDPError("未检测到已连接的设备（adb devices 为空）")
        if serial not in self._sessions:
            self.setup(serial)
        return serial, self._sessions[serial]

    def current_url(self, serial=None):
        _, sess = self._session(serial)
        return sess["current_url"]

    def _command(self, method, params=None, serial=None):
        serial, sess = self._session(serial)
        ws = WebSocketClient(sess["ws_url"])
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

    def command(self, method, params=None, serial=None):
        """执行命令，连接失效时自动重建一次。"""
        try:
            return self._command(method, params, serial=serial)
        except (OSError, CDPError):
            # WebView 可能已重建（PID 变化），刷新后重试一次
            self.setup(serial=serial)
            return self._command(method, params, serial=serial)

    def navigate(self, url, serial=None):
        return self.command("Page.navigate", {"url": url}, serial=serial)

    def evaluate(self, expression, serial=None):
        return self.command("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "allowUnsafeEvalBlocklistBypass": True,
        }, serial=serial)
