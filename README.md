# H5 小工具

一个用来调试 Android App WebView 中 H5 页面的本地小工具。参考 `h5-verify` 技能的核心能力
（ADB + CDP 控制 WebView），做成一个**常驻的 Web 控制台**：浏览器打开即用，前端通过 HTTP
与宿主机上的 Python 后端通信。

支持两种运行模式：

- **本地单机**：前后端同机，`./run.sh` 启动后浏览器打开 <http://127.0.0.1:12787/>。
- **前后端分离**：前端部署到服务器（静态托管），后端仍跑在**使用者自己的电脑**上
  （`127.0.0.1:12787`）——因为后端要操作 USB 连接的手机（adb / CDP / scrcpy），必须在本地。
  浏览器打开服务器页面后，页面里的 JS 跨域访问本机后端：`127.0.0.1` 指向的是浏览器所在
  电脑，所以每个使用者连**自己**电脑上的手机，互不干扰，也不暴露到局域网。

## 能力

1. **发送 H5 链接到手机** — 让手机当前 WebView 导航到指定地址（CDP `Page.navigate`）。
2. **截图** — ADB 截取整屏，可下载 PNG。
3. **执行 JS 并获取返回值** — 在页面上下文执行任意 JS（CDP `Runtime.evaluate`，自动 await Promise）。
4. **实时屏幕镜像 + 模拟点击** — 支持 WebCodecs 的浏览器走 **scrcpy 硬件编码 + WebCodecs 解码**
   （30–60fps、低延迟，画到 `<canvas>`）；不支持时降级为 ADB 截图轮询（~3fps）。在画面上点击 =
   真机点击（`input tap`），拖动 = 滑动（`input swipe`），并提供「返回 / 主页」按键。
5. **WebView 调试（完整 Chrome DevTools）** — 复用 Chromium 开源的 devtools-frontend，浏览器内
   直接使用 Console / Sources 断点 / Network / Storage / Elements 全套调试能力调试手机 WebView。
   控制台右上角「🛠 调试」进入，选择目标后 iframe 内嵌 DevTools（也可新窗口打开）。

## 依赖

- `adb`（已在 PATH 中，且手机已连接、开启 USB 调试）
- App 的 WebView 需开启调试（`WebView.setWebContentsDebuggingEnabled(true)`）
- Python 3（无第三方依赖，纯标准库；CDP 的 WebSocket 是自己用 socket 实现的，不需要 node/ws）
- **scrcpy 视频流**：项目内已附带 `scrcpy-server`（与 scrcpy 4.0 配套）。实时镜像走 scrcpy
  硬件编码 + 浏览器 **WebCodecs** 解码。前端在支持 WebCodecs 的浏览器（Chrome / Edge）上使用
  scrcpy 管线（30–60fps、低延迟）；不支持时自动降级为原来的 PNG 轮询（约 3fps）。
- **输入（点击/滑动/按键）仍走原有 ADB**，不经过 scrcpy 控制通道。
- **devtools-frontend 产物**（WebView 调试面板用）：默认读 `devtools-local/front_end/`（构建产物，
  已 gitignore，不入库）。`DEVTOOLS_DIR` 环境变量支持两种形态：**本地目录** 或 **远程 URL**
  （如 `http://10.0.0.5:8080/devtools`，见下方「部署到服务器」），远程模式下自动拉取并缓存到
  `devtools-local/.remote-cache/`。缺失时 `/devtools/` 返回提示，不影响其他功能。

## 使用

> Agent / 自动化场景的完整安装与排障手册见 **`INSTALL.md`**（含全部报错→原因→解法对照）。

### 安装依赖（首次，跨平台一键）

```bash
./install.sh          # macOS / Linux / Windows Git Bash
# Windows cmd / PowerShell：
install.bat
```

安装脚本流程：查找 Python（支持 venv / 环境变量 `PYTHON` / 系统 python3）→ 校验版本（>= 3.8）
→ 查找 Node.js（可选，devtools 构建用）→ 查找 adb → 安装 Python 依赖（若有 requirements.txt）
→ 校验 devtools 产物 → 服务代码语法校验。跳过 Node 检查：`./install.sh --skip-node`。

### 启动

```bash
./run.sh              # macOS / Linux / Windows Git Bash（自动探测 Python）
run.bat               # Windows cmd / PowerShell
# 或： python3 server.py --host 127.0.0.1 --port 12787
```

启动后在浏览器打开 <http://127.0.0.1:12787/> 即可。

#### 前后端分离模式（前端在服务器）

前端（`web/` 三个页面 + `config.js`）部署到任意静态托管 / nginx，后端照常在本机 `./run.sh` 启动。
浏览器打开**服务器上**的页面，页面会请求本机 `127.0.0.1:12787` 的后端。

- 后端地址：`web/config.js` 里 `H5TOOL_CONFIG.backend`（默认 `http://127.0.0.1:12787`）；
  也可以点页面右上角「⚙ 后端」临时改地址（存 localStorage），或 URL 加 `?backend=` 覆盖。
- **Chrome 142+ 首次访问会弹「是否允许此网站访问本地网络」的授权框，点允许即可**（Chrome 的
  Local Network Access 安全策略；后端已返回 `Access-Control-Allow-Private-Network: true`）。
- 页面顶部黄色横幅「后端未连接」= 没连上后端，点横幅上的「⚙ 设置」检查地址。
- DevTools 调试面板资源默认走 `config.js` 里的 `devtoolsPanel`（远程静态托管），无需本地构建。

## 架构

```
浏览器控制台 (web/)  ──HTTP──▶  server.py (127.0.0.1:12787)
                                   ├─ ADB: screencap / input tap|swipe|keyevent
                                   ├─ CDP (cdp.py, 纯 Python WebSocket)
                                   │     └─ adb forward tcp:9222 → webview_devtools_remote
                                   │          ├─ Page.navigate     (发送链接)
                                   │          └─ Runtime.evaluate  (执行 JS)
                                   ├─ WebView 调试（devtools-frontend 复用）
                                   │     ├─ /devtools/ 静态托管（devtools-local/front_end）
                                   │     ├─ /api/webview-targets 目标列表（指定设备 WebView）
                                   │     └─ /cdp-ws/<targetId> WebSocket 代理
                                   │          └─ 绕过 WebView CDP 的 Origin 校验 → 9222
                                   └─ 视频流：scrcpy-server (设备端 MediaCodec 硬件 H.264)
                                         └─ adb forward tcp:27183 → 裸 H.264 字节流
                                              └─ /api/stream (chunked) → 浏览器 WebCodecs 解码到 <canvas>
```

**前后端分离模式的链路**（页面在服务器，后端在使用者本机）：

```
浏览器（使用者本机）
  ├─ HTTPS ──▶ 服务器静态托管（index.html / app.js / devtools.html / config.js）
  └─ fetch / WebSocket ──▶ http://127.0.0.1:12787（后端，使用者本机；跨域 CORS + PNA 已内置）
                              └─ adb / CDP / scrcpy ──▶ 手机（USB 连接使用者本机）
```

## 部署前端到服务器（前后端分离）

前端是纯静态的 4 个文件，可部署到任意静态托管 / nginx / 对象存储：

```bash
# 打包（或直接用项目根目录 dist/，已含全部最新文件）
mkdir -p dist && cp web/index.html web/app.js web/devtools.html web/config.js dist/
```

上传 `dist/` 下 4 个文件到静态托管**根目录**（CloudBase 静态托管 / CloudStudio / 公司 nginx 均可）：

| 文件 | 说明 |
|---|---|
| `index.html` | 控制台主页面 |
| `app.js` | 控制台逻辑（请求 `H5TOOL_CONFIG.backend`） |
| `devtools.html` | WebView 调试面板入口 |
| `config.js` | 全局配置：`backend`（默认 `http://127.0.0.1:12787`）、`devtoolsPanel`（远程 DevTools 资源） |

部署后使用者无需改任何配置：后端照常 `./run.sh` 本地启动，浏览器打开服务器页面即用。
首次访问 Chrome 可能弹「允许访问本地网络」授权框，点允许。

## HTTP 接口

> **多设备支持（v2）**：所有接口都支持 `device` 参数（GET 用 `?device=SERIAL`，
> POST 用 body 里的 `device` 字段），不传则默认操作 `adb devices` 第一台设备。
> 设备列表见 `GET /api/devices`。端口按设备序列号哈希分配固定槽位：
> CDP `9222+槽位`、scrcpy `27183+槽位`，多台设备互不冲突、互不干扰。
>
> **WebView socket 命名兼容**：标准 WebView 是 `@webview_devtools_remote_<pid>`，
> 但小米浏览器等会带前缀（`@browser_webview_devtools_remote_<pid>`），
> 已兼容两种命名，`adb forward localabstract` 使用完整 socket 名。

| 方法 | 路径              | 说明                                  |
|------|-------------------|---------------------------------------|
| GET  | `/api/devices`    | 设备列表（serial/model/product）+ 默认设备 |
| GET  | `/api/status`     | 指定设备状态（WebView / 分辨率 / scrcpy） |
| GET  | `/api/screenshot` | 返回 PNG（静态截图 / 降级镜像用）      |
| GET  | `/api/stream`     | chunked H.264 裸流，供 WebCodecs 解码（按设备懒启动） |
| GET  | `/api/webview-targets` | 可调试目标列表（指定设备 WebView） |
| GET  | `/cdp-ws/<targetId>` | WebSocket 代理：转发到指定设备 WebView CDP（绕过 Origin 校验） |
| GET  | `/devtools/*`     | devtools-frontend 静态资源（DevTools 面板） |
| POST | `/api/navigate`   | `{url, device?}` 导航当前 WebView      |
| POST | `/api/eval`       | `{expression, device?}` 执行 JS，返回 `{value,type}` |
| POST | `/api/tap`        | `{x,y, device?}` 设备坐标点击          |
| POST | `/api/swipe`      | `{x1,y1,x2,y2,dur, device?}` 滑动     |
| POST | `/api/key`        | `{code, device?}` 按键（back=4, home=3） |
| POST | `/api/text`       | `{text, device?}` 输入文本             |
| POST | `/api/restart`    | 重启本服务（launchctl kickstart，代码改动后生效） |

## WebView 调试（复用 Chrome DevTools）

控制台右上角「🛠 调试」进入 `web/devtools.html`，顶部可选择设备，列出该设备 WebView
的调试目标，点击「内嵌打开」在右侧 iframe 使用完整 DevTools
（Console / Sources 断点 / Network / Storage / Elements），或「新窗口」独立打开。
多台设备时多个浏览器标签各选一台即可并行调试。

工作原理：devtools-frontend（Chromium 开源前端）是纯 Web 应用，给它一个 CDP WebSocket 地址
（`inspector.html?ws=...`）即可工作。因为 Android WebView（Chrome 111+ 内核）的 CDP server
会校验 WebSocket Origin，浏览器 iframe 直连会被 403 拒绝，所以走本服务的 `/cdp-ws/<targetId>`
代理中转（代理用无 Origin 的连接转发到 9222）。

更新 devtools-frontend 产物（可选，通常不需要）：

```bash
# 源码在 ~/Desktop/code/devtools-frontend（GitHub ChromeDevTools/devtools-frontend）
cd ~/Desktop/code/devtools-frontend && git pull
# 构建（注意先去掉 WorkBuddy 的 safe-delete 环境变量，否则 TS 编译删文件会被拦）
unset CODEBUDDY_SAFE_DELETE_BULK_GUARD CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR
PATH=$(echo "$PATH" | tr ':' '\n' | grep -v safe-bin | paste -sd: -) \
  node scripts/run_build.mjs     # 产物在 out/Default/gen/front_end
# 复制到 h5-tool 的 devtools-local/front_end（或用 DEVTOOLS_DIR 指向产物目录）
rsync -a out/Default/gen/front_end/ ~/Desktop/code/h5-tool/devtools-local/front_end/
```

### 部署到服务器（可选，省本地磁盘）

devtools 产物是纯静态文件，可以只部署一份到服务器，多台机器共享：

```bash
# 服务器（Linux）上：把 front_end 目录用 nginx 托管
# nginx.conf
location /devtools/ {
    alias /opt/devtools-frontend/front_end/;
    index inspector.html;
}
# 然后本机设置环境变量（重启 h5-tool 生效）：
export DEVTOOLS_DIR=http://服务器IP:端口/devtools
```

远程模式下 h5-tool 会自动从服务器拉取资源并缓存到本地 `devtools-local/.remote-cache/`
（首次访问较慢，之后走本地缓存）。服务器不可达时面板不可用，其余功能不受影响。

> 前后端分离模式下，devtools 面板资源地址统一在 `web/config.js` 的 `devtoolsPanel` 配置
> （默认指向已部署的 CloudBase URL），前端直接引用远程面板，无需每台机器再设 `DEVTOOLS_DIR`。

已知限制：

- **仅 Android**：devtools-frontend 只认 CDP，iOS WKWebView 不适用（iOS 可用 vConsole/eruda 注入方案）。
- 手机 WebView 需开启 `setWebContentsDebuggingEnabled(true)` 才会出现在目标列表。
- 页面导航 / WebView 重建后 target id 会变，重新在列表里点目标即可。

## 后台常驻 / 开机自启（macOS launchd）

已配置为 LaunchAgent：登录时自动启动，进程挂掉会自动拉起，日志写到
`h5-tool/h5-tool.log`。配置文件：`~/Library/LaunchAgents/com.aidog.h5-tool.plist`。

> 注意：`plist` 里显式补了 `PATH=/opt/homebrew/bin:...`，否则 launchd 的极简环境找不到 `adb`。

常用管理命令（`gui/$(id -u)` 是当前登录用户的域）：

```bash
# 启动 / 加载（首次或改完 plist 后）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aidog.h5-tool.plist

# 停止 / 卸载（改 plist 前先 bootout，再 bootstrap）
launchctl bootout gui/$(id -u)/com.aidog.h5-tool

# 立即重启
launchctl kickstart -k gui/$(id -u)/com.aidog.h5-tool

# 查看状态（state / pid / last exit code）
launchctl print gui/$(id -u)/com.aidog.h5-tool

# 看日志
tail -f h5-tool/h5-tool.log
```

改了 `server.py` / `cdp.py` / `web/` 后，代码会在下次重启后生效：
`launchctl kickstart -k gui/$(id -u)/com.aidog.h5-tool`。
改了 `plist` 本身则需要先 `bootout` 再 `bootstrap`。

## 说明

- 镜像画面上的坐标会按图片真实分辨率换算为设备坐标，因此点击位置准确。
- WebView 重建（PID 变化）时，CDP 会自动重连一次。
- 若右上角 WebView 指示灯为红色，把鼠标悬停在上面可看到具体原因。
