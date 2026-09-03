#!/usr/bin/env node
/**
 * @fkjs/h5-tool 命令行入口 —— 启动 h5-tool Python 后端。
 *
 * 用法：
 *   h5-tool start [--port N] [--host H]   启动后端（前台运行，Ctrl+C 退出）
 *   h5-tool status                         检测后端是否在运行（HTTP 探测 12787）
 *   h5-tool stop                           停止本命令启动的后端（读 pid 文件）
 *
 * 原理：npx 从缓存执行本脚本时 cwd 是用户当前目录，所以用 __dirname 定位包内文件；
 * 后端 server.py 纯标准库，跨平台找本机 python 拉起即可。
 */

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const PKG_DIR = path.join(__dirname, ".."); // 包根（bin/ 的上一级）
const SERVER = path.join(PKG_DIR, "server.py");
const PID_FILE = path.join(os.tmpdir(), "h5-tool.pid");
const DEFAULT_PORT = 12787;

function findPython() {
  if (process.env.PYTHON) return { cmd: process.env.PYTHON, args: [] };
  if (process.platform === "win32") {
    // Windows：py -3 优先（launcher），其次 python
    const py = spawnSync("py", ["-3", "--version"], { stdio: "ignore" });
    if (py.status === 0) return { cmd: "py", args: ["-3"] };
    return { cmd: "python", args: [] };
  }
  const r = spawnSync("python3", ["--version"], { stdio: "ignore" });
  if (r.status === 0) return { cmd: "python3", args: [] };
  return { cmd: "python", args: [] };
}

function httpProbe(port, timeout = 800) {
  return new Promise((resolve) => {
    const req = require("http").get(
      { host: "127.0.0.1", port, path: "/api/status", timeout },
      (res) => resolve(res.statusCode === 200)
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

async function cmdStatus() {
  const ok = await httpProbe(DEFAULT_PORT);
  if (ok) {
    console.log(`✓ h5-tool 后端运行中（http://127.0.0.1:${DEFAULT_PORT}）`);
    process.exit(0);
  }
  console.log(`✗ 后端未运行（http://127.0.0.1:${DEFAULT_PORT} 无响应）`);
  process.exit(1);
}

async function cmdStop() {
  if (!fs.existsSync(PID_FILE)) {
    console.log("未找到 pid 文件（后端可能未由本命令启动）");
    return;
  }
  const pid = fs.readFileSync(PID_FILE, "utf-8").trim();
  try {
    process.kill(Number(pid), "SIGTERM");
    console.log(`已发送停止信号到 pid ${pid}`);
  } catch (e) {
    console.log(`停止失败或进程已退出：${e.message}`);
  }
  fs.rmSync(PID_FILE, { force: true });
}

function cmdStart(extraArgs) {
  const args = extraArgs.filter((a) => a !== "start");
  // 默认启动后自动在浏览器打开调试面板（服务器部署版）；可传 --no-open-browser 关闭
  if (!args.includes("--no-open-browser")) args.push("--open-browser");
  const portIdx = args.indexOf("--port");
  const port = portIdx >= 0 ? Number(args[portIdx + 1]) : DEFAULT_PORT;
  const serverPath = path.join(PKG_DIR, "server.py");
  const { cmd, args: pyArgs } = findPython();

  if (!fs.existsSync(serverPath)) {
    console.error("包内缺少 server.py，安装异常");
    process.exit(1);
  }

  const py = spawn(cmd, [...pyArgs, serverPath, ...args], {
    cwd: PKG_DIR, // server.py 用 Path(__file__).parent 定位 web/ 等，cwd 影响不大，但保持一致
    stdio: "inherit",
  });

  const pid = py.pid;
  fs.writeFileSync(PID_FILE, String(pid));

  // 启动后做一次健康探测（后端起得比 spawn 慢一点）
  let tried = 0;
  const probe = setInterval(async () => {
    tried++;
    if (await httpProbe(port, 500)) {
      clearInterval(probe);
      console.log(`✓ 后端已启动：http://127.0.0.1:${port}  （Ctrl+C 停止）`);
    } else if (tried > 20) {
      clearInterval(probe);
      console.error("✗ 健康检查超时，见上方日志");
    }
  }, 500);

  py.on("exit", (code, signal) => {
    clearInterval(probe);
    fs.rmSync(PID_FILE, { force: true });
    if (signal === "SIGINT" || signal === "SIGTERM") {
      console.log("\n后端已停止");
    } else {
      console.log(`\n后端退出（code=${code}）`);
    }
    process.exit(code ?? 0);
  });
}

async function main() {
  const cmd = process.argv[2] || "start";
  const rest = process.argv.slice(3);

  // start 前先探测：已在运行就不重复起（避免 12787 端口冲突，如 launchd 常驻场景）
  if (cmd === "start") {
    const port = rest.includes("--port") ? Number(rest[rest.indexOf("--port") + 1]) : DEFAULT_PORT;
    if (await httpProbe(port)) {
      console.log(`✓ 后端已在运行（http://127.0.0.1:${port}），无需重复启动`);
      return;
    }
  }

  switch (cmd) {
    case "start": cmdStart(rest); break;
    case "status": await cmdStatus(); break;
    case "stop": await cmdStop(); break;
    case "restart":
      await cmdStop();
      console.log("重启：请在 1 秒后重新执行 h5-tool start（前台进程无法原地重启）");
      break;
    default:
      console.log("用法：h5-tool start [--port N] | status | stop");
      process.exit(1);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
