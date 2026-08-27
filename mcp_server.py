#!/usr/bin/env python3
"""
h5-tool MCP server —— 把本地 h5-tool 后端的 HTTP 接口封装成 MCP 工具。

让 AI（WorkBuddy 等支持 MCP 的客户端）可以直接：
  - 获取手机画面（截图，以图片形式返回，AI 能"看"到）
  - 在画面上点击 / 滑动 / 按键 / 输入文本
  - 发送 H5 链接到手机、在 WebView 里执行 JS、查设备状态

本进程只作为 HTTP 客户端访问 h5-tool 后端（默认 127.0.0.1:12787），
不直接调用 adb，因此运行环境不需要 PATH 里有 adb。

依赖：mcp[cli]（装在 workbuddy managed venv 中）
启动：python mcp_server.py   （stdio 传输，由 MCP 客户端拉起）
"""
import json
import os
import urllib.parse
import urllib.request
from mcp.server.fastmcp import FastMCP, Image

# 后端地址，可用环境变量 H5_TOOL_URL 覆盖（后端换端口时无需改本文件）。
# 例：H5_TOOL_URL=http://127.0.0.1:12999 python mcp_server.py
BASE = os.environ.get("H5_TOOL_URL", "http://127.0.0.1:12787")

mcp = FastMCP("h5-tool")


def _get(path, device=None):
    url = f"{BASE}{path}"
    if device:
        sep = "&" if "?" in url else "?"
        url += f"{sep}device={urllib.parse.quote(device)}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read(), r.headers.get("Content-Type", "")


def _post(path, payload, device=None):
    data = json.dumps(payload).encode("utf-8")
    url = f"{BASE}{path}"
    if device:
        sep = "&" if "?" in url else "?"
        url += f"{sep}device={urllib.parse.quote(device)}"
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


@mcp.tool()
def h5_devices() -> str:
    """列出当前连接的所有 Android 设备（serial + model），并标注默认设备。"""
    try:
        body, _ = _get("/api/devices")
        return body.decode("utf-8", "replace")
    except Exception as e:
        return f"获取设备列表失败：{e}（h5-tool 后端未启动？）"


@mcp.tool()
def h5_status(device: str = None) -> str:
    """查看指定设备（默认第一台）的连接状态、WebView 状态、屏幕分辨率、scrcpy 流状态。"""
    try:
        body, _ = _get("/api/status", device)
        return body.decode("utf-8", "replace")
    except Exception as e:
        return f"获取状态失败：{e}（h5-tool 后端未启动？）"


@mcp.tool()
def h5_screenshot(device: str = None) -> Image:
    """截取指定设备（默认第一台）当前全屏画面并以图片形式返回。

    ⚠️ 重要：返回的 PNG 经 scrcpy 采集，通常是【降采样】过的，
    分辨率往往【小于】设备真实分辨率（例如设备 1080x2400，截图只有 900x2000）。
    因此【图片像素坐标 ≠ 设备真实坐标】，直接拿截图上读到的坐标去 h5_tap 会整体偏移。

    正确用法（按比例映射到设备真实坐标）：
      1. 调用 h5_status 拿到设备真实分辨率 W(宽)、H(高)；
      2. 在截图上读到目标点 (xi, yi)，并知道截图尺寸 (wi, hi)；
      3. 换算后再传给 h5_tap / h5_swipe：
             x = round(xi / wi * W)
             y = round(yi / hi * H)
    对水平/垂直居中的元素，直接用 x = W/2 或 y = H/2 更稳。
    """
    data, ctype = _get("/api/screenshot", device)
    fmt = "png" if "png" in ctype else "png"
    return Image(data=data, format=fmt)


@mcp.tool()
def h5_tap(x: int, y: int, device: str = None) -> str:
    """在指定设备（默认第一台）屏幕的【设备真实坐标】(x, y) 处点击。

    ⚠️ 这里的 x/y 必须是设备真实分辨率下的坐标（见 h5_status 的 screen.width/height），
    而【不是】h5_screenshot 截图上的像素坐标——截图通常被降采样，二者不一致。
    若坐标来自截图，请先按 h5_screenshot 文档里的公式换算：
        x = xi / wi * W，  y = yi / hi * H
    """
    try:
        return _post("/api/tap", {"x": int(x), "y": int(y)}, device)
    except Exception as e:
        return f"点击失败：{e}"


@mcp.tool()
def h5_swipe(x1: int, y1: int, x2: int, y2: int, dur: int = 200,
             device: str = None) -> str:
    """在指定设备（默认第一台）从 (x1,y1) 滑动到 (x2,y2)，dur 为滑动时长(毫秒，默认 200)。

    坐标同样是【设备真实坐标】，不是截图像素；若来自 h5_screenshot 请先按其文档公式换算。
    """
    try:
        return _post("/api/swipe", {
            "x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2),
            "dur": int(dur),
        }, device)
    except Exception as e:
        return f"滑动失败：{e}"


@mcp.tool()
def h5_press_key(code: int = 4, device: str = None) -> str:
    """在指定设备（默认第一台）模拟系统按键。code: 4=返回, 3=主页, 66=回车, 67=删除。"""
    try:
        return _post("/api/key", {"code": int(code)}, device)
    except Exception as e:
        return f"按键失败：{e}"


@mcp.tool()
def h5_type_text(text: str, device: str = None) -> str:
    """向指定设备（默认第一台）当前焦点输入框输入文本（空格会被自动转义为 %s）。"""
    try:
        return _post("/api/text", {"text": text}, device)
    except Exception as e:
        return f"输入失败：{e}"


@mcp.tool()
def h5_navigate(url: str, device: str = None) -> str:
    """让指定设备（默认第一台）当前 WebView 导航到指定 H5 链接（自动补全 https://）。"""
    try:
        return _post("/api/navigate", {"url": url}, device)
    except Exception as e:
        return f"导航失败：{e}"


@mcp.tool()
def h5_eval(expression: str, device: str = None) -> str:
    """在指定设备（默认第一台）WebView 当前页面上下文执行 JS，返回 {value,type}。"""
    try:
        return _post("/api/eval", {"expression": expression}, device)
    except Exception as e:
        return f"执行 JS 失败：{e}"


if __name__ == "__main__":
    mcp.run()
