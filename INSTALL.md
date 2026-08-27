# H5 小工具 · Agent 安装运维手册

> 本文档面向 **AI Agent**：读完即可在一台新机器上完成安装、启动、验证，
> 并在遇到问题时按「报错 → 原因 → 解法」自行排障。
> 面向人的说明见 `README.md`。

---

## 1. 项目是什么（先理解原理，才好排障）

单进程 Python 服务，**纯标准库实现，无任何第三方 Python 依赖**（CDP 的 WebSocket
是自己用 socket 手写的）。它通过 **ADB + CDP** 控制手机 App 里的 WebView，给用户一个
浏览器里的常驻控制台：发链接、执行 JS、屏幕镜像、以及内嵌完整 Chrome DevTools 调试面板。

### 关键端口

| 端口 | 用途 |
|------|------|
| **12787** | 本服务 HTTP 控制台（`http://127.0.0.1:12787/`） |
| **9222**  | 手机 WebView 的 CDP 调试端口（`adb forward tcp:9222 localabstract:webview_devtools_remote_<pid>`） |
| **27183** | scrcpy 视频流（设备端 H.264 → 浏览器 WebCodecs） |
| **9333**  | （可选）本机 Chrome 调试端口（Chrome Debug.app 启动） |

### 核心文件

| 文件 | 职责 |
|------|------|
| `server.py` | HTTP 服务 + 路由（/api/*、/devtools/、/cdp-ws/） |
| `cdp.py` | 纯 Python CDP 客户端（WebSocket 握手 + Page.navigate / Runtime.evaluate） |
| `scr_stream.py` | scrcpy 视频流（SPS/PPS 缓存、中断降级） |
| `web/` | 前端页面（index.html 控制台、devtools.html 调试入口） |
| `devtools-local/front_end/` | devtools-frontend 构建产物（438MB，已 gitignore，可用远程替代） |
| `install.sh` / `install.bat` | 一键安装脚本 |
| `run.sh` / `run.bat` | 启动脚本 |

### 依赖清单

| 依赖 | 版本要求 | 必需？ | 用途 |
|------|---------|--------|------|
| Python | >= 3.8 | **必需** | 运行服务（纯标准库，无需 pip 装包） |
| adb | 任意 | **必需** | 手机控制/CDP/截图/镜像 |
| Node.js | >= 18 | 可选 | 仅重建 devtools-frontend 产物时需要 |
| devtools 产物 | — | 可选 | WebView 调试面板；缺失不影响其余功能 |

---

## 2. 安装

### 2.1 一键脚本（推荐）

```bash
# macOS / Linux / Windows Git Bash / WSL
./install.sh
# 跳过 Node 检查（不需要 devtools 构建时）：
./install.sh --skip-node
# 输出每条检测明细：
./install.sh --verbose

# Windows cmd / PowerShell
install.bat
install.bat --skip-node
```

脚本执行 6 步，任何一步 FAIL 会退出并给出提示：

1. **查找 Python**：`venv` → 环境变量 `PYTHON` → workbuddy managed python（macOS 用户环境）
   → `python3` / `python` / `py`
2. **校验版本**：>= 3.8，过低 FAIL
3. **查找 Node.js**（可选）：>= 18 通过；缺失/过低只 WARN；`--skip-node` 跳过
4. **查找 adb**：缺失 WARN（手机功能不可用）
5. **安装 Python 依赖**：有 `requirements.txt` 才装（本项目通常没有）
6. **校验**：devtools 产物存在性 + `py_compile` 服务代码语法

### 2.2 手动安装（脚本不可用时的兜底）

```bash
# 1) 确认 Python
python3 --version        # >= 3.8；Windows 可能是 py -3 / python
# 2) 确认 adb
adb devices              # 应能看到设备（手机需开 USB 调试并授权）
# 3) 本项目无第三方 Python 依赖，不需要 pip install。
#    若有 requirements.txt 才需要：python3 -m pip install -r requirements.txt
# 4) devtools 产物（可选）：
#    - 本地：把 front_end 目录放到 devtools-local/front_end/
#    - 远程：export DEVTOOLS_DIR=http://服务器:端口/devtools （见第 5 节）
```

---

## 3. 启动

```bash
./run.sh                    # macOS / Linux / Git Bash（自动探测 Python）
run.bat                     # Windows
# 或手动指定：
python3 server.py --host 127.0.0.1 --port 12787
```

`run.sh` 的 Python 探测优先级：环境变量 `PYTHON` → 项目 `venv` → workbuddy managed python
（macOS）→ 系统 `python3`/`python`/`py`。支持 `py -3` 这类带参数的命令。

启动成功后浏览器打开 <http://127.0.0.1:12787/>。

---

## 4. 验证安装成功（检查清单）

按顺序执行，全部通过即安装成功：

```bash
# ① 服务活着
curl -s http://127.0.0.1:12787/api/status
#    期望 JSON 含 device_connected、webview 等字段，如：
#    {"device":"xxxx","device_connected":true,"webview":true,"current_url":"http://...","screen":{"width":1080,"height":2400}}

# ② 控制台页面
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:12787/          # 200

# ③ devtools 面板（可选，devtools 产物存在时）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:12787/devtools/inspector.html   # 200

# ④ 调试目标列表
curl -s http://127.0.0.1:12787/api/webview-targets
#    期望 phone 数组里有 target（手机 WebView 在线且开启调试时）

# ⑤ CDP 连通（可选，用 node 或任意 WebSocket 客户端）
#    连 ws://127.0.0.1:12787/cdp-ws/<targetId> 发 Runtime.evaluate 应能拿到结果
```

关键判定：`device_connected:true`（手机在）且 `webview:true`（WebView 可调试）。
若 `webview:false`，看 `webview_error` 字段，通常是 App 未开 WebView 调试。

---

## 5. 环境变量

| 变量 | 作用 | 示例 |
|------|------|------|
| `DEVTOOLS_DIR` | devtools 产物来源。**以 `http://`/`https://` 开头 = 远程反代模式**，否则当本地目录；不设置默认 `devtools-local/front_end/` | `DEVTOOLS_DIR=/data/front_end` 或 `DEVTOOLS_DIR=http://10.0.0.5:8080/devtools` |
| `PYTHON` | 指定 python 解释器（run.sh 优先用它） | `PYTHON=/usr/bin/python3 ./run.sh` |

远程模式下 h5-tool 自动从服务器拉取资源并缓存到 `devtools-local/.remote-cache/`，
前端仍统一走本地 `/devtools/`，服务器不可达时仅面板不可用、其余功能不受影响。

---

## 6. 常见问题排查（报错 → 原因 → 解法）

### 安装阶段

| 报错 / 现象 | 原因 | 解法 |
|------------|------|------|
| `未找到 Python` / `command not found: python3` | 未装或不在 PATH | 装 Python 3.8+（Windows 勾选 Add to PATH）；或设 `PYTHON=/path/to/python3` |
| `Python 版本过低: x.x.x，需要 >= 3.8` | 系统 python 太老 | 换新版本；macOS 注意 brew 装的可能是 3.9+，检查 `which python3` |
| `未找到 adb` | 没装 Android Platform Tools | 安装并加入 PATH；验证 `adb devices` |
| `adb devices` 空 | 手机未开 USB 调试 / 未授权 / 驱动 | 开发者选项开 USB 调试；手机弹窗点允许；换线/USB 直连 |

### 服务启动阶段

| 报错 / 现象 | 原因 | 解法 |
|------------|------|------|
| `OSError: [Errno 48] Address already in use` / `bind 127.0.0.1:12787` 失败 | 端口被占（常驻服务或上次实例没退） | `lsof -ti tcp:12787 \| xargs kill -9` 后重启；或 `--port` 换端口 |
| `curl /api/status` 拒绝连接 | 服务没起来 | 看启动输出/日志（macOS 常驻见第 7 节）；确认端口 |
| macOS 上服务"时好时坏" | LaunchAgent 用极简 PATH，找不到 adb | plist 已内置 `PATH=/opt/homebrew/bin:...`；改过 plist 需 bootout+bootstrap |

### WebView / CDP 阶段

| 报错 / 现象 | 原因 | 解法 |
|------------|------|------|
| 控制台右上角 WebView 红灯，`webview_error: 未找到 WebView` | App 未开启 `WebView.setWebContentsDebuggingEnabled(true)`；或当前没打开 H5 页面 | 让客户端开调试开关（仅测试包）；先打开 H5 页面再刷新 |
| `webview_error: 未检测到已连接的设备` | adb devices 为空 | 见安装阶段 adb 条目 |
| 执行 JS 报 `CDP 命令超时` | WebView 刚重建 / 页面在跳转 | 自动重连一次后仍失败就等 1-2s 重试；页面导航后 target 会变 |
| `Runtime.evaluate` 结果不符预期 | 页面上下文限制 | 确认表达式在页面上下文（非 iframe）；可先 `location.href` 验证 |

### devtools 调试面板阶段

| 报错 / 现象 | 原因 | 解法 |
|------------|------|------|
| `/devtools/inspector.html` 返回 `devtools 资源未构建` | 产物缺失且未设 DEVTOOLS_DIR | 放本地产物到 `devtools-local/front_end/`，或设远程 URL（第 5 节） |
| 返回 `devtools 远程获取失败: ...` | 远程服务器不可达 / 路径 404 | 检查服务器、nginx 配置、`DEVTOOLS_DIR` URL 末尾路径 |
| DevTools 面板打不开 / iframe 空白 | inspector.html 资源 404 | 确认产物目录结构完整（`entrypoints/` 等相对路径不能被改动） |
| 控制台报 `Connecting to 'ws://ws//127.0.0.1:9222/...' violates CSP` | **`?ws=` 参数带了协议**，devtools 前端会自己加 `ws://` 前缀导致双协议 | 入口 URL 的 ws 参数必须是 `127.0.0.1:12787/cdp-ws/<targetId>`（不带 `ws://`） |
| 控制台报 `WebSocket connection to 'ws://127.0.0.1:9222/...' failed`（无 CSP 字样） | **直连 9222 被 Origin 校验拒绝**（Chrome 111+，手机 WebView 也校验）：浏览器 iframe 带 Origin 会被 403 | 必须走 `/cdp-ws/<targetId>` 本地代理（代理用无 Origin 连接转发到 9222）；确认代理地址正确 |
| 报 `Executing inline script violates CSP 'script-src ...'`（prepare.js） | 浏览器扩展/脚本管理器注入的脚本被 devtools 页面 CSP 拦 | **无害**，devtools 自身不需要 inline script；忽略即可 |
| DevTools 面板显示但连不上目标 | target id 过期（页面导航/WebView 重建） | 回 `devtools.html` 列表重新点「内嵌打开」 |
| 本机 Chrome（9333）不在目标列表 | 9333 调试端口没开 | 用 Chrome Debug.app 启动（带 `--remote-debugging-port=9333 --user-data-dir`）；确认带 `--remote-allow-origins` |

### devtools 产物重建（可选，仅升级面板时需要）

```bash
cd ~/Desktop/code/devtools-frontend && git pull
# 关键：必须先去掉 WorkBuddy/CodeBuddy 的 safe-delete 环境变量，
# 否则 TS 编译删除 tsbuildinfo 会被拦（报 [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]）
unset CODEBUDDY_SAFE_DELETE_BULK_GUARD CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR
PATH=$(echo "$PATH" | tr ':' '\n' | grep -v safe-bin | paste -sd: -) \
  node scripts/run_build.mjs     # 产物在 out/Default/gen/front_end
rsync -a out/Default/gen/front_end/ <h5-tool>/devtools-local/front_end/
# e2e 的 wasm 编译失败（缺 emscripten）可忽略，不影响前端产物
```

---

## 7. macOS 常驻（LaunchAgent）运维

服务以 `com.aidog.h5-tool` LaunchAgent 常驻，登录自启、崩溃自拉起，日志在
`<h5-tool>/h5-tool.log`（plist：`~/Library/LaunchAgents/com.aidog.h5-tool.plist`）。

```bash
# 重启（代码改动后生效）
launchctl kickstart -k gui/$(id -u)/com.aidog.h5-tool
# 查看状态
launchctl print gui/$(id -u)/com.aidog.h5-tool
# 看日志
tail -f ~/Desktop/code/h5-tool/h5-tool.log
```

注意：plist 里 ProgramArguments 直接指定了 python 绝对路径；改 plist 需
`launchctl bootout` + `bootstrap`。

---

## 8. 服务器部署 devtools（可选）

devtools 产物是纯静态文件，可只部署一份到服务器共享：

```nginx
# 服务器 nginx
location /devtools/ {
    alias /opt/devtools-frontend/front_end/;
    index inspector.html;
}
```

本机 `export DEVTOOLS_DIR=http://服务器IP:端口/devtools` 后重启服务即可（远程反代 + 本地缓存）。

---

## 9. 一句话速查

- **安装**：`./install.sh`（Windows：`install.bat`）
- **启动**：`./run.sh`（Windows：`run.bat`）
- **验证**：`curl http://127.0.0.1:12787/api/status` 看 `device_connected` 和 `webview`
- **看日志**：macOS `tail -f h5-tool.log`；Windows 看启动终端输出
- **服务挂了**：
  - macOS：`lsof -ti tcp:12787 | xargs kill -9` 再重启
  - Windows：`netstat -ano | findstr :12787` 记下 LISTENING 的 PID → `taskkill /F /PID <pid>` 再重启
- **WebView 红灯**：让客户端开 `setWebContentsDebuggingEnabled(true)`，打开 H5 页面再试

---

## 10. Windows 专属注意事项

代码与脚本已做跨平台兼容（`kill_port` 走 netstat+taskkill、`resolve_host_ip` 走 ipconfig、
`/api/restart` 在 Windows 返回手动重启提示），以下为 Windows 上的额外注意点：

### 命令差异速查

| 场景 | macOS / Linux | Windows |
|------|---------------|---------|
| 启动 | `./run.sh` | `run.bat` 或 `py -3 server.py` |
| 安装 | `./install.sh` | `install.bat` |
| 找 Python | `python3` | `py -3` / `python`（装 Python 时勾选 Add to PATH） |
| 释放端口 | `lsof -ti tcp:PORT \| xargs kill -9` | `netstat -ano \| findstr :PORT` → `taskkill /F /PID <pid>` |
| 复制 devtools 产物 | `rsync -a src/ dst/` | `robocopy src dst /E`（或 `xcopy /E /I`） |
| 查日志 | `tail -f h5-tool.log` | `type h5-tool.log` / `Get-Content h5-tool.log -Wait`（PowerShell） |

### devtools 产物获取（Windows）

- **推荐**：不本地构建。要么用**远程模式**（`DEVTOOLS_DIR=http://服务器/devtools`，见第 5 节），
  要么从已有环境把 `front_end` 目录复制过来（`robocopy`）。
- 若坚持本地构建 devtools-frontend：Windows 需要 **Windows 版 depot_tools**（`fetch devtools-frontend`
  会拉 win 版 gn/ninja/node），构建命令同第 6 节，但 `unset` 一行改为：
  ```powershell
  Remove-Item Env:CODEBUDDY_SAFE_DELETE_BULK_GUARD, Env:CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR
  $env:PATH = ($env:PATH -split ';' | Where-Object { $_ -notmatch 'safe-bin' }) -join ';'
  node scripts/run_build.mjs
  ```

### 其他 Windows 注意点

- **`adb`**：确认在 PATH 中（`adb version`），手机 USB 驱动装好；Git Bash 里 `adb devices` 也能用。
- **路径分隔符**：`DEVTOOLS_DIR` 本地目录用 Windows 路径即可（如 `D:\devtools\front_end`），
  代码用 `pathlib.Path` 处理，无需转换；远程 URL 形态无平台差异。
- **控制台启动（`run.bat`）**：`Ctrl+C` 停止；不要关掉窗口（服务在窗口进程里）。
- **防火墙**：首次启动如果手机通过局域网 IP 访问（`{ip}` 占位符），Windows 防火墙可能拦截，
  允许 `python.exe` 入站即可。
- **scrcpy 镜像**：`scr_stream.py` 会把项目内 `scrcpy-server` 推送到手机，设备端逻辑与宿主机
  平台无关，Windows 上同样可用（依赖本机已装 `adb`）。

