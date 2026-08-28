#!/usr/bin/env python3
"""
H5 小工具后端服务。

运行在宿主机上，H5 控制台通过 HTTP 与其通信（默认 127.0.0.1:12787）。
能力：
  1. 发送 H5 链接到手机当前 WebView（CDP Page.navigate）
  2. 截图（ADB screencap）
  3. 执行 JS 并返回结果（CDP Runtime.evaluate）
  4. 实时查看屏幕（前端按 3fps 轮询截图）+ 模拟点击（ADB input tap）

用法:
  python3 server.py                 # 默认 127.0.0.1:12787
  python3 server.py --host 0.0.0.0 --port 12787
"""

import argparse
import atexit
import json
import os
import queue
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cdp import (CDPError, CDPSession, _http_get_json, run_adb, WebSocketClient,
                 resolve_port, default_serial, list_devices, CDP_PORT)
from scr_stream import StreamError, get_streamer, stop_all

# 进程退出时清理所有设备的设备端 scrcpy server 与转发
atexit.register(stop_all)

WEB_DIR = Path(__file__).parent / "web"

# devtools-frontend 静态资源（复用 Chrome DevTools UI）
# DEVTOOLS_DIR 支持两种形态：
#   - 本地目录：如 /path/to/front_end 或 devtools-local/front_end（默认）
#   - 远程 URL：如 http://10.0.0.5:8080/devtools —— 反向代理拉取并本地缓存到
#     devtools-local/.remote-cache/，前端仍统一走本地 /devtools/
_DEVTOOLS_DIR_RAW = os.environ.get("DEVTOOLS_DIR") or str(Path(__file__).parent / "devtools-local" / "front_end")
DEVTOOLS_REMOTE_BASE = _DEVTOOLS_DIR_RAW if _DEVTOOLS_DIR_RAW.startswith(("http://", "https://")) else None
DEVTOOLS_DIR = Path(_DEVTOOLS_DIR_RAW) if not DEVTOOLS_REMOTE_BASE else None
DEVTOOLS_CACHE = Path(__file__).parent / "devtools-local" / ".remote-cache"

_DEVTOOLS_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
}

# 全局 CDP 会话（导航/执行 JS 复用）
cdp = CDPSession()

# 本服务自身监听的端口，禁止被「释放端口」误杀
SERVER_PORT = 12787


def adb_screencap_png(serial, timeout=15):
    """用 exec-out 直接把 PNG 从 stdout 拉回，避免落盘与 CRLF 转换。"""
    r = subprocess.run(
        f"adb -s {serial} exec-out screencap -p",
        shell=True, capture_output=True, timeout=timeout,
    )
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(r.stderr.decode("utf-8", "replace") or "screencap 失败")
    return r.stdout


def adb_tap(serial, x, y):
    r = run_adb(f"shell input tap {int(x)} {int(y)}", serial=serial)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or "input tap 失败")


def adb_swipe(serial, x1, y1, x2, y2, dur=200):
    r = run_adb(f"shell input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(dur)}",
                serial=serial)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or "input swipe 失败")


def adb_keyevent(serial, code):
    r = run_adb(f"shell input keyevent {int(code)}", serial=serial)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or "input keyevent 失败")


def adb_text(serial, text):
    # 空格需转义为 %s，其它特殊字符简单处理
    safe = text.replace(" ", "%s").replace("'", "")
    r = run_adb(f"shell input text '{safe}'", serial=serial)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or "input text 失败")


# ---------- 剪贴板（adb-clip） ----------
# 基于 polygraphene/adb-clip：通过 app_process 加载 clip.jar 访问 Android 10-16 剪贴板，
# 无需设备上装 App。工具安装到 /data/local/tmp/clip（clip + clip.jar）。
CLIP_BIN = "/data/local/tmp/clip"
CLIP_RELEASE_URL = "https://github.com/polygraphene/adb-clip/releases/latest/download"


def _shell_quote(s):
    """为远程 shell 命令加安全的单引号包裹（内容里的特殊字符不会被远程 shell 解析）。"""
    return "'" + s.replace("'", "'\\''") + "'"


def clip_installed(serial):
    return run_adb(f"shell [ -e {CLIP_BIN} ]", serial=serial).returncode == 0


def clip_install(serial):
    """从 GitHub 下载 adb-clip 并 push 到设备 /data/local/tmp。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        clip_p, jar_p = os.path.join(tmp, "clip"), os.path.join(tmp, "clip.jar")
        for dest, name in ((clip_p, "clip"), (jar_p, "clip.jar")):
            urllib.request.urlretrieve(f"{CLIP_RELEASE_URL}/{name}", dest)
        r = run_adb(f"push {shell_needed(clip_p)} {shell_needed(jar_p)} /data/local/tmp",
                    timeout=60, serial=serial)
        if r.returncode != 0:
            raise RuntimeError(r.stderr or "adb push 失败")
    run_adb(f"shell chmod 755 {CLIP_BIN}", serial=serial)


def shell_needed(path):
    """macOS/Linux 路径一般无空格风险，但 Downloads 场景仍稳妥加引号。"""
    return _shell_quote(path) if (" " in path or "'" in path or ")" in path) else path


def clipboard_get(serial):
    r = run_adb(f"shell {CLIP_BIN}", timeout=15, serial=serial)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "does not have foreground focus" in err or "clipboard" in err.lower():
            raise RuntimeError("设备剪贴板不可读：请确保手机屏幕点亮且解锁")
        raise RuntimeError(err or "读取手机剪贴板失败")
    # app_process 输出可能带 Warnings 行，只取非空正文；结尾统一去掉一个换行
    out = r.stdout.strip("\n")
    return out


def clipboard_set(serial, text):
    r = run_adb(f"shell {CLIP_BIN} {_shell_quote(text)}", timeout=15, serial=serial)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "does not have foreground focus" in err or "clipboard" in err.lower():
            raise RuntimeError("设备剪贴板不可写：请确保手机屏幕点亮且解锁")
        raise RuntimeError(err or "写入手机剪贴板失败")


# ---------- Mac 本机剪贴板 ----------
def mac_clipboard_get():
    """读取 Mac（本机）剪贴板文本。Windows 用 PowerShell 实现。"""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
            return r.stdout.rstrip("\n") if r.returncode == 0 else ""
        if sys.platform == "win32":
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-Clipboard -Raw -ErrorAction SilentlyContinue"],
                               capture_output=True, text=True, timeout=5)
            return r.stdout.rstrip("\r\n") if r.returncode == 0 else ""
    except Exception:
        pass
    return ""


def mac_clipboard_set(text):
    """写入 Mac（本机）剪贴板。"""
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
        elif sys.platform == "win32":
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Set-Clipboard -Value $input"],
                           input=text, text=True, timeout=5)
    except Exception:
        pass


# ---------- 剪贴板双向自动同步（纯文本） ----------
CLIP_SYNC_INTERVAL = 2.0      # 轮询间隔（秒）
CLIP_HISTORY_LIMIT = 100      # 历史条数上限
_clip_sync = None             # 全局同步器（main 里创建）


class ClipboardSync:
    """后台线程：双向同步 Mac ⇄ 手机剪贴板文本。

    检测到哪边内容变了就同步到另一边；写入后更新本侧记忆值，避免回声循环。
    """

    def __init__(self, interval=CLIP_SYNC_INTERVAL):
        self.interval = interval
        self.lock = threading.Lock()
        self.history = []            # [{direction, content, time, serial}]
        self.installed = False       # clip 是否已部署到设备
        self.installing = False      # 正在自动安装中
        self.syncing = False         # 最近一轮是否正常同步
        self.error = None            # 最近错误（如屏幕锁定）
        self.serial = None           # 当前绑定设备
        self.last_phone = None
        self.last_mac = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="clipboard-sync")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def ensure_installed(self, serial):
        """自动安装 clip 到设备；成功置 installed=True。"""
        if self.installing:
            return
        self.installing = True
        try:
            if not clip_installed(serial):
                clip_install(serial)
            with self.lock:
                self.installed = True
                self.error = None
        except Exception as e:
            with self.lock:
                self.installed = False
                self.error = str(e)
        finally:
            self.installing = False

    def add_history(self, direction, content, serial):
        with self.lock:
            self.history.insert(0, {
                "direction": direction,   # "mac" 或 "phone"
                "content": content,
                "time": time.strftime("%H:%M:%S"),
                "serial": serial,
            })
            del self.history[CLIP_HISTORY_LIMIT:]

    @staticmethod
    def _md5_bytes(b):
        import hashlib
        return hashlib.md5(b).hexdigest()

    @staticmethod
    def _md5_bytes(b):
        import hashlib
        return hashlib.md5(b).hexdigest()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                serial = default_serial()
                if not serial:
                    with self.lock:
                        self.syncing = False
                    continue
                if serial != self.serial:
                    # 设备变化：重绑并初始化两侧基线（不触发同步）
                    self.serial = serial
                    try:
                        self.last_phone = clipboard_get(serial)
                    except Exception:
                        self.last_phone = ""
                    self.last_mac = mac_clipboard_get()
                if not self.installed:
                    self.ensure_installed(serial)
                if not self.installed:
                    continue
                phone_now = clipboard_get(serial)
                mac_now = mac_clipboard_get()
                with self.lock:
                    self.syncing = True
                    self.error = None
                if phone_now != self.last_phone and phone_now != self.last_mac:
                    # 手机复制了新内容 → 同步到 Mac（若与上次 Mac 值相同则视为回声，跳过）
                    mac_clipboard_set(phone_now)
                    self.last_mac = phone_now
                    self.add_history("phone", phone_now, serial)
                    # phone→Mac 写入后，mac_now（旧值）一定不等于 last_mac（新值），
                    # 不重读就会被下方 mac→手机 判定为"Mac 变化"再写回手机，造成来回抖
                    mac_now = mac_clipboard_get()
                self.last_phone = phone_now
                if mac_now != self.last_mac and mac_now != self.last_phone:
                    # Mac 复制了新内容 → 同步到手机
                    clipboard_set(serial, mac_now)
                    self.last_phone = mac_now
                    self.add_history("mac", mac_now, serial)
                self.last_mac = mac_now
            except Exception as e:
                with self.lock:
                    self.error = str(e)
                    self.syncing = False


def kill_port(port):
    """杀掉占用指定端口（TCP）的进程，返回被杀进程信息。跨平台（lsof / netstat+taskkill）。"""
    if sys.platform == "win32":
        r = subprocess.run(f'netstat -ano | findstr ":{int(port)}"', shell=True,
                           capture_output=True, text=True, timeout=10)
        pids = set()
        for line in r.stdout.splitlines():
            # 行格式: TCP    127.0.0.1:12787    0.0.0.0:0    LISTENING    12345
            if f":{port}" in line and "LISTENING" in line.upper():
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pids.add(parts[-1])
        if not pids:
            return {"port": port, "killed": [], "message": f"端口 {port} 未被占用"}
        killed = []
        for pid in pids:
            name = "?"
            try:
                nr = subprocess.run(f'tasklist /FI "PID eq {pid}" /FO CSV /NH', shell=True,
                                    capture_output=True, text=True, timeout=10)
                name = nr.stdout.strip().split(",")[0].strip('"') or "?"
            except Exception:
                pass
            killed.append({"pid": int(pid), "name": name})
            subprocess.run(f"taskkill /F /PID {pid}", shell=True, timeout=10)
        return {"port": port, "killed": killed,
                "message": f"已释放端口 {port}，杀掉 {len(killed)} 个进程"}
    r = subprocess.run(f"lsof -ti tcp:{int(port)}", shell=True,
                       capture_output=True, text=True, timeout=10)
    pids = [p for p in r.stdout.split() if p.strip().isdigit()]
    if not pids:
        return {"port": port, "killed": [], "message": f"端口 {port} 未被占用"}
    killed = []
    for pid in pids:
        name = subprocess.run(f"ps -p {pid} -o comm=", shell=True,
                              capture_output=True, text=True).stdout.strip()
        killed.append({"pid": int(pid), "name": name or "?"})
    subprocess.run(f"kill -9 {' '.join(pids)}", shell=True, timeout=10)
    return {"port": port, "killed": killed,
            "message": f"已释放端口 {port}，杀掉 {len(pids)} 个进程"}


def _ifconfig_inet(iface):
    """取指定网络接口的 IPv4 地址（inet），拿不到返回 None。"""
    try:
        out = subprocess.run(["ifconfig", iface], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def _mac_wifi_ip():
    """macOS：从硬件端口列表里找到 Wi-Fi（或 AirPort）对应的设备（如 en0），取其 inet 地址。"""
    try:
        hw = subprocess.run(["networksetup", "-listallhardwareports"],
                            capture_output=True, text=True, timeout=5).stdout
        lines = hw.splitlines()
        for i, line in enumerate(lines):
            if re.search(r"Wi-?Fi|AirPort", line):
                for nxt in lines[i + 1:i + 4]:
                    if "Device" in nxt:
                        return _ifconfig_inet(nxt.split(":", 1)[1].strip())
    except Exception:
        pass
    return None


def _default_route_ip():
    """回退：取默认路由所在接口的 IPv4 地址（可能是以太网/VPN，不一定是 WiFi）。"""
    try:
        out = subprocess.run(["route", "get", "default"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"interface:\s*(\w+)", out)
        if m:
            return _ifconfig_inet(m.group(1))
    except Exception:
        pass
    return None


def _udp_trick_ip():
    """最后回退：用 UDP 连接外部地址探测出口网卡 IP（不真正发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def _windows_ip():
    """Windows：解析 ipconfig 中第一个非回环 IPv4 地址。"""
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=5).stdout
        for m in re.finditer(r"IPv4[^\n:]*:\s*(\d+\.\d+\.\d+\.\d+)", out):
            ip = m.group(1)
            if ip != "127.0.0.1" and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass
    return None


def resolve_host_ip():
    """获取手机能访问到的本机局域网 IP。

    macOS 优先级：Wi-Fi 硬件端口 IP → 默认路由接口 IP → UDP 出口探测；
    Windows：ipconfig 首个非回环 IPv4 → UDP 出口探测。
    跳过 127.0.0.1 / 169.254.x（链路本地）。
    """
    if sys.platform == "win32":
        for fn in (_windows_ip, _udp_trick_ip):
            ip = fn()
            if ip and ip != "127.0.0.1" and ip != "0.0.0.0":
                return ip
        return "127.0.0.1"
    for fn in (_mac_wifi_ip, _default_route_ip, _udp_trick_ip):
        ip = fn()
        if ip and ip != "127.0.0.1" and ip != "0.0.0.0":
            return ip
    return "127.0.0.1"


def get_screen_size(serial):
    r = run_adb("shell wm size", serial=serial)
    # "Physical size: 1080x2400"  (可能还有 Override size)
    w = h = None
    for line in r.stdout.splitlines():
        if "size:" in line and "x" in line:
            try:
                dims = line.split(":")[1].strip()
                w, h = (int(v) for v in dims.split("x"))
            except Exception:
                pass
    return w, h


def get_status(serial=None):
    """获取指定设备（默认第一台）的状态。"""
    devices = list_devices()
    if serial is None or not any(d["serial"] == serial for d in devices):
        serial = default_serial()
    status = {
        "device": serial,
        "devices": devices,
        "device_connected": bool(devices),
        "webview": None,
        "current_url": None,
        "screen": None,
    }
    if serial:
        w, h = get_screen_size(serial)
        if w:
            status["screen"] = {"width": w, "height": h}
        streamer = get_streamer(serial)
        status["scrcpy"] = streamer._server_path is not None
        status["streaming"] = streamer.is_alive()
        try:
            cdp.setup(serial)
            status["webview"] = True
            status["current_url"] = cdp.current_url(serial)
        except Exception as e:
            status["webview"] = False
            status["webview_error"] = str(e)
    return status


def _pick_targets(pages):
    """精简 CDP /json 返回的 target 字段，避免把大对象全量透传。"""
    out = []
    for p in pages or []:
        out.append({
            "id": p.get("id"),
            "title": p.get("title", ""),
            "url": p.get("url", ""),
            "type": p.get("type", ""),
            "ws": p.get("webSocketDebuggerUrl"),
            "frontend": p.get("devtoolsFrontendUrl"),
        })
    return out


def get_webview_targets(serial=None):
    """汇总可调试目标：指定设备 WebView（按设备槽位端口）。

    复用 cdp.setup()（幂等，自动建 adb forward 并刷新目标）。
    """
    result = {"device": serial, "phone": [], "phone_error": None}
    try:
        if serial is None:
            serial = default_serial()
        if serial is None:
            raise CDPError("未检测到已连接的设备（adb devices 为空）")
        cdp.setup(serial)
        serials = [d["serial"] for d in list_devices()]
        port = resolve_port(serial, CDP_PORT, serials)
        result["phone"] = _pick_targets(
            _http_get_json(f"http://127.0.0.1:{port}/json"))
    except Exception as e:
        result["phone_error"] = str(e)
    return result


# ---------- WebSocket 代理（CDP Origin 校验绕过） ----------
# Android WebView（Chrome 111+ 内核）的 CDP server 会校验 WebSocket 的 Origin：
# 浏览器 iframe（Origin=http://127.0.0.1:12787）直连 ws://127.0.0.1:9222 会被 403 拒绝。
# 本代理作为中间层：浏览器连 h5-tool 的 /cdp-ws/<targetId>，
# 代理用 cdp.WebSocketClient（不带 Origin）转发到 9222，实现双向透传。

import hashlib
import base64
import struct

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被关闭")
        buf += chunk
    return buf


def _read_ws_frame(sock):
    """读一帧，返回 (fin, opcode, payload)。payload 已解 mask。"""
    b0, b1 = _recv_exact(sock, 2)
    fin = b0 & 0x80
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


def _write_ws_frame(sock, opcode, payload, mask_key=None, fin=True):
    """发一帧。mask_key 为 None 则不 mask（服务端→客户端方向）。"""
    header = bytearray([0x80 | opcode] if fin else [opcode])
    length = len(payload)
    if length < 126:
        header.append((0x80 if mask_key else 0) | length)
    elif length < 65536:
        header.append((0x80 if mask_key else 0) | 126)
        header.extend(struct.pack(">H", length))
    else:
        header.append((0x80 if mask_key else 0) | 127)
        header.extend(struct.pack(">Q", length))
    if mask_key:
        header.extend(mask_key)
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + payload)


def _ws_pump(src, dst, mask_key, fin_flag):
    """把 src 的帧转发到 dst。返回 False 表示应结束（close 或异常）。"""
    while True:
        try:
            fin, opcode, payload = _read_ws_frame(src)
        except Exception:
            return False
        if opcode == 0x8:  # close：透传后结束
            try:
                _write_ws_frame(dst, 0x8, payload, mask_key=mask_key)
            except Exception:
                pass
            return False
        try:
            if opcode == 0x9:  # ping
                _write_ws_frame(dst, 0x9, payload, mask_key=mask_key)
            elif opcode == 0xA:  # pong
                _write_ws_frame(dst, 0xA, payload, mask_key=mask_key)
            else:  # text / binary / continuation
                _write_ws_frame(dst, opcode, payload, mask_key=mask_key,
                                fin=bool(fin))
        except Exception:
            return False


def handle_cdp_proxy(browser_sock, target_id, serial=None):
    """浏览器 WebSocket <-> 指定设备 CDP 双向代理。

    browser_sock 已由 Handler 完成握手；target 侧复用 cdp.WebSocketClient
    （握手不带 Origin，Android WebView 的 9222 才能 101）。
    """
    if serial is None:
        serial = default_serial()
    if serial is None:
        try:
            _write_ws_frame(browser_sock, 0x8, b"", fin=True)
        except Exception:
            pass
        return
    serials = [d["serial"] for d in list_devices()]
    port = resolve_port(serial, CDP_PORT, serials)
    upstream = WebSocketClient(f"ws://127.0.0.1:{port}/devtools/page/{target_id}")
    try:
        upstream.connect()
    except Exception as e:
        try:
            _write_ws_frame(browser_sock, 0x8, b"", fin=True)
        except Exception:
            pass
        sys.stderr.write(f"[h5-tool] CDP 代理上游连接失败 {serial}/{target_id}: {e}\n")
        return
    upstream_sock = upstream.sock

    def close_both():
        for s in (browser_sock, upstream_sock):
            try:
                s.close()
            except Exception:
                pass

    def to_upstream():
        # 浏览器帧已 mask → 转发给 9222 需重新 mask
        if not _ws_pump(browser_sock, upstream_sock, os.urandom(4), True):
            close_both()

    def to_browser():
        # 9222 帧未 mask → 直接透传给浏览器
        if not _ws_pump(upstream_sock, browser_sock, None, True):
            close_both()

    t1 = threading.Thread(target=to_upstream, daemon=True)
    t2 = threading.Thread(target=to_browser, daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def _claude_tcp_latency(host, port=443, timeout=5.0):
    """TCP 建连延迟（直连，绕过代理，看宿主机到目标的裸延迟）。"""
    t = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout):
        pass
    return (time.perf_counter() - t) * 1000.0


def _claude_https_latency(host, timeout=12.0):
    """HTTPS 请求往返延迟（urllib 自动走 HTTPS_PROXY 环境变量）。打到 /v1/models，
    预期 401，但能完整测出「经出口代理 → Anthropic」的真实往返。"""
    url = f"https://{host}/v1/models"
    t = time.perf_counter()
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "h5-tool"})
    try:
        urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError:
        pass
    return (time.perf_counter() - t) * 1000.0


def claude_latency_probe(rounds=4):
    """测当前出口节点到 api.anthropic.com 的延迟，返回结构化结果供前端展示。"""
    host = "api.anthropic.com"
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "（直连）"
    # warmup：丢弃首轮（代理冷启动会虚高），只统计稳态
    try:
        _claude_tcp_latency(host)
        _claude_https_latency(host)
    except Exception:
        pass
    tcp, https = [], []
    for _ in range(rounds):
        try:
            tcp.append(_claude_tcp_latency(host))
        except Exception:
            pass
        try:
            https.append(_claude_https_latency(host))
        except Exception:
            pass

    def _stat(s):
        if not s:
            return None
        s = sorted(s)
        p90 = s[min(len(s) - 1, int(len(s) * 0.9))]
        return {
            "min": round(min(s), 1),
            "p50": round(statistics.median(s), 1),
            "p90": round(p90, 1),
            "max": round(max(s), 1),
            "n": len(s),
        }

    return {
        "ok": bool(https),
        "proxy": proxy,
        "host": host,
        "tcp": _stat(tcp),
        "https": _stat(https),
        "ts": time.strftime("%H:%M:%S"),
    }


def simplify_eval(result):
    """把 Runtime.evaluate 的结果整理成 {value, type, error}。"""
    if result.get("exceptionDetails"):
        exc = result["exceptionDetails"]
        text = exc.get("exception", {}).get("description") or exc.get("text") or "执行出错"
        return {"error": text}
    obj = result.get("result", {})
    out = {"type": obj.get("type"), "subtype": obj.get("subtype")}
    if "value" in obj:
        out["value"] = obj["value"]
    elif obj.get("description") is not None:
        out["value"] = obj["description"]
    else:
        out["value"] = None
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def finish(self):
        # 视频流客户端中途断开时会触发 BrokenPipe，压制以避免刷日志
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, fmt, *args):
        # 精简日志：截图轮询太频繁，不打印
        if "screenshot" in self.path:
            return
        sys.stderr.write("[h5-tool] %s\n" % (fmt % args))

    # PNA（Private Network Access）相关头：公网页面访问 127.0.0.1 等私网地址需要后端 opt-in。
    # 注意：开了 ACA-PN 后不能再用 Allow-Origin: *，必须 echo 请求的 Origin；没有 Origin 时 fallback 回 *。
    def _cors_origin(self):
        o = self.headers.get("Origin")
        return o if o else "*"

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, content_type, cache=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", f"max-age={cache}")
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _read_multipart(self):
        """解析 multipart/form-data 请求体，返回 {field: (filename, bytes)}。

        文件上传（文件推送 / APK 安装）用；无法解析时返回 None。
        """
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
        if not ctype.startswith("multipart/form-data") or not m:
            return None
        boundary = (m.group(1) or m.group(2)).encode()
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return None
        if not length:
            return None
        body = self.rfile.read(length)
        parts = {}
        for raw in body.split(b"--" + boundary):
            raw = raw.strip(b"\r\n")
            if not raw or raw == b"--":
                continue
            sep = raw.find(b"\r\n\r\n")
            if sep < 0:
                continue
            headers = raw[:sep].decode("utf-8", "replace")
            content = raw[sep + 4:]
            if content.endswith(b"\r\n"):
                content = content[:-2]
            m1 = re.search(r'name="([^"]+)"', headers)
            m2 = re.search(r'filename="([^"]*)"', headers)
            if not m1:
                continue
            parts[m1.group(1)] = (m2.group(1) if m2 else None, content)
        return parts

    def _stream_video(self, serial=None):
        """订阅指定设备 scrcpy 视频流，以 chunked 二进制形式转发给浏览器（WebCodecs 解码）。"""
        if serial is None:
            serial = default_serial()
        if serial is None:
            try:
                self.wfile.write(b"0\r\n\r\n")
            except Exception:
                pass
            return
        streamer = get_streamer(serial)
        # 先发响应头，让浏览器立刻进入连接态；scrcpy 启动时数据稍后到达。
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        try:
            q = streamer.subscribe()
        except StreamError as e:
            try:
                self.wfile.write(("0\r\n\r\n").encode("latin-1"))
            except Exception:
                pass
            return
        try:
            while True:
                try:
                    chunk = q.get(timeout=1)
                except queue.Empty:
                    if not streamer.is_alive():
                        break
                    continue
                if chunk == b"":       # 流结束信号
                    break
                try:
                    self.wfile.write(("%X\r\n" % len(chunk)).encode("latin-1") + chunk + b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, OSError):
                    break
        finally:
            streamer.unsubscribe(q)
            try:
                self.wfile.write(b"0\r\n\r\n")
            except Exception:
                pass

    def _serve_static(self, rel):
        path = (WEB_DIR / rel).resolve()
        if not str(path).startswith(str(WEB_DIR.resolve())) or not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        ext = path.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(ext, "application/octet-stream")
        self._send_bytes(path.read_bytes(), ctype)

    def _serve_devtools(self, rel):
        """托管 devtools-frontend 静态资源（inspector.html 及其 js/css/资源）。

        支持本地目录（DEVTOOLS_DIR=本地路径）与远程反向代理（DEVTOOLS_DIR=http(s)://…）
        两种形态；保持原始目录结构按相对路径 serve。
        """
        if DEVTOOLS_REMOTE_BASE:
            return self._serve_devtools_remote(rel)
        if DEVTOOLS_DIR is None or not DEVTOOLS_DIR.is_dir():
            self._send_json({
                "error": "devtools 资源未构建",
                "hint": "设置 DEVTOOLS_DIR 指向 front_end 产物目录（本地路径或 http(s):// 远程地址）",
            }, 404)
            return
        # 目录请求：跳到 inspector.html（DevTools 入口）
        if not rel or rel.endswith("/"):
            rel = "inspector.html"
        path = (DEVTOOLS_DIR / rel).resolve()
        if not str(path).startswith(str(DEVTOOLS_DIR.resolve())) or not path.is_file():
            self._send_json({"error": "devtools: not found"}, 404)
            return
        ext = path.suffix.lower()
        ctype = _DEVTOOLS_MIME.get(ext, "application/octet-stream")
        # devtools 资源较大且不变，浏览器缓存 1 小时
        self._send_bytes(path.read_bytes(), ctype, cache=3600)

    def _serve_devtools_remote(self, rel):
        """远程模式：从 DEVTOOLS_REMOTE_BASE 拉取资源，落盘缓存到 devtools-local/.remote-cache/。"""
        if not rel or rel.endswith("/"):
            rel = "inspector.html"
        if ".." in rel or rel.startswith("/") or "\\" in rel:
            return self._send_json({"error": "devtools: bad path"}, 400)
        cache_path = DEVTOOLS_CACHE / rel
        data = None
        ctype = None
        if cache_path.is_file():
            data = cache_path.read_bytes()
            ctype = _DEVTOOLS_MIME.get(cache_path.suffix.lower(), "application/octet-stream")
        else:
            url = f"{DEVTOOLS_REMOTE_BASE.rstrip('/')}/{rel}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "h5-tool"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status != 200:
                        return self._send_json({"error": f"devtools 远程 {resp.status}"}, 502)
                    data = resp.read()
                    ctype = resp.headers.get("Content-Type", "application/octet-stream")
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(data)
            except Exception as e:
                return self._send_json({"error": f"devtools 远程获取失败: {e}"}, 502)
        self._send_bytes(data, ctype, cache=3600)

    # ---------- 路由 ----------
    def _query_device(self):
        """从 GET query string 提取 device 参数（无则返回 None）。"""
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        vals = q.get("device")
        return vals[0] if vals else None

    def do_OPTIONS(self):
        origin = self.headers.get("Origin") or "*"
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Access-Control-Request-Private-Network")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        device = self._query_device()
        try:
            if path == "/" or path == "/index.html":
                self._serve_static("index.html")
            elif path == "/devtools.html":
                self._serve_static("devtools.html")
            elif path == "/api/status":
                self._send_json(get_status(device))
            elif path == "/api/devices":
                self._send_json({"devices": list_devices(),
                                 "default": default_serial()})
            elif path == "/api/webview-targets":
                self._send_json(get_webview_targets(device))
            elif path == "/api/screenshot":
                if device is None:
                    device = default_serial()
                if device is None:
                    return self._send_json({"error": "未检测到已连接的设备"}, 400)
                self._send_bytes(adb_screencap_png(device), "image/png")
            elif path == "/api/stream":
                self._stream_video(device)
            elif path == "/api/claude-latency":
                self._send_json(claude_latency_probe())
            elif path == "/api/clipboard":
                if device is None:
                    device = default_serial()
                if device is None:
                    return self._send_json({"error": "未检测到已连接的设备"}, 400)
                if not clip_installed(device):
                    return self._send_json({"error": "设备未安装 clip 工具，请先点「安装到手机」"}, 409)
                content = clipboard_get(device)
                self._send_json({"ok": True, "content": content, "device": device})
            elif path == "/api/clipboard/status":
                sync = _clip_sync
                if sync is None:
                    return self._send_json({"enabled": False, "reason": "服务以 --no-clip-sync 启动"})
                with sync.lock:
                    self._send_json({
                        "enabled": True,
                        "installed": sync.installed,
                        "installing": sync.installing,
                        "syncing": sync.syncing,
                        "error": sync.error,
                        "device": sync.serial,
                        "interval": sync.interval,
                        "history": len(sync.history),
                    })
            elif path == "/api/clipboard/history":
                sync = _clip_sync
                if sync is None:
                    return self._send_json({"items": []})
                with sync.lock:
                    self._send_json({"items": list(sync.history)})
            elif path.startswith("/cdp-ws/"):
                # WebSocket 代理：devtools 前端连本地代理，绕过 WebView CDP 的 Origin 校验
                if self.headers.get("Upgrade", "").lower() != "websocket":
                    return self._send_json({"error": "需要 WebSocket 连接"}, 400)
                target_id = path[len("/cdp-ws/"):]
                if not target_id or "/" in target_id or "?" in target_id:
                    return self._send_json({"error": "target 无效"}, 400)
                key = self.headers.get("Sec-WebSocket-Key", "")
                if not key:
                    return self._send_json({"error": "缺少 Sec-WebSocket-Key"}, 400)
                resp = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {_ws_accept(key)}\r\n"
                    "\r\n"
                )
                self.connection.sendall(resp.encode())
                self.close_connection = True
                handle_cdp_proxy(self.connection, target_id, serial=device)
            elif path.startswith("/devtools/"):
                self._serve_devtools(path[len("/devtools/"):])
            elif path.startswith("/"):
                self._serve_static(path.lstrip("/"))
            else:
                self._send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            # multipart 请求（文件上传）不能让 _read_body 抢先消费 body
            ctype = self.headers.get("Content-Type", "")
            is_multipart = ctype.startswith("multipart/form-data")
            body = {} if is_multipart else self._read_body()
            device = body.get("device") or self._query_device()
            if path == "/api/navigate":
                url = (body.get("url") or "").strip()
                if not url:
                    return self._send_json({"error": "缺少 url"}, 400)
                # 将 {ip} 占位符替换为本机实际 LAN IP（如 http://{ip}:5173 → http://172.16.136.225:5173）
                host_ip = resolve_host_ip()
                resolved = url.replace("{ip}", host_ip)
                replaced_ip = resolved != url
                url = resolved
                if not url.startswith(("http://", "https://", "file://", "about:")):
                    url = "https://" + url
                cdp.navigate(url, serial=device)
                self._send_json({"ok": True, "url": url, "device": device,
                                 "ip": host_ip, "replaced_ip": replaced_ip})

            elif path == "/api/eval":
                expr = body.get("expression") or ""
                if not expr.strip():
                    return self._send_json({"error": "缺少 expression"}, 400)
                result = cdp.evaluate(expr, serial=device)
                self._send_json({"ok": True, "device": device, **simplify_eval(result)})

            elif path == "/api/tap":
                if device is None:
                    device = default_serial()
                if device is None:
                    return self._send_json({"error": "未检测到已连接的设备"}, 400)
                adb_tap(device, body["x"], body["y"])
                self._send_json({"ok": True, "device": device})

            elif path == "/api/swipe":
                if device is None:
                    device = default_serial()
                if device is None:
                    return self._send_json({"error": "未检测到已连接的设备"}, 400)
                adb_swipe(device, body["x1"], body["y1"], body["x2"], body["y2"],
                          body.get("dur", 200))
                self._send_json({"ok": True, "device": device})

            elif path == "/api/key":
                # 常用：back=4, home=3, enter=66, back-space=67
                if device is None:
                    device = default_serial()
                if device is None:
                    return self._send_json({"error": "未检测到已连接的设备"}, 400)
                adb_keyevent(device, body.get("code", 4))
                self._send_json({"ok": True, "device": device})

            elif path == "/api/text":
                if device is None:
                    device = default_serial()
                if device is None:
                    return self._send_json({"error": "未检测到已连接的设备"}, 400)
                adb_text(device, body.get("text", ""))
                self._send_json({"ok": True, "device": device})

            elif path == "/api/clipboard/push":
                # Mac -> 手机
                if device is None:
                    device = default_serial()
                if device is None:
                    return self._send_json({"error": "未检测到已连接的设备"}, 400)
                text = body.get("text")
                if text is None:
                    return self._send_json({"error": "缺少 text"}, 400)
                if not clip_installed(device):
                    return self._send_json({"error": "设备未安装 clip 工具，请先点「安装到手机」"}, 409)
                clipboard_set(device, text)
                self._send_json({"ok": True, "device": device, "length": len(text)})

            elif path == "/api/clipboard/install":
                if device is None:
                    device = default_serial()
                if device is None:
                    return self._send_json({"error": "未检测到已连接的设备"}, 400)
                clip_install(device)
                sync = _clip_sync
                if sync is not None:
                    with sync.lock:
                        sync.installed = True
                        sync.error = None
                self._send_json({"ok": True, "device": device,
                                 "message": f"adb-clip 已安装到 {CLIP_BIN}"})

            elif path == "/api/files/push":
                # 文件推送：multipart 上传 → adb push 到手机
                if device is None:
                    device = default_serial()
                if device is None:
                    return self._send_json({"error": "未检测到已连接的设备"}, 400)
                parts = self._read_multipart()
                if parts is None:
                    return self._send_json({"error": "需要 multipart/form-data 上传"}, 400)
                filename, data = parts.get("file") or (None, None)
                if data is None:
                    return self._send_json({"error": "缺少 file 字段"}, 400)
                tmp = tempfile.mkstemp(suffix=os.path.splitext(filename)[1] or ".bin")[1]
                with open(tmp, "wb") as f:
                    f.write(data)
                try:
                    remote_dir = "/sdcard/Download/h5-tool"
                    run_adb(f"shell mkdir -p {remote_dir}", serial=device)
                    r = run_adb(f"push {tmp} {remote_dir}/{filename}",
                                timeout=120, serial=device)
                    if r.returncode != 0:
                        raise RuntimeError(r.stderr or "adb push 失败")
                    self._send_json({"ok": True, "device": device,
                                     "name": filename, "size": len(data),
                                     "remote": f"{remote_dir}/{filename}"})
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

            elif path == "/api/apk/install":
                # APK 安装：multipart 上传 .apk → adb install -r 到手机
                if device is None:
                    device = default_serial()
                if device is None:
                    return self._send_json({"error": "未检测到已连接的设备"}, 400)
                parts = self._read_multipart()
                if parts is None:
                    return self._send_json({"error": "需要 multipart/form-data 上传"}, 400)
                filename, data = parts.get("file") or (None, None)
                if data is None:
                    return self._send_json({"error": "缺少 file 字段"}, 400)
                if not filename.lower().endswith(".apk"):
                    return self._send_json({"error": "请选择 .apk 文件"}, 400)
                tmp = tempfile.mkstemp(suffix=".apk")[1]
                with open(tmp, "wb") as f:
                    f.write(data)
                try:
                    r = run_adb(f"install -r {tmp}", timeout=300, serial=device)
                    out = (r.stdout + r.stderr).strip()
                    if r.returncode != 0:
                        detail = out.replace("\n", " ") or "安装失败"
                        # 截断过长的错误信息
                        detail = detail[:300]
                        return self._send_json({"error": f"安装失败：{detail}"}, 500)
                    self._send_json({"ok": True, "device": device,
                                     "name": filename, "size": len(data),
                                     "output": out[:200]})
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

            elif path == "/api/kill-port":
                try:
                    port = int(body.get("port"))
                except (TypeError, ValueError):
                    return self._send_json({"error": "端口无效"}, 400)
                if not (1 <= port <= 65535):
                    return self._send_json({"error": "端口需在 1-65535 之间"}, 400)
                if port == SERVER_PORT:
                    return self._send_json({"error": "不能释放本工具自身占用的端口"}, 400)
                self._send_json({"ok": True, **kill_port(port)})

            else:
                self._send_json({"error": "not found"}, 404)
        except CDPError as e:
            self._send_json({"error": str(e)}, 502)
        except BrokenPipeError:
            pass
        except Exception as e:
            self._send_json({"error": str(e)}, 500)


def main():
    parser = argparse.ArgumentParser(description="H5 小工具后端服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=12787)
    parser.add_argument("--no-clip-sync", action="store_true",
                        help="禁用剪贴板自动同步")
    args = parser.parse_args()

    global SERVER_PORT, _clip_sync
    SERVER_PORT = args.port

    if not args.no_clip_sync:
        _clip_sync = ClipboardSync()
        _clip_sync.start()
        print("[h5-tool] 剪贴板双向自动同步已启动（每 %.1fs 检测）" % _clip_sync.interval, flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    print(f"[h5-tool] 服务已启动：{url}", flush=True)
    print("[h5-tool] 在浏览器打开上面的地址即可使用控制台。按 Ctrl+C 退出。", flush=True)
    print("[h5-tool] 控制台（服务器部署版）：https://devtools-xhstudy-d1g9ap809fb38788a.webapps.tcloudbase.com/", flush=True)
    print("[h5-tool] 调试面板（服务器部署版）：https://devtools-xhstudy-d1g9ap809fb38788a.webapps.tcloudbase.com/devtools.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[h5-tool] 已退出")
        server.shutdown()


if __name__ == "__main__":
    main()
