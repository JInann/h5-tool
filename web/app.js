// H5 小工具控制台前端逻辑。与本机 py 后端（同源）通过 HTTP 通信。
const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const res = await fetch(path, opts);
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res;
}

function setMsg(el, text, ok) {
  el.textContent = text;
  el.className = "msg " + (ok ? "ok" : "err");
}

// ---------- 状态 ----------
async function refreshStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    $("dotDevice").className = "dot " + (s.device_connected ? "on" : "off");
    $("deviceLabel").textContent = s.device || "无设备";
    $("dotWebview").className = "dot " + (s.webview ? "on" : "off");
    if (s.screen) {
      screenSize = s.screen;
      $("resLabel").textContent = `${s.screen.width}×${s.screen.height}`;
    }
    if (s.webview === false && s.webview_error) {
      $("dotWebview").title = s.webview_error;
    }
  } catch (e) {
    $("dotDevice").className = "dot off";
    $("deviceLabel").textContent = "后端未连接";
  }
}
let screenSize = null;

// ---------- 1. 发送链接（多行 + 历史记录） ----------
const LS_ROWS = "h5tool.urlRows";
const LS_HISTORY = "h5tool.urlHistory";
const DEFAULT_ROWS = 5;

function loadRows() {
  try {
    const saved = JSON.parse(localStorage.getItem(LS_ROWS));
    if (Array.isArray(saved) && saved.length) return saved;
  } catch (e) {}
  return Array(DEFAULT_ROWS).fill("");
}
function saveRows() {
  const vals = [...document.querySelectorAll(".url-input")].map((i) => i.value);
  localStorage.setItem(LS_ROWS, JSON.stringify(vals));
}

let urlHistory = [];
try { urlHistory = JSON.parse(localStorage.getItem(LS_HISTORY)) || []; } catch (e) {}
function renderHistory() {
  $("urlHistory").innerHTML = urlHistory
    .map((u) => `<option value="${u.replace(/"/g, "&quot;")}"></option>`)
    .join("");
}
function pushHistory(url) {
  urlHistory = [url, ...urlHistory.filter((u) => u !== url)].slice(0, 30);
  localStorage.setItem(LS_HISTORY, JSON.stringify(urlHistory));
  renderHistory();
}

function addRow(value = "") {
  const row = document.createElement("div");
  row.className = "url-row";
  row.innerHTML =
    '<input class="url-input" list="urlHistory" placeholder="https://example.com/h5  或 http://{ip}:5173/xx.html" />' +
    '<button class="btn-send">发送</button>' +
    '<button class="ghost btn-del" title="删除此行">✕</button>';
  row.querySelector(".url-input").value = value;
  $("urlRows").appendChild(row);
}

async function sendUrl(input, btn) {
  const url = input.value.trim();
  if (!url) return setMsg($("navMsg"), "请输入链接", false);
  btn.disabled = true;
  setMsg($("navMsg"), "发送中…", true);
  try {
    const r = await api("/api/navigate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    let msg = "✓ 已发送到手机：" + r.url;
    if (r.replaced_ip) msg += `（已把 {ip} 替换为 ${r.ip}）`;
    setMsg($("navMsg"), msg, true);
    pushHistory(r.url);
  } catch (e) {
    setMsg($("navMsg"), "✗ " + e.message, false);
  } finally {
    btn.disabled = false;
  }
}

$("urlRows").addEventListener("click", (e) => {
  const row = e.target.closest(".url-row");
  if (!row) return;
  if (e.target.classList.contains("btn-send")) {
    sendUrl(row.querySelector(".url-input"), e.target);
  } else if (e.target.classList.contains("btn-del")) {
    row.remove();
    saveRows();
  }
});
$("urlRows").addEventListener("input", (e) => {
  if (e.target.classList.contains("url-input")) saveRows();
});
$("urlRows").addEventListener("keydown", (e) => {
  if (e.target.classList.contains("url-input") && e.key === "Enter") {
    sendUrl(e.target, e.target.closest(".url-row").querySelector(".btn-send"));
  }
});
$("btnAddRow").onclick = () => { addRow(""); saveRows(); };

loadRows().forEach((v) => addRow(v));
renderHistory();

// ---------- 2. 执行 JS ----------
$("btnEval").onclick = async () => {
  const expression = $("jsInput").value;
  if (!expression.trim()) return;
  const out = $("jsOut");
  out.style.display = "block";
  out.textContent = "执行中…";
  try {
    const r = await api("/api/eval", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expression }),
    });
    if (r.error) { out.textContent = "⚠ " + r.error; return; }
    let val = r.value;
    if (typeof val === "object") val = JSON.stringify(val, null, 2);
    out.textContent = `// type: ${r.type}${r.subtype ? " / " + r.subtype : ""}\n${val}`;
  } catch (e) {
    out.textContent = "✗ " + e.message;
  }
};
$("btnEvalClear").onclick = () => { $("jsOut").style.display = "none"; $("jsOut").textContent = ""; };
$("jsInput").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") $("btnEval").click();
});

// ---------- 3. 释放端口 ----------
const PRESET_PORTS = [5173, 5174, 5175, 8080, 8081];
$("portGrid").innerHTML = PRESET_PORTS
  .map((p) => `<button class="ghost port-btn" data-port="${p}">${p}</button>`)
  .join("");

async function killPort(port, btn) {
  port = String(port).trim();
  if (!port) return setMsg($("portMsg"), "请输入端口", false);
  if (btn) btn.disabled = true;
  setMsg($("portMsg"), `正在释放 ${port} …`, true);
  try {
    const r = await api("/api/kill-port", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ port: Number(port) }),
    });
    if (r.killed && r.killed.length) {
      const names = r.killed.map((k) => `${k.name}(${k.pid})`).join("、");
      setMsg($("portMsg"), `✓ 端口 ${port} 已释放：${names}`, true);
    } else {
      setMsg($("portMsg"), `· 端口 ${port} 未被占用`, true);
    }
  } catch (e) {
    setMsg($("portMsg"), "✗ " + e.message, false);
  } finally {
    if (btn) btn.disabled = false;
  }
}

$("portGrid").addEventListener("click", (e) => {
  const b = e.target.closest(".port-btn");
  if (b) killPort(b.dataset.port, b);
});
$("btnKillCustom").onclick = () => killPort($("portCustom").value, $("btnKillCustom"));
$("portCustom").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btnKillCustom").click(); });

// ---------- 3. 实时镜像 + 截图/复制/下载 + 点击 ----------
async function grabScreenshot() {
  const res = await fetch("/api/screenshot?t=" + Date.now());
  if (!res.ok) throw new Error("截图失败 HTTP " + res.status);
  return await res.blob();
}

async function copyBlobToClipboard(blob) {
  if (!navigator.clipboard || !window.ClipboardItem) {
    throw new Error("当前浏览器不支持图片剪贴板");
  }
  await navigator.clipboard.write([
    new ClipboardItem({ [blob.type || "image/png"]: blob }),
  ]);
}

// 截图 = 抓一帧 PNG + 复制到剪贴板 + 可下载（不干扰实时画面）
let lastPngBlob = null;
$("btnShot").onclick = async () => {
  $("btnShot").disabled = true;
  setMsg($("shotMsg"), "截图中…", true);
  try {
    const blob = await grabScreenshot();
    lastPngBlob = blob;
    $("btnShotDl").disabled = false;
    try {
      await copyBlobToClipboard(blob);
      setMsg($("shotMsg"), "✓ 已截图并复制到剪贴板", true);
    } catch (e) {
      setMsg($("shotMsg"), "✓ 已截图（复制失败：" + e.message + "）", false);
    }
  } catch (e) {
    setMsg($("shotMsg"), "✗ " + e.message, false);
  } finally {
    $("btnShot").disabled = false;
  }
};

$("btnShotDl").onclick = () => {
  if (!lastPngBlob) return;
  const url = URL.createObjectURL(lastPngBlob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "screenshot-" + Date.now() + ".png";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
const screen = $("screen");
const screenEmpty = $("screenEmpty");
const canvas = $("scrcpyCanvas");
const scrcpyCtx = canvas.getContext("2d");
let living = false, liveTimer = null, curObjUrl = null;
const FPS = 3, INTERVAL = 1000 / FPS;
// WebCodecs 能力检测：用于决定是否走 scrcpy 硬件编码管线
const HAS_WC = !!(window.VideoDecoder && window.VideoFrame && window.EncodedVideoChunk);

let lastBlob = null; // 当前显示的那一帧（legacy 模式）
function showFrame(blob) {
  lastBlob = blob;
  const url = URL.createObjectURL(blob);
  screen.onload = () => { if (curObjUrl) URL.revokeObjectURL(curObjUrl); curObjUrl = url; };
  screen.src = url;
  screen.style.display = "block";
  screenEmpty.style.display = "none";
}

// 静默刷新一帧（legacy 模式）：点击/滑动后立即回显
async function refreshFrameQuiet() {
  try { showFrame(await grabScreenshot()); } catch (e) { /* 忽略 */ }
}

// 当前可见画面元素与内部分辨率（scrcpy 用 canvas，legacy 用 img）
function activeSurface() {
  return (engine === "scrcpy" && canvas.style.display !== "none") ? canvas : screen;
}
function activeIntrinsic() {
  if (engine === "scrcpy") return videoSize;
  return {
    w: screen.naturalWidth || (screenSize && screenSize.width),
    h: screen.naturalHeight || (screenSize && screenSize.height),
  };
}
function toDeviceCoords(clientX, clientY) {
  const el = activeSurface();
  const rect = el.getBoundingClientRect();
  const intr = activeIntrinsic();
  const nx = (clientX - rect.left) / rect.width;
  const ny = (clientY - rect.top) / rect.height;
  return { x: Math.round(nx * (intr.w || 1)), y: Math.round(ny * (intr.h || 1)) };
}

// ---------------- scrcpy 引擎（WebCodecs 解码 H.264） ----------------
let engine = null;            // "scrcpy" | "legacy"
let videoSize = { w: 0, h: 0 };
let scrcpyPlayer = null;
let scrcpyFrameCount = 0, scrcpyFpsTimer = null;

function createScrcpyPlayer(canvasEl, ctx) {
  let decoder = null, reader = null;
  let sps = null, pps = null, configured = false;
  let ts = 0;
  let buf = new Uint8Array(0);

  const concat = (a, b) => { const n = new Uint8Array(a.length + b.length); n.set(a, 0); n.set(b, a.length); return n; };
  function findSC(b, from) {
    for (let i = from; i + 3 < b.length; i++) {
      if (b[i] === 0 && b[i + 1] === 0) {
        if (b[i + 2] === 1) return { start: i, end: i + 3 };
        if (b[i + 2] === 0 && b[i + 3] === 1) return { start: i, end: i + 4 };
      }
    }
    return null;
  }
  // 用 SPS/PPS 拼出 avcC 描述，供 WebCodecs 配置解码器
  function buildAvcC(sps, pps) {
    const d = new Uint8Array(11 + sps.length + pps.length);
    let o = 0;
    d[o++] = 0x01; d[o++] = sps[1]; d[o++] = sps[2]; d[o++] = sps[3]; d[o++] = 0xFF; d[o++] = 0xE1;
    d[o++] = (sps.length >> 8) & 0xff; d[o++] = sps.length & 0xff; d.set(sps, o); o += sps.length;
    d[o++] = 0x01; d[o++] = (pps.length >> 8) & 0xff; d[o++] = pps.length & 0xff; d.set(pps, o); o += pps.length;
    return d;
  }
  function codecOf(sps) {
    const p = sps[1].toString(16).padStart(2, "0");
    const c = sps[2].toString(16).padStart(2, "0");
    const l = sps[3].toString(16).padStart(2, "0");
    return "avc1." + p + c + l;
  }
  // 把 Annex-B NAL 转成 4 字节长度前缀，按 AVCC 格式喂给解码器
  function feedNal(nalType, nal) {
    if (!configured || !decoder) return;
    const out = new Uint8Array(4 + nal.length);
    out[0] = (nal.length >> 24) & 0xff; out[1] = (nal.length >> 16) & 0xff;
    out[2] = (nal.length >> 8) & 0xff; out[3] = nal.length & 0xff;
    out.set(nal, 4);
    ts += 40000; // 约 25fps 的时间戳步长（微秒）
    const type = (nalType === 5 || nalType === 7 || nalType === 8) ? "key" : "delta";
    try { decoder.decode(new EncodedVideoChunk({ type, timestamp: ts, data: out })); }
    catch (e) { console.warn("[scrcpy] decode chunk err", e); }
  }
  function onNal(nal) {
    const nalType = nal[0] & 0x1f;
    if (nalType === 7) sps = nal;
    else if (nalType === 8) pps = nal;
    if (!configured && sps && pps) {
      const desc = buildAvcC(sps, pps);
      const codec = codecOf(sps);
      decoder = new VideoDecoder({
        output: (frame) => {
          if (!videoSize.w) videoSize = { w: frame.displayWidth, h: frame.displayHeight };
          if (canvasEl.width !== frame.displayWidth || canvasEl.height !== frame.displayHeight) {
            canvasEl.width = frame.displayWidth; canvasEl.height = frame.displayHeight;
          }
          ctx.drawImage(frame, 0, 0);
          scrcpyFrameCount++;
          frame.close();
        },
        error: (e) => console.error("[scrcpy] decoder error", e),
      });
      decoder.configure({ codec, description: desc, optimizeForLatency: true });
      configured = true;
      videoSize = { w: 0, h: 0 };
    }
    feedNal(nalType, nal);
  }
  function ingest(data) {
    buf = concat(buf, new Uint8Array(data));
    let pos = 0;
    while (true) {
      const sc = findSC(buf, pos);
      if (!sc) break;
      const start = sc.end;
      const next = findSC(buf, start);
      if (!next) break;
      onNal(buf.slice(start, next.start));
      pos = next.start;
    }
    if (pos > 0) buf = buf.slice(pos);
  }
  async function start() {
    const res = await fetch("/api/stream");
    if (!res.ok || !res.body) throw new Error("视频流不可用（scrcpy 未启动？）");
    reader = res.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.byteLength) ingest(value);
      }
    } finally {
      stop();
    }
  }
  function stop() {
    try { if (reader) reader.cancel(); } catch (e) {}
    try { if (decoder && decoder.state !== "closed") decoder.close(); } catch (e) {}
    decoder = null; configured = false; sps = null; pps = null; buf = new Uint8Array(0);
  }
  return { start, stop };
}

function startScrcpyEngine() {
  engine = "scrcpy";
  canvas.style.display = "block";
  screen.style.display = "none";
  screenEmpty.style.display = "none";
  videoSize = { w: 0, h: 0 };
  scrcpyFrameCount = 0;
  scrcpyPlayer = createScrcpyPlayer(canvas, scrcpyCtx);
  scrcpyPlayer.start().catch((e) => {
    console.error("[scrcpy] start failed", e);
    $("fpsLabel").textContent = "✗ scrcpy 启动失败，已降级到 PNG 镜像";
    if (living) startLegacyEngine();
  });
  scrcpyFpsTimer = setInterval(() => {
    $("fpsLabel").textContent = `scrcpy 实时 · ${scrcpyFrameCount}fps`;
    scrcpyFrameCount = 0;
  }, 1000);
}

// ---------------- legacy 引擎（PNG 轮询，降级用） ----------------
function startLegacyEngine() {
  engine = "legacy";
  screen.style.display = "block";
  canvas.style.display = "none";
  screenEmpty.style.display = "none";
  liveLoop();
}

async function liveLoop() {
  if (!living || engine !== "legacy") return;
  const t0 = performance.now();
  try {
    const blob = await grabScreenshot();
    if (!living) return;
    showFrame(blob);
    const dt = performance.now() - t0;
    $("fpsLabel").textContent = `实时 · ${Math.min(FPS, Math.round(1000 / Math.max(dt, INTERVAL)))}fps`;
  } catch (e) {
    $("fpsLabel").textContent = "✗ " + e.message;
  }
  const elapsed = performance.now() - t0;
  liveTimer = setTimeout(liveLoop, Math.max(0, INTERVAL - elapsed));
}

// ---------------- 开关 ----------------
$("btnLive").onclick = () => {
  living = !living;
  $("btnLive").textContent = living ? "⏸ 停止镜像" : "▶ 开始镜像";
  if (living) {
    if (HAS_WC) startScrcpyEngine();
    else startLegacyEngine();
  } else {
    stopLive();
  }
};
function stopLive() {
  if (engine === "scrcpy" && scrcpyPlayer) { scrcpyPlayer.stop(); scrcpyPlayer = null; }
  if (engine === "legacy") clearTimeout(liveTimer);
  if (scrcpyFpsTimer) { clearInterval(scrcpyFpsTimer); scrcpyFpsTimer = null; }
  engine = null;
  $("fpsLabel").textContent = "";
}

function flashTap(clientX, clientY) {
  const frameRect = $("screenFrame").getBoundingClientRect();
  const mark = $("tapMark");
  mark.style.left = (clientX - frameRect.left) + "px";
  mark.style.top = (clientY - frameRect.top) + "px";
  mark.style.opacity = "1";
  setTimeout(() => { mark.style.opacity = "0"; }, 250);
}

// 按下/抬起：区分点击(tap)与拖动(swipe)。监听容器，兼容 canvas 与 img。
let down = null;
$("screenFrame").addEventListener("mousedown", (e) => {
  down = { x: e.clientX, y: e.clientY, t: performance.now() };
});
$("screenFrame").addEventListener("mouseup", async (e) => {
  if (!down) return;
  const dx = e.clientX - down.x, dy = e.clientY - down.y;
  const dist = Math.hypot(dx, dy);
  flashTap(e.clientX, e.clientY);
  try {
    if (dist < 8) {
      const p = toDeviceCoords(e.clientX, e.clientY);
      await api("/api/tap", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(p),
      });
    } else {
      const a = toDeviceCoords(down.x, down.y);
      const b = toDeviceCoords(e.clientX, e.clientY);
      const dur = Math.min(800, Math.max(120, Math.round(performance.now() - down.t)));
      await api("/api/swipe", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x1: a.x, y1: a.y, x2: b.x, y2: b.y, dur }),
      });
    }
    if (engine === "legacy") setTimeout(refreshFrameQuiet, 100);
  } catch (err) {
    console.error(err);
  }
  down = null;
});
$("screenFrame").addEventListener("dragstart", (e) => e.preventDefault());

async function sendKey(code) {
  try { await api("/api/key", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  }); } catch (e) { console.error(e); }
}
$("btnBack").onclick = () => sendKey(4);
$("btnHome").onclick = () => sendKey(3);

// ---------- 卡片折叠展开（本地缓存；实时镜像卡片不参与） ----------
const LS_COLLAPSED = "h5tool.cardCollapsed";
let collapsedState = {};
try { collapsedState = JSON.parse(localStorage.getItem(LS_COLLAPSED)) || {}; } catch (e) {}

document.querySelectorAll(".card-title").forEach((title) => {
  const card = title.closest(".card");
  if (!card || !card.id) return;
  if (collapsedState[card.id]) card.classList.add("collapsed");
  title.addEventListener("click", () => {
    const nowCollapsed = card.classList.toggle("collapsed");
    collapsedState[card.id] = nowCollapsed;
    localStorage.setItem(LS_COLLAPSED, JSON.stringify(collapsedState));
  });
});

// ---------- 初始化 ----------
$("btnRefresh").onclick = refreshStatus;
refreshStatus();
setInterval(() => { if (!living) refreshStatus(); }, 8000);

// ---------- 重启服务（launchctl kickstart） ----------
// 重启会杀掉本进程，前端先拿到 200 响应，再轮询直到服务重新起来
async function waitForServer(maxSec = 15) {
  for (let i = 0; i < maxSec; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    try {
      const res = await fetch("/api/status");
      if (res.ok) return true;
    } catch (e) { /* 服务还在重启，继续等 */ }
  }
  return false;
}

$("btnRestart").onclick = async () => {
  const btn = $("btnRestart"), msg = $("restartMsg");
  btn.disabled = true;
  msg.textContent = "重启中…";
  msg.style.color = "var(--warn)";
  try {
    await api("/api/restart", { method: "POST" });
    const ok = await waitForServer();
    if (ok) {
      msg.textContent = "✓ 已重启";
      msg.style.color = "var(--ok)";
      refreshStatus();
    } else {
      msg.textContent = "✗ 重启后未恢复，请手动检查";
      msg.style.color = "var(--err)";
    }
  } catch (e) {
    msg.textContent = "✗ " + e.message;
    msg.style.color = "var(--err)";
  } finally {
    btn.disabled = false;
    setTimeout(() => { msg.textContent = ""; }, 3000);
  }
};

// ---------- Claude 节点延迟测试 ----------
const claudeHistory = [];
function claudeRateOf(ms) {
  if (ms == null) return { cls: "err", tag: "失败" };
  if (ms < 600) return { cls: "ok", tag: "优" };
  if (ms < 1500) return { cls: "warn", tag: "一般" };
  return { cls: "err", tag: "差" };
}
function renderClaudeHist() {
  const box = $("claudeHist");
  if (!claudeHistory.length) { box.innerHTML = ""; return; }
  box.innerHTML = claudeHistory.map((h, i) =>
    `<div class="hist-item"><span class="ht">${h.ts}</span>` +
    `<span class="hv">TCP ${h.tcp} / HTTPS ${h.https} ms</span>` +
    `<span class="hd" data-i="${i}" title="删除">✕</span></div>`
  ).join("");
}
async function testClaudeLatency() {
  const btn = $("btnClaudeTest");
  btn.disabled = true;
  const rate = $("claudeRate");
  rate.className = "claude-rate run";
  rate.textContent = "测试中…";
  $("claudeProxy").textContent = "";
  $("claudeMetrics").textContent = "";
  try {
    const r = await (await fetch("/api/claude-latency")).json();
    const httpsP50 = r.https ? r.https.p50 : null;
    if (!r.ok || httpsP50 == null) {
      rate.className = "claude-rate err";
      rate.textContent = "✕ 失败";
      $("claudeProxy").textContent = (r.proxy || "") + (r.ts ? "  ·  " + r.ts : "");
      $("claudeMetrics").textContent = "无法连接到 api.anthropic.com（可能被墙或代理不通）";
      return;
    }
    const rt = claudeRateOf(httpsP50);
    rate.className = "claude-rate " + rt.cls;
    rate.textContent = httpsP50 + " ms";
    $("claudeProxy").textContent =
      "代理: " + (r.proxy || "直连") + "  ·  " + r.ts + "  ·  评级 " + rt.tag;
    const tcp = r.tcp ? `min ${r.tcp.min} / p50 ${r.tcp.p50} / max ${r.tcp.max}` : "—";
    const https = r.https
      ? `min ${r.https.min} / p50 ${r.https.p50} / p90 ${r.https.p90} / max ${r.https.max} (n=${r.https.n})`
      : "—";
    $("claudeMetrics").innerHTML =
      `<div><b>TCP 直连</b>  ${tcp} ms</div>` +
      `<div><b>HTTPS 经代理</b>  ${https} ms</div>`;
    claudeHistory.unshift({ ts: r.ts, tcp: r.tcp ? r.tcp.p50 : "—", https: httpsP50 });
    if (claudeHistory.length > 8) claudeHistory.pop();
    renderClaudeHist();
  } catch (e) {
    rate.className = "claude-rate err";
    rate.textContent = "✕ 出错";
    $("claudeMetrics").textContent = String(e);
  } finally {
    btn.disabled = false;
  }
}
$("btnClaudeTest").onclick = testClaudeLatency;
$("btnClaudeClear").onclick = () => { claudeHistory.length = 0; renderClaudeHist(); };
$("claudeHist").addEventListener("click", (e) => {
  const d = e.target.closest(".hd");
  if (d) { claudeHistory.splice(+d.dataset.i, 1); renderClaudeHist(); }
});
