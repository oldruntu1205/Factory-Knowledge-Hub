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
  .toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }
  .toolbar input[type=search] { flex:1; min-width:220px; padding:9px 12px; border-radius:9px;
                                border:1px solid var(--line); background:var(--panel);
                                color:var(--text); font-size:14px; }
  .toolbar input[type=search]:focus { outline:none; border-color:var(--accent); }
  .toolbar select { padding:9px 10px; border-radius:9px; border:1px solid var(--line);
                    background:var(--panel); color:var(--text); font-size:14px; }
  .toolbar .toggle { padding:8px 12px; border-radius:9px; border:1px solid var(--line);
                     background:transparent; color:var(--muted); font-size:13px; cursor:pointer; }
  .toolbar .toggle.on { color:var(--text); border-color:var(--accent); }
  .diff { font-size:12px; color:var(--warn); margin-left:8px; }
  button.ghost { padding:2px 8px; font-size:12px; color:var(--muted); margin-left:8px; }
  button.ghost:hover { color:var(--accent); }
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

  <div class="toolbar">
    <input id="q" type="search" placeholder="按货号搜索，例如 A1124" autocomplete="off" />
    <select id="status">
      <option value="OPEN">待办（OPEN）</option>
      <option value="KEEP">已保留（KEEP）</option>
      <option value="DROP_ALL">已丢弃（DROP_ALL）</option>
    </select>
    <select id="sort">
      <option value="code">按货号排序</option>
      <option value="diff">按价差从大到小</option>
    </select>
    <button class="toggle" id="same-price">只看同价</button>
  </div>

  <div id="list"></div>
  <div id="empty" class="empty" style="display:none">没有待办。所有冲突都已决议。</div>
  <div class="hint">决议一次性生效、不可原地改判；错判请走新版本。KEEP 会用所选候选的行回填目录条目。</div>
</main>
<div id="toast" class="toast"></div>
<script src="/assets/evidence.js"></script>

<script>
const state = { items: [], all: [], q: "", status: "OPEN", sort: "code", samePrice: false };

function priceNum(raw) {
  const n = parseFloat(String(raw ?? "").replace(/[^\d.]/g, ""));
  return Number.isFinite(n) ? n : null;
}

function priceSpread(group) {
  const nums = (group.candidates || []).map(c => priceNum(c.raw_price)).filter(n => n !== null);
  if (nums.length < 2) return 0;
  return Math.max(...nums) - Math.min(...nums);
}

function visibleItems() {
  let items = state.items.slice();
  if (state.q) {
    const needle = state.q.trim().toUpperCase();
    items = items.filter(g => String(g.code).toUpperCase().includes(needle));
  }
  if (state.samePrice) {
    items = items.filter(g => priceSpread(g) === 0);
  }
  if (state.sort === "diff") {
    items.sort((a, b) => priceSpread(b) - priceSpread(a) || String(a.code).localeCompare(String(b.code)));
  } else {
    items.sort((a, b) => String(a.code).localeCompare(String(b.code)));
  }
  return items;
}

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
  const res = await fetch("/api/conflicts?status=" + encodeURIComponent(state.status) + "&limit=200");
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
  const shown = visibleItems();
  const totalGroups = shown.length;
  const totalCands = shown.reduce((n, g) => n + (g.candidates?.length || 0), 0);
  document.getElementById("total").textContent = totalGroups;
  document.getElementById("cands").textContent = totalCands;
  const emptyEl = document.getElementById("empty");
  emptyEl.style.display = totalGroups ? "none" : "block";
  emptyEl.textContent = state.items.length
    ? "当前筛选没有匹配的待办。"
    : "这个状态下没有待办。";

  const list = document.getElementById("list");
  list.innerHTML = shown.map(group => {
    const rows = (group.candidates || []).map(c => `
      <div class="row">
        <div class="spec">${esc(c.spec)}<button class="ghost" data-ev="${esc(c.chunk_id)}" data-price="${esc(c.raw_price)}" data-spec="${esc(c.spec)}">原文</button></div>
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
          ${priceSpread(group) > 0
            ? '<span class="diff">价差 ¥ ' + priceSpread(group).toFixed(2) + '</span>'
            : '<span class="badge">同价</span>'}
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
  if (state.status === "OPEN") {
    state.items = state.items.filter(g => g.group_id !== groupId);
  }
  render();
  toast(okText);
  return true;
}

document.addEventListener("click", async ev => {
  const raw = ev.target.closest("[data-ev]");
  if (raw) { window.fkhEvidence.open(raw.dataset.ev, raw.dataset.price, raw.dataset.spec); return; }
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

const params = new URLSearchParams(location.search);
const presetEvidence = params.get("evidence");
// 支持 /conflicts?q=A11&status=KEEP&sort=diff&same=1（演示与截图用）
if (params.get("q")) { state.q = params.get("q"); document.getElementById("q").value = state.q; }
if (params.get("status")) { state.status = params.get("status"); document.getElementById("status").value = state.status; }
if (params.get("sort")) { state.sort = params.get("sort"); document.getElementById("sort").value = state.sort; }
if (params.get("same") === "1") { state.samePrice = true; document.getElementById("same-price").classList.add("on"); }
document.getElementById("q").addEventListener("input", ev => {
  state.q = ev.target.value;
  render();
});
document.getElementById("status").addEventListener("change", ev => {
  state.status = ev.target.value;
  document.getElementById("loading").textContent = "加载中…";
  load();
});
document.getElementById("sort").addEventListener("change", ev => {
  state.sort = ev.target.value;
  render();
});
document.getElementById("same-price").addEventListener("click", ev => {
  state.samePrice = !state.samePrice;
  ev.target.classList.toggle("on", state.samePrice);
  render();
});

load().then(() => {
  if (!presetEvidence) return;
  const byId = state.items.flatMap(g => g.candidates || []).find(c => c.chunk_id === presetEvidence);
  window.fkhEvidence.open(presetEvidence, byId?.raw_price, byId?.spec);
});
</script>
</body>
</html>
"""
