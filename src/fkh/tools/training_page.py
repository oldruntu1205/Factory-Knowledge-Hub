"""培训页（业务前端第四片）：我的任务 → 读正文本 → 带引用提问。

接的是 2026-11-07 L1 条件通过的三件事：
- GET /v1/training/me/assignments（任务）
- GET /v1/training/assignments/{id}/content（正文本，签名游标分页）
- POST /v1/training/ask（带引用问答，与知识问答同一条闸门）
"""

TRAINING_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>培训 · Factory Knowledge Hub</title>
<style>
  :root { --bg:#0f1419; --panel:#161d26; --line:#243040; --text:#e6edf3; --muted:#8b9bb0;
          --ok:#3fb950; --warn:#d29922; --bad:#f85149; --accent:#4493f8; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.7 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  header { padding:16px 28px; border-bottom:1px solid var(--line); display:flex;
           align-items:center; gap:16px; flex-wrap:wrap; }
  h1 { font-size:18px; margin:0; font-weight:600; }
  nav a { color:var(--muted); text-decoration:none; font-size:14px; padding:4px 10px;
          border-radius:8px; border:1px solid transparent; }
  nav a.active, nav a:hover { color:var(--text); border-color:var(--line); }
  .tag { font-size:12px; color:var(--muted); border:1px solid var(--line);
         border-radius:999px; padding:2px 10px; }
  main { max-width:1120px; margin:0 auto; padding:24px 20px 80px;
         display:grid; grid-template-columns: 320px 1fr; gap:20px; align-items:start; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px 18px; }
  .panel h2 { font-size:14px; margin:0 0 12px; font-weight:600; color:var(--muted); }
  .task { padding:10px 12px; border-radius:9px; border:1px solid var(--line);
          margin-bottom:8px; cursor:pointer; }
  .task:hover { border-color:var(--accent); }
  .task.on { border-color:var(--accent); background:#111a24; }
  .task .t { font-weight:600; font-size:14.5px; }
  .meta { font-size:12.5px; color:var(--muted); margin-top:4px; }
  .badge { font-size:11.5px; padding:1px 8px; border-radius:999px; border:1px solid var(--line); }
  .b-PUBLISHED { color:var(--ok); border-color:var(--ok); }
  .b-NEEDS_REVIEW { color:var(--warn); border-color:var(--warn); }
  button { cursor:pointer; border-radius:8px; border:1px solid var(--line); background:transparent;
           color:var(--text); padding:7px 13px; font-size:13px; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button:disabled { opacity:.5; cursor:default; }
  .block { border-left:2px solid var(--line); padding:6px 0 6px 14px; margin-bottom:14px; }
  .block h3 { font-size:14px; margin:0 0 6px; color:var(--muted); font-weight:500; }
  .block pre { margin:0; white-space:pre-wrap; word-break:break-word;
               font:14px/1.8 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  .src { font-size:12.5px; color:var(--muted); margin:6px 0 10px; }
  .ask { display:flex; gap:10px; margin-top:6px; }
  .ask input { flex:1; padding:9px 12px; border-radius:9px; border:1px solid var(--line);
               background:#111820; color:var(--text); font-size:14px; }
  .answer { margin-top:12px; border-top:1px solid var(--line); padding-top:12px; }
  .ans-text { white-space:pre-wrap; }
  .cite { font-size:12px; color:var(--muted); margin-top:8px; }
  .empty { color:var(--muted); font-size:13.5px; }
  .toast { position:fixed; right:24px; bottom:24px; background:#111820; border:1px solid var(--line);
           border-radius:10px; padding:12px 16px; font-size:14px; opacity:0; transition:opacity .2s; }
  .toast.show { opacity:1; }
  .toast.err { border-color:var(--bad); color:var(--bad); }
</style>
</head>
<body>
<header>
  <h1>Factory Knowledge Hub</h1>
  <nav>
    <a href="/">问答</a>
    <a href="/conflicts">冲突待办</a>
    <a href="/documents">文档审核</a>
    <a href="/training" class="active">培训</a>
  </nav>
  <span class="tag">本地私域 · 127.0.0.1</span>
</header>
<main>
  <section class="panel">
    <h2>我的学习任务</h2>
    <div id="tasks"><div class="empty">加载中…</div></div>
    <div class="meta" id="taskHint"></div>
  </section>

  <section class="panel">
    <h2 id="contentTitle">课程正文本</h2>
    <div id="content"><div class="empty">左侧选择一门课程开始阅读。</div></div>
    <div class="ask" id="askBox" style="display:none">
      <input id="q" type="text" placeholder="就本课程提问，例如：保压时间是多少？" />
      <button class="primary" id="ask">提问</button>
    </div>
    <div class="answer" id="answer" style="display:none"></div>
  </section>
</main>
<div id="toast" class="toast"></div>

<script>
const state = { assignmentId: null, cursor: null, loading: false };

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}
function toast(msg, isErr) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = "toast"; }, 3000);
}

async function loadTasks() {
  const res = await fetch("/api/training/assignments");
  const body = await res.json();
  const box = document.getElementById("tasks");
  if (!res.ok) {
    box.innerHTML = '<div class="empty">加载失败：' + esc(body.error?.code || res.status) + "</div>";
    return;
  }
  const items = body.assignments || [];
  document.getElementById("taskHint").textContent =
    items.length ? "共 " + items.length + " 个任务" : "暂无任务（课程被撤下后会立即消失）";
  box.innerHTML = items.length ? items.map(a => `
    <div class="task" data-task="${esc(a.assignment_id)}">
      <div class="t">${esc(a.title)}</div>
      <div class="meta">版本 v${esc(a.course_version)} ·
        <span class="badge b-PUBLISHED">${esc(a.status)}</span></div>
    </div>`).join("") : '<div class="empty">暂无任务</div>';
}

async function openTask(assignmentId) {
  state.assignmentId = assignmentId;
  state.cursor = null;
  document.querySelectorAll(".task").forEach(el =>
    el.classList.toggle("on", el.dataset.task === assignmentId));
  document.getElementById("askBox").style.display = "flex";
  document.getElementById("answer").style.display = "none";
  document.getElementById("content").innerHTML = '<div class="empty">加载正文…</div>';
  await loadContent(true);
}

async function loadContent(first) {
  const url = "/api/training/assignments/" + encodeURIComponent(state.assignmentId) +
              "/content?limit=3" + (state.cursor ? "&cursor=" + encodeURIComponent(state.cursor) : "");
  const res = await fetch(url);
  const body = await res.json();
  const box = document.getElementById("content");
  if (!res.ok) {
    box.innerHTML = '<div class="empty">正文不可读：' + esc(body.error?.code || res.status) +
      "<br>（课程被撤下/源知识被撤销后会立即停止读取）</div>";
    return;
  }
  document.getElementById("contentTitle").textContent = body.course_title + " · 正文本";
  const html = (body.sources || []).map(src => `
    <div class="src">来源：${esc(src.title)}（v${esc(src.version_no)}，${esc(src.status)}）</div>
    ${(src.blocks || []).map(b => `
      <div class="block">
        <h3>${esc((b.heading_path || []).join(" / ") || "正文")}</h3>
        <pre>${esc(b.text.trim())}</pre>
      </div>`).join("")}`).join("");
  const next = body.next_cursor;
  const more = next ? '<button id="more">继续阅读下一页</button>' : '<div class="meta">已到本课程末尾</div>';
  box.innerHTML = (first ? "" : box.innerHTML) + html + more;
  state.cursor = next;
  const btn = document.getElementById("more");
  if (btn) btn.addEventListener("click", () => { btn.disabled = true; loadContent(false); });
}

async function ask() {
  const q = document.getElementById("q").value.trim();
  if (!q || !state.assignmentId) return;
  const btn = document.getElementById("ask");
  btn.disabled = true;
  const res = await fetch("/api/training/ask", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({assignment_id: state.assignmentId, question: q, language: "zh-CN"}),
  });
  btn.disabled = false;
  const body = await res.json();
  const box = document.getElementById("answer");
  box.style.display = "block";
  if (!res.ok) {
    box.innerHTML = '<div class="empty">提问失败：' + esc(body.error?.code || res.status) + "</div>";
    return;
  }
  const cites = (body.citations || []).map((c, i) => `[${i + 1}]`);
  box.innerHTML = `
    <div><span class="badge">${esc(body.status)}</span>
      ${body.refusal_code ? '<span class="meta">reason: ' + esc(body.refusal_code) + "</span>" : ""}</div>
    <div class="ans-text">${esc(body.answer)}</div>
    <div class="cite">引用 ${cites.length} 条 ${esc(cites.join(" "))}</div>`;
}

document.addEventListener("click", ev => {
  const task = ev.target.closest("[data-task]");
  if (task) openTask(task.dataset.task);
});
document.getElementById("ask").addEventListener("click", ask);
document.getElementById("q").addEventListener("keydown", ev => { if (ev.key === "Enter") ask(); });

loadTasks().then(() => {
  const preset = new URLSearchParams(location.search).get("assignment");
  if (preset) openTask(preset);
});
</script>
</body>
</html>
"""
