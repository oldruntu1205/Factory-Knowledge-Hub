"""冲突待办页（业务前端第一片）。

刻意与演示问答页分文件：问答页是"给人看效果"，这一页是"给人干活"——
每个候选旁边就是 KEEP，组底部是 DROP_ALL，动作成功后卡片消失、计数减一。
"""

CONFLICTS_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>目录冲突待办 · Factory Knowledge Hub</title>
<style>
  :root {
    --bg:#0f1419; --panel:#161d26; --line:#243040; --text:#e6edf3; --muted:#8b9bb0;
    --ok:#3fb950; --warn:#d29922; --bad:#f85149; --accent:#4493f8;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  header { padding:16px 28px; border-bottom:1px solid var(--line); display:flex;
           align-items:center; gap:16px; flex-wrap:wrap; }
  h1 { font-size:18px; margin:0; font-weight:600; }
  nav a { color:var(--muted); text-decoration:none; font-size:14px; padding:4px 10px;
          border-radius:8px; border:1px solid transparent; }
  nav a.active, nav a:hover { color:var(--text); border-color:var(--line); }
  .tag { font-size:12px; color:var(--muted); border:1px solid var(--line);
         border-radius:999px; padding:2px 10px; }
  main { max-width:1080px; margin:0 auto; padding:24px 20px 80px; }
  .bar { display:flex; gap:16px; align-items:center; flex-wrap:wrap; margin-bottom:18px;
         font-size:14px; color:var(--muted); }
  .bar b { color:var(--text); }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
          padding:16px 18px; margin-bottom:14px; }
  .card-head { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:10px; }
  .code { font-size:17px; font-weight:600; letter-spacing:.02em; }
  .badge { font-size:12px; padding:2px 10px; border-radius:999px; border:1px solid var(--line);
           color:var(--muted); }
  .row { display:grid; grid-template-columns:1fr 110px 120px 96px; gap:10px;
         align-items:center; padding:8px 10px; border-radius:8px; }
  .row + .row { border-top:1px solid rgba(36,48,64,.7); }
  .row:hover { background:#111820; }
  .spec { font-size:14px; }
  .price { font-size:15px; font-weight:600; color:var(--ok); }
  .loc { font-size:12px; color:var(--muted); }
  button { cursor:pointer; border-radius:8px; border:1px solid var(--line);
           background:transparent; color:var(--text); padding:6px 12px; font-size:13px; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.danger:hover { border-color:var(--bad); color:var(--bad); }
  button:disabled { opacity:.5; cursor:default; }
  .actions { margin-top:10px; display:flex; gap:10px; justify-content:flex-end; }
  .empty { color:var(--muted); text-align:center; padding:60px 0; font-size:15px; }
  .toast { position:fixed; right:24px; bottom:24px; background:#111820; border:1px solid var(--line);
           border-radius:10px; padding:12px 16px; font-size:14px; opacity:0; transition:opacity .2s; }
  .toast.show { opacity:1; }
  .toast.err { border-color:var(--bad); color:var(--bad); }
  .hint { font-size:12.5px; color:var(--muted); margin-top:6px; }
</style>
</head>
<body>
<header>
  <h1>Factory Knowledge Hub</h1>
  <nav>
    <a href="/">问答</a>
    <a href="/conflicts" class="active">冲突待办</a>
  </nav>
  <span class="tag">本地私域 · 127.0.0.1</span>
</header>
<main>
  <div class="bar">
    <span>待办组 <b id="total">—</b></span>
    <span>候选行 <b id="cands">—</b></span>
    <span id="loading">加载中…</span>
  </div>

  <div id="list"></div>
  <div id="empty" class="empty" style="display:none">没有待办。所有冲突都已决议。</div>
  <div class="hint">决议一次性生效、不可原地改判；错判请走新版本。KEEP 会用所选候选的行回填目录条目。</div>
</main>
<div id="toast" class="toast"></div>

<script>
const state = { items: [] };

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}
function locText(loc) {
  if (!loc) return "—";
  if (loc.page_start) return "第 " + loc.page_start + " 页";
  if (loc.block_index !== undefined) return "块 " + (Number(loc.block_index) + 1);
  return "—";
}
function toast(msg, isErr) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = "toast"; }, 2600);
}

async function load() {
  const res = await fetch("/api/conflicts?status=OPEN&limit=50");
  const body = await res.json();
  if (!res.ok) {
    document.getElementById("loading").textContent = "加载失败：" + (body.error?.code || res.status);
    return;
  }
  state.items = body.items || [];
  document.getElementById("loading").textContent = "";
  render();
}

function render() {
  const totalGroups = state.items.length;
  const totalCands = state.items.reduce((n, g) => n + (g.candidates?.length || 0), 0);
  document.getElementById("total").textContent = totalGroups;
  document.getElementById("cands").textContent = totalCands;
  document.getElementById("empty").style.display = totalGroups ? "none" : "block";

  const list = document.getElementById("list");
  list.innerHTML = state.items.map(group => {
    const rows = (group.candidates || []).map(c => `
      <div class="row">
        <div class="spec">${esc(c.spec)}</div>
        <div class="price">¥ ${esc(c.raw_price)}</div>
        <div class="loc">${esc(locText(c.locator))}</div>
        <div style="text-align:right">
          <button class="primary" data-keep="${esc(group.group_id)}|${esc(c.candidate_id)}">选这个</button>
        </div>
      </div>`).join("");
    return `
      <div class="card" data-group="${esc(group.group_id)}">
        <div class="card-head">
          <span class="code">${esc(group.code)}</span>
          <span class="badge">${esc(group.status)}</span>
          <span class="badge">版本 ${esc(String(group.version_id).slice(0, 8))}</span>
        </div>
        ${rows}
        <div class="actions">
          <button class="danger" data-drop="${esc(group.group_id)}">全部丢弃（该货号不设条目）</button>
        </div>
      </div>`;
  }).join("");
}

async function resolve(groupId, payload, okText) {
  const res = await fetch("/api/conflicts/" + encodeURIComponent(groupId) + "/resolve", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    toast("决议失败：" + (body.error?.code || res.status), true);
    return false;
  }
  state.items = state.items.filter(g => g.group_id !== groupId);
  render();
  toast(okText);
  return true;
}

document.addEventListener("click", async ev => {
  const keep = ev.target.closest("[data-keep]");
  if (keep) {
    const [groupId, candidateId] = keep.dataset.keep.split("|");
    keep.disabled = true;
    await resolve(groupId, {action: "KEEP", candidate_id: candidateId}, "已保留该候选，其余候选作废");
    return;
  }
  const drop = ev.target.closest("[data-drop]");
  if (drop) {
    const groupId = drop.dataset.drop;
    if (!confirm("确认该货号不设条目？此操作不可原地改判。")) return;
    drop.disabled = true;
    await resolve(groupId, {action: "DROP_ALL"}, "已标记为该货号不设条目");
  }
});

load();
</script>
</body>
</html>
"""
