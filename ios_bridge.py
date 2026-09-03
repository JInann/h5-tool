#!/usr/bin/env python3
"""
iOS Web 调试桥管理器（基于 inspect-webkit，macOS only）。

inspect-webkit 是 macOS 上的 WIR→CDP 翻译桥：连接 iOS 真机（usbmuxd +
lockdownd TLS 配对）或模拟器（webinspectord_sim Unix socket）的 WebKit
远程调试服务，在本机暴露标准 CDP 端点（/json/list + /devtools/page/*），
让 h5-tool 能用与 Android 相同的链路调试 iOS Safari / App 内 WKWebView。

本模块只负责一件事：管理这个桥进程（bunx 子进程）的完整生命周期。
h5-tool 不管理 iOS 模拟器/真机 —— 用户自行启动后，桥自然能探测到。

约束：
  - 仅 macOS（桥依赖 /var/run/usbmuxd 与 webinspectord_sim socket）
  - 需要 bun 运行时（inspect-webkit 是纯 Bun/TS 项目，Node.js 跑不了）

运行时依赖探测（宽松，按序）：
  1. 环境变量 H5TOOL_BUN（显式指定 bun 或 bunx 可执行文件路径）
  2. PATH 中的 bunx / bun
  3. ~/.bun/bin/bun（官方安装脚本默认位置）
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

IOS_BRIDGE_PORT = int(os.environ.get("H5TOOL_IOS_PORT", "9322"))
BRIDGE_PKG = "inspect-webkit@0.0.5"

# 桥日志文件（进程退出/握手失败等排障信息），与 access_log 同目录
_DEFAULT_LOG_DIR = os.path.join(os.path.expanduser("~"), ".h5-tool", "logs")
LOG_DIR = os.environ.get("H5TOOL_LOG_DIR") or _DEFAULT_LOG_DIR
BRIDGE_LOG = os.path.join(LOG_DIR, "ios-bridge.log")

START_TIMEOUT = 60.0   # 首次启动含 bunx 下载 inspect-webkit 包（本地有缓存时 ~2s）
HEALTH_TIMEOUT = 2.0   # 健康探测单次超时


class BridgeError(Exception):
    pass


def supported() -> bool:
    """桥仅支持 macOS。"""
    return sys.platform == "darwin"


def find_bun():
    """按序探测 bun 可执行文件。返回 (exe 路径, 是否需要 `bun x` 前缀)。

    H5TOOL_BUN 可直接指向 bunx（脚本）或 bun；其余情况尽量先找 bunx。
    """
    env = os.environ.get("H5TOOL_BUN")
    if env:
        return env, os.path.basename(env) != "bun"
    for name in ("bunx", "bun"):
        hit = shutil.which(name)
        if hit:
            return hit, name == "bun"
    home_bun = Path.home() / ".bun" / "bin" / "bun"
    if home_bun.is_file():
        return str(home_bun), True
    home_bunx = Path.home() / ".bun" / "bin" / "bunx"
    if home_bunx.is_file():
        return str(home_bunx), False
    return None, False


def _no_proxy_get_json(url, timeout=HEALTH_TIMEOUT):
    """GET 本机 JSON 端点。显式禁用代理 —— 本机服务直连即可，
    避免被环境 http_proxy 劫持（WorkBuddy 沙箱代理会转发并返回 502）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _extract_app(description):
    """从 description 提取 App bundle id，如 'sim:14407 (com.apple.mobilesafari)'。"""
    m = re.search(r"\(([A-Za-z0-9][A-Za-z0-9._-]*)\)", description or "")
    return m.group(1) if m else None


class IOSBridge:
    """inspect-webkit 桥进程生命周期管理（模块级单例）。"""

    def __init__(self, port=IOS_BRIDGE_PORT):
        self.port = port
        self._lock = threading.Lock()
        self._proc = None          # 桥子进程
        self._starting = False     # 后台启动线程进行中
        self._running = False
        self._error = None         # 最近一次启动/探测失败原因
        self._log_fh = None        # 桥 stdout/stderr 落盘句柄
        self._started_at = 0.0

    # ---------------- 状态 ----------------
    def status(self):
        """轻量状态（不 spawn、不探测网络），供 /api/devices 等展示。"""
        bun, use_x = find_bun()
        with self._lock:
            running = self._running
            starting = self._starting
            error = self._error
        return {
            "supported": supported(),
            "bun": bool(bun),
            "bun_path": bun,
            "running": running,
            "starting": starting,
            "port": self.port,
            "error": error,
        }

    def is_running(self):
        with self._lock:
            return self._running

    # ---------------- 生命周期 ----------------
    def ensure_started(self):
        """幂等触发启动（后台线程执行，立即返回）。

        首次调用会 spawn `bunx --yes inspect-webkit@0.0.5 --port N`，
        含包下载耗时（数秒~数十秒）；就绪状态由调用方轮询 status()。
        """
        with self._lock:
            if self._running or self._starting:
                return
            if not supported():
                self._error = "仅支持 macOS（桥依赖 usbmuxd / webinspectord_sim）"
                return
            self._starting = True
            self._error = None
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _bunx_cmd(self):
        bun, use_x = find_bun()
        if not bun:
            raise BridgeError(
                "未找到 bun 运行时。安装：brew install oven-sh/bun，"
                "或 curl -fsSL https://bun.sh/install | bash "
                "（或用 H5TOOL_BUN 指定路径）")
        args = ["--yes", BRIDGE_PKG, "--port", str(self.port)]
        if use_x:
            args.insert(0, "x")
        return [bun] + args

    def _precheck_port(self):
        """启动前探测端口空闲：被占用则直接报错（避免 spawn 后 bind 失败、
        health check 误打到占用方进程导致「假 running」）。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", self.port))
        except OSError:
            raise BridgeError("端口 %d 已被其它进程占用，请释放或用 H5TOOL_IOS_PORT 换端口"
                              % self.port)
        finally:
            s.close()

    def _start_worker(self):
        try:
            self._open_log()
            self._precheck_port()
            cmd = self._bunx_cmd()
            self._log("launch: " + " ".join(cmd))
            self._proc = subprocess.Popen(
                cmd,
                stdout=self._log_fh, stderr=subprocess.STDOUT,
                start_new_session=True,   # 独立进程组，stop 时整组清理
            )
            self._wait_healthy()
            if self._proc.poll() is not None:
                raise BridgeError("inspect-webkit 进程已退出，见 %s" % BRIDGE_LOG)
            with self._lock:
                self._running = True
                self._started_at = time.time()
                self._error = None
            self._log("bridge ready on :%d" % self.port)
        except Exception as e:
            self._log("start failed: %s" % e)
            self._kill_proc()
            with self._lock:
                self._error = str(e)
        finally:
            with self._lock:
                self._starting = False

    def _wait_healthy(self):
        """轮询 /json/list 直到桥可响应（进程异常退出则立即失败）。"""
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise BridgeError("inspect-webkit 进程提前退出，见 %s" % BRIDGE_LOG)
            try:
                _no_proxy_get_json("http://127.0.0.1:%d/json/list" % self.port)
                return
            except Exception:
                time.sleep(0.8)
        raise BridgeError("inspect-webkit 启动超时（%ds），见 %s"
                          % (START_TIMEOUT, BRIDGE_LOG))

    def stop(self):
        """停止桥进程（进程退出 / 主动停止时清理）。幂等。"""
        with self._lock:
            self._starting = False
            self._running = False
        self._kill_proc()
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    def _kill_proc(self):
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        except Exception:
            pass

    # ---------------- 日志 ----------------
    def _open_log(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            self._log_fh = open(BRIDGE_LOG, "a", encoding="utf-8")
        except Exception:
            self._log_fh = None   # 打不开日志不阻塞启动

    def _log(self, line):
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            if self._log_fh:
                self._log_fh.write("%s %s\n" % (ts, line))
                self._log_fh.flush()
        except Exception:
            pass

    # ---------------- 目标列表 ----------------
    def targets(self):
        """拉取桥 /json/list 并精简。桥未就绪返回 None，失败抛异常由调用方兜底。"""
        if not self.is_running():
            return None
        pages = _no_proxy_get_json(
            "http://127.0.0.1:%d/json/list" % self.port, timeout=4.0)
        out = []
        for p in pages or []:
            out.append({
                "id": p.get("id"),
                "title": p.get("title", ""),
                "url": p.get("url", ""),
                "type": p.get("type", "page"),
                "ws": p.get("webSocketDebuggerUrl"),
                "frontend": p.get("devtoolsFrontendUrl"),
                "app": _extract_app(p.get("description")),
            })
        return out


# 模块级单例 + server.py 可直接使用的函数
bridge = IOSBridge()


def status():
    return bridge.status()


def ensure_started():
    bridge.ensure_started()


def is_running():
    """桥是否运行中（server.py 代理路由判断用）。"""
    return bridge.is_running()


def targets():
    """拉取桥目标列表（桥未就绪返回 None）。"""
    return bridge.targets()


def stop_all():
    """停止桥进程（server.py 退出时 atexit 调用）。"""
    bridge.stop()
