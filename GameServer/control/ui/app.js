/* CODOL 控制服务管理页逻辑（M1）。
 * 约定：令牌存 localStorage；任何 401 都回到登录视图；
 * 页面只做展示与触发，状态权威永远在服务端。 */
"use strict";

const TOKEN_KEY = "codol_control_token";
// 构建标记：悬停连接徽章可确认浏览器加载的是哪版脚本（排查"改了没生效"用）。
const UI_BUILD = "2026-10-16.2";
let timer = null;

/* ---------- 基础请求 ---------- */

async function api(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers["Authorization"] = "Bearer " + token;
  const resp = await fetch("/api" + path, Object.assign({}, options, { headers }));
  if (resp.status === 401) { showLogin(); throw new Error("未认证"); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error((data.error && data.error.message) || ("HTTP " + resp.status));
  return data;
}

function showLogin() {
  localStorage.removeItem(TOKEN_KEY);
  document.getElementById("view-main").classList.add("hidden");
  document.getElementById("view-login").classList.remove("hidden");
  setBadge("未登录", "bad");
  if (timer) clearInterval(timer);
}

async function showMain() {
  document.getElementById("view-login").classList.add("hidden");
  document.getElementById("view-main").classList.remove("hidden");
  await refreshAll();
  if (timer) clearInterval(timer);
  timer = setInterval(() => {
    refreshOverview();
    if (!document.getElementById("tab-rooms").classList.contains("hidden")) refreshRooms().catch(() => {});
    if (!document.getElementById("tab-runtime").classList.contains("hidden")) refreshRuntime().catch(() => {});
  }, 5000); // 房间/运行时页展示租约与对局推进；编辑中的表单由 refreshRooms 保留。
}

/* 刷新当前默认视图（总览）；其他页签在点击时按需加载。 */
async function refreshAll() {
  await refreshOverview();
}

function setBadge(text, cls) {
  const el = document.getElementById("conn-badge");
  el.textContent = text;
  el.className = "badge " + (cls || "");
  el.title = "管理页脚本构建 " + UI_BUILD + "（与服务端版本无关；改了页面没变化请先强刷）";
}

/* ---------- 渲染工具 ---------- */

function kv(elId, pairs) {
  const el = document.getElementById(elId);
  el.innerHTML = "";
  for (const [k, v, cls] of pairs) {
    const row = document.createElement("div");
    const key = document.createElement("span"); key.className = "muted"; key.textContent = k;
    const val = document.createElement("span");
    const value = v === null || v === undefined ? "—" : String(v);
    val.textContent = value;
    if (value.length > 40) { val.title = value; val.textContent = value.slice(0, 16) + "…" + value.slice(-8); }
    if (cls) val.className = cls;
    row.append(key, val); el.append(row);
  }
}

function fmtDuration(sec) {
  sec = Math.floor(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + "时" : "") + (m ? m + "分" : "") + s + "秒";
}

/* ---------- 各页刷新 ---------- */

async function refreshOverview() {
  try {
    const data = await api("/overview");
    setBadge("已连接 · " + data.service.boot_id, "ok");
    kv("ov-service", [
      ["版本", data.service.version],
      ["启动时间", fmtDuration(data.service.uptime_seconds) + " 前"],
      ["监听", data.service.bind],
      ["本次启动标识", data.service.boot_id],
    ]);
    kv("ov-rooms", [
      ["活动房间", data.rooms.active + " / 上限 " + data.rooms.max_active],
      ["已关闭", data.rooms.total - data.rooms.active],
    ]);
    kv("ov-players", [
      ["在线玩家", data.players.connected],
      ["说明", data.players.note, "muted"],
    ]);
    kv("ov-runtime", [
      ["契约预检", data.runtime.probe_ready ? "通过" : (data.runtime.probe_never_run ? "尚未探测" : "未通过"),
        data.runtime.probe_ready ? "ok" : "warn"],
      ["探测时间", data.runtime.probe_checked_at || "—"],
      ["战斗进程", data.runtime.match_processes + "（活动对局）",
        data.runtime.match_processes ? "ok" : "muted"],
    ]);
  } catch (e) {
    if (e.message !== "未认证") setBadge("连接失败", "bad");
  }
}

async function refreshRooms() {
  const box = document.getElementById("rooms-list");
  if (box.dataset.editing === "true") return;
  const data = await api("/rooms");
  box.innerHTML = "";
  if (!data.items.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "（当前没有房间）";
    box.append(p);
  } else {
    for (const room of data.items) {
      // 玩家名来自外部输入，只走 textContent，避免原来的模板字符串产生 HTML 注入。
      const card = document.createElement("section");
      const title = document.createElement("h3");
      title.textContent = room.name + " · 版本 " + room.revision;
      card.append(title);
      const info = document.createElement("p");
      info.textContent = `${room.id} · ${room.map} / ${room.gametype} · ${room.state} · ` +
        room.members.map(p => p.display_name + (p.online ? "（在线）" : "（离线）")).join("、");
      card.append(info);
      if (room.members.length && room.members.every(p => !p.online)) {
        // 客户端崩溃不会发来"离开"，服务端按心跳超时回收（ADR 0006 遗留项）。
        const reaper = document.createElement("p");
        reaper.className = "warn";
        reaper.textContent = `全员离线：${data.reap_timeout_seconds} 秒内仍无心跳，服务端会自动关闭该房间；` +
          "也可点下方「关闭房间」立即处置。";
        card.append(reaper);
      }
      // 卡片内操作反馈：不用 alert/confirm——内嵌预览与部分宿主会静默拦截弹窗，
      // 点击后毫无反应，看起来就像"按钮坏了"。所有提示都落在这一行文本里。
      const opNote = document.createElement("p");
      opNote.className = "muted";
      const fail = (message) => { opNote.className = "error"; opNote.textContent = message; };

      const matchBar = document.createElement("p");
      if (room.match) {
        matchBar.textContent = `对局 ${room.match.id} · ${room.match.state}` +
          (room.match.entry ? ` · 入场 ${room.match.entry.host}:${room.match.entry.port}` : "");
        const stopBtn = document.createElement("button");
        stopBtn.textContent = "停止对局";
        stopBtn.addEventListener("click", async () => {
          stopBtn.disabled = true;
          try {
            await api(`/matches/${encodeURIComponent(room.match.id)}/stop`,
              {method: "POST", body: JSON.stringify({request_id: crypto.randomUUID()})});
            await refreshRooms();
          } catch (e) { fail("停止失败：" + e.message); stopBtn.disabled = false; }
        });
        matchBar.append(document.createTextNode(" "), stopBtn);
      } else if (room.state === "OPEN") {
        const startBtn = document.createElement("button");
        startBtn.className = "primary";
        startBtn.textContent = "开局（进入战斗）";
        startBtn.addEventListener("click", async () => {
          startBtn.disabled = true; startBtn.textContent = "开局请求中……";
          try {
            await api(`/rooms/${encodeURIComponent(room.id)}/match/start`,
              {method: "POST", body: JSON.stringify({request_id: crypto.randomUUID()})});
            await refreshRooms();
          } catch (e) { fail("开局失败：" + e.message); startBtn.disabled = false; startBtn.textContent = "开局（进入战斗）"; }
        });
        matchBar.append(startBtn);
      } else {
        matchBar.textContent = "房间状态：" + room.state;
      }
      card.append(matchBar);
      // 关闭房间：管理端对整个房间的处置（区别于玩家离房只移除本人席位）。
      // 二次确认在页面内完成（按钮变「确认关闭」+「取消」），不调用 confirm()。
      const closeBar = document.createElement("p");
      const closeBtn = document.createElement("button");
      closeBtn.className = "danger";
      closeBtn.textContent = "关闭房间";
      const cancelBtn = document.createElement("button");
      cancelBtn.textContent = "取消";
      cancelBtn.hidden = true;
      const closeNote = document.createElement("span");
      closeNote.className = "muted";
      closeNote.textContent = " 管理员处置：清空全部成员并关闭房间（对局中会先停局）。";
      let armed = false;
      const disarm = () => {
        armed = false; closeBtn.textContent = "关闭房间"; cancelBtn.hidden = true;
        closeNote.className = "muted";
        closeNote.textContent = " 管理员处置：清空全部成员并关闭房间（对局中会先停局）。";
      };
      closeBtn.addEventListener("click", async () => {
        const inMatch = !!room.match || room.state === "IN_MATCH" || room.state === "STARTING";
        if (!armed) {
          armed = true;
          closeBtn.textContent = "确认关闭";
          cancelBtn.hidden = false;
          closeNote.className = "warn";
          closeNote.textContent = ` 将移出全部 ${room.members.length} 名成员` +
            (inMatch ? "，并先停止对局让客户端返回大厅" : "") + "。再点一次「确认关闭」生效。";
          return;
        }
        closeBtn.disabled = true; cancelBtn.hidden = true;
        closeNote.className = "muted"; closeNote.textContent = " 正在关闭……";
        try {
          await api(`/rooms/${encodeURIComponent(room.id)}/close`,
            {method: "POST", body: JSON.stringify({request_id: crypto.randomUUID(), reason: "管理页关闭房间"})});
          delete box.dataset.editing; // 关闭后强制重绘，不再保留未提交的 Bot 输入。
          await refreshRooms();
        } catch (e) {
          fail("关闭失败：" + e.message);
          disarm(); closeBtn.disabled = false;
        }
      });
      cancelBtn.addEventListener("click", disarm);
      closeBar.append(closeBtn, document.createTextNode(" "), cancelBtn, closeNote);
      card.append(closeBar, opNote);
      const form = document.createElement("form");
      const count = document.createElement("input");
      count.type = "number"; count.min = "0"; count.max = "11"; count.value = room.bots.count;
      count.setAttribute("aria-label", "Bot 数量");
      const team = document.createElement("select");
      for (const [v, text] of [[1, "特遣队"], [2, "暗影军团"]]) {
        const opt = document.createElement("option"); opt.value = v; opt.textContent = text; team.append(opt);
      }
      team.value = room.bots.team;
      const difficulty = document.createElement("select");
      for (const [v, text] of [["easy", "简单"], ["normal", "普通"], ["hard", "困难"]]) {
        const opt = document.createElement("option"); opt.value = v; opt.textContent = text; difficulty.append(opt);
      }
      difficulty.value = room.bots.difficulty;
      const submit = document.createElement("button"); submit.textContent = "同步 Bot 配置";
      const msg = document.createElement("p"); msg.textContent = "仅配置房间席位，尚未生成可交战 Bot。";
      form.append("Bot 数量 ", count, " 阵营 ", team, " 难度 ", difficulty, submit, msg);
      // 有编辑时暂缓自动重绘，避免五秒刷新吃掉尚未提交的输入。
      form.addEventListener("input", () => { box.dataset.editing = "true"; });
      form.addEventListener("submit", async ev => {
        ev.preventDefault(); submit.disabled = true;
        const body = {request_id: crypto.randomUUID(), expected_room_revision: room.revision,
          count: Number(count.value), team: Number(team.value), difficulty: difficulty.value};
        try {
          await api(`/rooms/${encodeURIComponent(room.id)}/bots`, {method: "POST", body: JSON.stringify(body)});
          delete box.dataset.editing; await refreshRooms();
        } catch (e) { msg.textContent = e.message + "；切换页签刷新后可重试。"; submit.disabled = false; }
      });
      card.append(form); box.append(card);
    }
  }
  document.getElementById("rooms-note").textContent = data.note;
}

function renderProbe(probe, portFree) {
  const pairs = [
    ["整体结论", probe.ready ? "可用" : "不可用", probe.ready ? "ok" : "warn"],
    ["契约版本", "v" + probe.contract_version + (probe.fake ? "（假探测器）" : "")],
    ["探测时间", probe.checked_at],
    ["原生 EXE", probe.exe_found ? "已找到" : "缺失", probe.exe_found ? "" : "warn"],
    ["网络审计一致", probe.audit_passed ? "通过" : "不一致", probe.audit_passed ? "ok" : "warn"],
    ["版本镜像", probe.mirror_ok ? "哈希一致" : "不符", probe.mirror_ok ? "ok" : "warn"],
    ["资产校验", probe.assets_verified + " / " + probe.assets_total,
      probe.assets_verified === probe.assets_total ? "ok" : "warn"],
    ["控制端口(UDP " + (probe.supported_content.length && 28969) + ")", portFree === null ? "未测" : (portFree ? "空闲" : "被占用"),
      portFree ? "ok" : "warn"],
  ];
  for (const c of probe.supported_content) pairs.push(["已验证组合", c.map + " · " + c.gametype]);
  kv("rt-probe", pairs);
  const problems = document.createElement("div");
  problems.className = "muted";
  problems.innerHTML = probe.problems.map(p => "⚠ " + p).join("<br>") || "";
  document.getElementById("rt-probe").append(problems);
}

async function refreshRuntime() {
  const data = await api("/runtime");
  renderMatches(data.matches || {active: [], recent: [], match_processes: 0});
  if (data.probe) renderProbe(data.probe, data.port_free);
  else document.getElementById("rt-probe").innerHTML =
    '<p class="empty">尚未探测。点击下方按钮执行首次深度探测。</p>';
}

/* M3 对局面板：活动对局操作 + 最近对局状态。 */
function renderMatches(status) {
  const box = document.getElementById("rt-matches");
  box.innerHTML = "";
  if (!status.active.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "（当前没有活动对局）";
    box.append(p);
  } else {
    for (const m of status.active) {
      const row = document.createElement("p");
      const text = document.createElement("span");
      text.textContent = `${m.id} · ${m.map}/${m.gametype} · ${m.state} · 房间 ${m.room_id}` +
        (m.error ? ` · ${m.error}` : "");
      row.append(text);
      const stop = document.createElement("button");
      stop.textContent = "停止";
      stop.addEventListener("click", () => terminateMatch(m.id, "stop"));
      const cancel = document.createElement("button");
      cancel.textContent = "取消";
      cancel.addEventListener("click", () => terminateMatch(m.id, "cancel"));
      row.append(document.createTextNode(" "), stop, cancel);
      box.append(row);
    }
  }
  if (status.recent && status.recent.length) {
    const table = document.createElement("table");
    table.innerHTML = "<tr><th>对局</th><th>状态</th><th>地图</th><th>结束时间</th><th>说明</th></tr>" +
      status.recent.map(m => `<tr><td>${m.id}</td><td>${m.state}</td><td>${m.map || "—"}</td>` +
        `<td>${m.finished_at || "—"}</td><td class="muted">${m.error || ""}</td></tr>`).join("");
    box.append(table);
  }
}

async function terminateMatch(matchId, action) {
  try {
    await api(`/matches/${encodeURIComponent(matchId)}/${action}`,
      {method: "POST", body: JSON.stringify({request_id: crypto.randomUUID()})});
    await refreshRuntime();
  } catch (e) { alert("操作失败：" + e.message); }
}

async function refreshDiagnostics() {
  const [runs, logs] = await Promise.all([api("/diagnostics/server-runs"), api("/diagnostics/logs?limit=120")]);
  const runBox = document.getElementById("dg-runs");
  runBox.innerHTML = "";
  if (!runs.runs.length) runBox.innerHTML = '<p class="empty">（暂无原生运行记录）</p>';
  else {
    const table = document.createElement("table");
    table.innerHTML = "<tr><th>运行</th><th>结果</th><th>地图</th><th>耗时</th><th>错误</th></tr>" +
      runs.runs.map(r => `<tr><td>${r.id}</td>` +
        `<td class="${r.passed === true ? "ok" : (r.passed === false ? "error" : "muted")}">` +
        `${r.has_summary ? (r.passed === true ? "通过" : r.passed === false ? "失败" : "未知") : "无摘要"}</td>` +
        `<td>${r.map || "—"}</td><td>${r.seconds ?? "—"}s</td>` +
        `<td class="error">${(r.errors || []).join("; ") || ""}</td></tr>`).join("");
    runBox.append(table);
  }
  const logBox = document.getElementById("dg-logs");
  logBox.textContent = logs.entries
    .map(e => [e.ts, e.level, e.event, e.detail ? JSON.stringify(e.detail) : ""].join(" "))
    .join("\n") || "（暂无日志）";
}

/* ---------- 事件绑定 ---------- */

document.getElementById("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const err = document.getElementById("login-error");
  err.textContent = "";
  try {
    const resp = await fetch("/api/admin/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("login-user").value,
        password: document.getElementById("login-pass").value,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error ? data.error.message : "登录失败");
    localStorage.setItem(TOKEN_KEY, data.token);
    try {
      await showMain();
    } catch (e) {
      // 主视图装配失败也要可见地报错，不能停在“未登录”状态。
      setBadge("加载失败：" + e.message, "bad");
    }
  } catch (e) { err.textContent = e.message; }
});

document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", async () => {
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b === btn));
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.add("hidden"));
  const panel = document.getElementById("tab-" + btn.dataset.tab);
  panel.classList.remove("hidden");
  if (btn.dataset.tab === "overview") await refreshOverview();
  if (btn.dataset.tab === "rooms") {
    delete document.getElementById("rooms-list").dataset.editing;
    await refreshRooms().catch(() => {});
  }
  if (btn.dataset.tab === "runtime") await refreshRuntime().catch(() => {});
  if (btn.dataset.tab === "diagnostics") await refreshDiagnostics().catch(() => {});
}));

document.getElementById("rt-reprobe").addEventListener("click", async (ev) => {
  const btn = ev.target;
  btn.disabled = true; btn.textContent = "探测中……（资产全量哈希）";
  try {
    const data = await api("/runtime/probe", { method: "POST" });
    renderProbe(data.probe, data.port_free);
  } catch (e) { alert("探测失败：" + e.message); }
  btn.disabled = false; btn.textContent = "重新深度探测（全量哈希，耗时数十秒）";
});

document.getElementById("pwd-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const msg = document.getElementById("pwd-msg");
  msg.textContent = ""; msg.className = "";
  try {
    await api("/admin/password", {
      method: "POST",
      body: JSON.stringify({
        old_password: document.getElementById("pwd-old").value,
        new_password: document.getElementById("pwd-new").value,
      }),
    });
    msg.className = "ok"; msg.textContent = "密码已修改，请重新登录。";
    setTimeout(showLogin, 1200);
  } catch (e) { msg.className = "error"; msg.textContent = e.message; }
});

document.getElementById("shutdown-btn").addEventListener("click", async (ev) => {
  if (!confirm("确定停止控制服务？正在进行的操作将被优雅结束。")) return;
  const msg = document.getElementById("shutdown-msg");
  try {
    await api("/admin/shutdown", { method: "POST" });
    msg.className = "ok"; msg.textContent = "停服请求已受理，服务正在收尾……";
    setBadge("已停服", "bad");
    if (timer) clearInterval(timer);
  } catch (e) { msg.className = "error"; msg.textContent = e.message; }
});

/* ---------- 启动 ---------- */

(async function init() {
  // 已有令牌则直接进入主视图（verify 经由 overview 的 401 处理）。
  if (localStorage.getItem(TOKEN_KEY)) { await showMain().catch(showLogin); }
  else showLogin();
})();
