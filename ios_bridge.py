#!/usr/bin/env python3
"""
iOS Web 调试桥管理器（基于 pymobiledevice3 的 webinspector cdp，跨平台）。

pymobiledevice3（doronz88）是本机与 iOS 真机之间的瑞士军刀：`webinspector cdp`
子命令会起一个本地 CDP server（FastAPI/uvicorn），把 iPhone/iPad 上 Safari 与
App 内 WKWebView 的 WebKit 远程调试（WIR）翻译成标准 CDP 端点（/json/list +
/devtools/page/*），让 h5-tool 能用与 Android 相同的链路调试 iOS 真机。

与旧方案 inspect-webkit（bun 运行时、仅 macOS）的区别：
  - 纯 Python，pip / brew / pipx 均可安装，Windows / Linux / macOS 通用；
  - 每实例只连接一台设备（--udid），不支持 iOS 模拟器（真机 USB/RSD 专用）；
  - target 列表无 device/app 字段 → 设备分组改由 usbmux 设备表驱动。

本模块只负责一件事：管理这些桥进程（每台真机一个 pymobiledevice3 子进程）
的完整生命周期（懒启动 / 健康探测 / 端口分配 / 清理）。

约束：
  - 需要 pymobiledevice3 可执行文件（探测顺序见 find_pmd3）

运行时依赖探测（宽松，按序）：
  1. 环境变量 H5TOOL_PMD3（显式指定 pymobiledevice3 可执行文件路径）
  2. PATH 中的 pymobiledevice3
  3. PATH 中的 uvx（`uvx pymobiledevice3 ...` 兜底，首次会解析环境）
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

IOS_BRIDGE_PORT = int(os.environ.get("H5TOOL_IOS_PORT", "9322"))
ENGINE_NAME = "pymobiledevice3"

# 桥日志文件（进程退出/握手失败等排障信息），与 access_log 同目录
_DEFAULT_LOG_DIR = os.path.join(os.path.expanduser("~"), ".h5-tool", "logs")
LOG_DIR = os.environ.get("H5TOOL_LOG_DIR") or _DEFAULT_LOG_DIR
BRIDGE_LOG = os.path.join(LOG_DIR, "ios-bridge.log")

START_TIMEOUT = 60.0   # 实例就绪超时（pmd3 import 较重 + 首次连接设备）
HEALTH_TIMEOUT = 2.0   # 健康探测单次超时
USBMUX_TTL = 5.0       # usbmux 设备表缓存（秒），避免每 8s 轮询都 spawn 一次


class BridgeError(Exception):
    pass


def supported() -> bool:
    """pmd3 跨平台（win 需 Apple Devices / usbmuxd，失败会体现在 error）。"""
    return True


def find_pmd3():
    """按序探测 pymobiledevice3。返回 (argv_base, error)。

    argv_base 是可直接 spawn 的命令前缀列表（含 uvx 包装）。
    """
    env = os.environ.get("H5TOOL_PMD3")
    if env:
        return [env], None
    hit = shutil.which("pymobiledevice3")
    if hit:
        return [hit], None
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "pymobiledevice3"], None
    return None, ("未找到 pymobiledevice3 引擎。安装："
                  "brew install pymobiledevice3 或 pipx install pymobiledevice3"
                  "（或用 H5TOOL_PMD3 指定可执行文件路径）")


def _no_proxy_get_json(url, timeout=HEALTH_TIMEOUT):
    """GET 本机 JSON 端点。显式禁用代理 —— 本机服务直连即可，
    避免被环境 http_proxy 劫持（WorkBuddy 沙箱代理会转发并返回 502）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ---------------- usbmux 设备表 ----------------
_usbmux_lock = threading.Lock()
_usbmux_cache = None   # (ts, devices)


def _usbmux_devices():
    """本机 usbmuxd 上的 iOS 真机列表 [{udid, name}]（TTL 缓存，免频繁 spawn）。"""
    global _usbmux_cache
    now = time.time()
    with _usbmux_lock:
        if _usbmux_cache and now - _usbmux_cache[0] < USBMUX_TTL:
            return _usbmux_cache[1]
    base, _ = find_pmd3()
    if not base:
        return []
    try:
        out = subprocess.run(base + ["usbmux", "list"],
                             capture_output=True, text=True, timeout=8)
        raw = json.loads(out.stdout or "[]")
        devs = [{"udid": d.get("Identifier") or d.get("UniqueDeviceID"),
                 "name": d.get("DeviceName", "")}
                for d in raw if d.get("ConnectionType") == "USB"]
        devs = [d for d in devs if d["udid"]]
    except Exception:
        return []
    with _usbmux_lock:
        _usbmux_cache = (time.time(), devs)
    return devs


def _invalidate_usbmux():
    global _usbmux_cache
    with _usbmux_lock:
        _usbmux_cache = None


# ---------------- 单实例 ----------------
class _Instance:
    """一台真机对应的 pmd3 webinspector cdp 子进程。"""

    def __init__(self, udid, port):
        self.udid = udid
        self.port = port
        self.proc = None
        self.log_fh = None
        self.running = False
        self.starting = False
        self.error = None
        self.started_at = 0.0

    def _cmd(self, base):
        # 显式 --udid，多台真机互不串线
        return base + ["webinspector", "cdp",
                       "--port", str(self.port), "--udid", self.udid]

    def start(self, base):
        """spawn + 健康轮询（调用方持锁，本方法不做加锁）。成功置 running。"""
        self._open_log()
        try:
            self._precheck_port()
            cmd = self._cmd(base)
            self._log("launch: " + " ".join(cmd))
            self.proc = subprocess.Popen(
                cmd, stdout=self.log_fh, stderr=subprocess.STDOUT,
                start_new_session=True,   # 独立进程组，stop 时整组清理
            )
            self._wait_healthy()
            if self.proc.poll() is not None:
                raise BridgeError("pymobiledevice3 进程已退出，见 %s" % BRIDGE_LOG)
            self.running = True
            self.started_at = time.time()
            self.error = None
            self._log("instance ready %s on :%d" % (self.udid, self.port))
        except Exception as e:
            self._log("start failed %s: %s" % (self.udid, e))
            self._kill_proc()
            self.error = str(e)
        finally:
            self.starting = False

    def _precheck_port(self):
        """启动前探测端口空闲：被占用则直接报错（避免 spawn 后 bind 失败、
        health check 误打到占用方进程导致「假 running」）。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", self.port))
        except OSError:
            raise BridgeError(
                "端口 %d 已被其它进程占用，请释放或用 H5TOOL_IOS_PORT 调整基端口"
                % self.port)
        finally:
            s.close()

    def _wait_healthy(self):
        """轮询本实例 /json/list 直到可响应（进程异常退出则立即失败）。"""
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise BridgeError("pymobiledevice3 提前退出，见 %s" % BRIDGE_LOG)
            try:
                _no_proxy_get_json(
                    "http://127.0.0.1:%d/json/list" % self.port)
                return
            except Exception:
                time.sleep(0.8)
        raise BridgeError("实例启动超时（%ds），见 %s" % (START_TIMEOUT, BRIDGE_LOG))

    def stop(self):
        self.starting = False
        self.running = False
        self._kill_proc()
        if self.log_fh:
            try:
                self.log_fh.close()
            except Exception:
                pass
            self.log_fh = None

    def _kill_proc(self):
        proc = self.proc
        self.proc = None
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

    def _open_log(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            self.log_fh = open(BRIDGE_LOG, "a", encoding="utf-8")
        except Exception:
            self.log_fh = None   # 打不开日志不阻塞启动

    def _log(self, line):
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            if self.log_fh:
                self.log_fh.write("%s %s\n" % (ts, line))
                self.log_fh.flush()
        except Exception:
            pass


class IOSBridge:
    """pmd3 CDP 桥进程组管理（每台真机一个实例，模块级单例）。"""

    def __init__(self, base_port=IOS_BRIDGE_PORT):
        self.base_port = base_port
        self._lock = threading.Lock()
        self._instances = {}       # udid -> _Instance

    # ---------------- 状态 ----------------
    def status(self):
        """轻量状态（不 spawn、不探测网络），供 /api/devices 等展示。"""
        tool, _ = find_pmd3()
        with self._lock:
            insts = list(self._instances.values())
        running = any(i.running for i in insts)
        starting = any(i.starting for i in insts)
        port = None
        for i in insts:                       # 首个 running / 首个分配端口
            if i.running:
                port = i.port
                break
        if port is None and insts:
            port = insts[0].port
        error = next((i.error for i in insts if i.error), None)
        return {
            "supported": supported(),
            "tool": bool(tool),
            "tool_name": ENGINE_NAME,
            "tool_cmd": " ".join(tool) if tool else None,
            "running": running,
            "starting": starting,
            "port": port,
            "error": error,
        }

    def is_running(self):
        with self._lock:
            return any(i.running for i in self._instances.values())

    def port_for(self, udid=None):
        """实例端口：udid 指定查对应实例；None 返回首个 running 实例端口。"""
        with self._lock:
            if udid:
                i = self._instances.get(udid)
                return i.port if i and i.running else None
            for i in self._instances.values():
                if i.running:
                    return i.port
        return None

    # ---------------- 生命周期 ----------------
    def ensure_started(self, udid=None):
        """幂等触发启动（后台线程执行，立即返回）。

        每次触发先扫 usbmux 设备表；无设备不 spawn（不产生无意义的
        zombie 进程），错误通过 status()/devices 空态表达。
        """
        devs = _usbmux_devices()
        if udid and not any(d["udid"] == udid for d in devs):
            return
        targets = [d["udid"] for d in devs]
        if udid:
            targets = [udid]
        if not targets:
            return
        with self._lock:
            # 端口按设备序从基端口顺延；已存在实例沿用旧端口（插拔不漂移）
            insts = []
            for idx, uid in enumerate(targets):
                inst = self._instances.get(uid)
                if inst is None:
                    inst = _Instance(uid, self.base_port + idx)
                    self._instances[uid] = inst
                insts.append(inst)
            for inst in insts:
                if inst.running or inst.starting:
                    continue
                base, _ = find_pmd3()
                if not base:
                    inst.error = ("未找到 pymobiledevice3 引擎。安装："
                                  "brew install pymobiledevice3 或 "
                                  "pipx install pymobiledevice3")
                    continue
                inst.starting = True
                inst.error = None
                threading.Thread(target=self._start_worker,
                                 args=(inst, base), daemon=True).start()

    def _start_worker(self, inst, base):
        try:
            inst.start(base)
        except Exception as e:
            with self._lock:
                inst.error = str(e)
                inst.starting = False

    def stop_all(self):
        """停止全部实例（server.py 退出时 atexit 调用）。幂等。"""
        with self._lock:
            insts = list(self._instances.values())
            self._instances.clear()
        for inst in insts:
            inst.stop()
        _invalidate_usbmux()

    # ---------------- 目标列表 ----------------
    def targets(self, udid=None):
        """拉取实例 /json/list 并精简。全部未就绪返回 None。

        udid 指定 → 只查该实例；None → 聚合所有 running 实例（每个
        target 注入 device=<udid>，供前端按设备分组）。
        """
        with self._lock:
            keys = [udid] if udid else list(self._instances)
            insts = [self._instances[u] for u in keys if u in self._instances]
        out, got = [], False
        for inst in insts:
            if not inst.running:
                continue
            try:
                pages = _no_proxy_get_json(
                    "http://127.0.0.1:%d/json/list" % inst.port, timeout=4.0)
            except Exception:
                continue
            got = True
            for p in pages or []:
                out.append({
                    "id": p.get("id"),
                    "device": inst.udid,       # 归属实例 udid（分组用）
                    "title": p.get("title", ""),
                    "url": p.get("url", ""),
                    "type": p.get("type", "page"),
                    "ws": p.get("webSocketDebuggerUrl"),
                    "frontend": p.get("devtoolsFrontendUrl"),
                    "app": None,               # pmd3 无 app 信息（WIR 层面缺）
                })
        return out if got else None

    def devices(self):
        """usbmux 设备列表 [{key, scope, id}]（供 header 设备 tab）。

        与 target 归属的 udid 一致；实例未启动也可展示（点击即懒启动）。
        """
        devs = _usbmux_devices()
        return [{"key": d["udid"], "scope": "device", "id": d["udid"]}
                for d in devs]


# 模块级单例 + server.py 可直接使用的函数
bridge = IOSBridge()


def status():
    return bridge.status()


def ensure_started(udid=None):
    bridge.ensure_started(udid)


def is_running():
    """是否有实例运行中（server.py 代理路由判断用）。"""
    return bridge.is_running()


def targets(udid=None):
    """拉取桥目标列表（全部未就绪返回 None）。"""
    return bridge.targets(udid)


def devices():
    """拉取 usbmux 真机列表（无设备返回空列表）。"""
    return bridge.devices()


def port_for(udid=None):
    """实例端口查询（iOS 代理路由用）。"""
    return bridge.port_for(udid)


def stop_all():
    """停止全部实例（server.py 退出时 atexit 调用）。"""
    bridge.stop_all()
