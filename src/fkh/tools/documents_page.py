"""文档审核页（业务前端第二片）。

业务人员唯一的入口：传文档 → 看状态 → 通过/驳回 → 发布。
接的是 2026-11-07 L1 裁决后新增的两个接口：
- POST /v1/knowledge/documents（入库固定 DRAFT）
- GET  /v1/knowledge/documents（先取最新版再过滤）
以及既有的 review / publish。
"""

DOCUMENTS_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>文档审核 · Factory Knowledge Hub</title>
<style>
  :root { --bg:#0f1419; --panel:#161d26; --line:#243040; --text:#e6edf3; --muted:#8b9bb0;
          --ok:#3fb950; --warn:#d29922; --bad:#f85149; --accent:#4493f8; }
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
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px;
           padding:16px 18px; margin-bottom:18px; }
  .panel h2 { font-size:15px; margin:0 0 12px; font-weight:600; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:10px; }
  label { display:block; font-size:12.5px; color:var(--muted); margin-bottom:4px; }
  input[type=text], input[type=file], select {
    width:100%; padding:8px 10px; border-radius:8px; border:1px solid var(--line);
    background:#111820; color:var(--text); font-size:14px; }
  input:focus, select:focus { outline:none; border-color:var(--accent); }
  button { cursor:pointer; border-radius:8px; border:1px solid var(--line);
           background:transparent; color:var(--text); padding:7px 13px; font-size:13px; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.ok:hover { border-color:var(--ok); color:var(--ok); }
  button.bad:hover { border-color:var(--bad); color:var(--bad); }
  button:disabled { opacity:.5; cursor:default; }
  .row-actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }
  .bar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:14px;
         font-size:14px; color:var(--muted); }
  .bar b { color:var(--text); }
  .doc { background:var(--panel); border:1px solid var(--line); border-radius:12px;
         padding:14px 16px; margin-bottom:12px; }
  .doc-head { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .doc-title { font-weight:600; font-size:15.5px; }
  .meta { font-size:12.5px; color:var(--muted); margin-top:6px; }
  .meta code { color:var(--text); font-size:12px; }
  .badge { font-size:12px; padding:2px 9px; border-radius:999px; border:1px solid var(--line); }
  .b-DRAFT { color:var(--warn); border-color:var(--warn); }
  .b-REVIEWED { color:var(--accent); border-color:var(--accent); }
  .b-APPROVED_INTERNAL, .b-PUBLISHED_PUBLIC { color:var(--ok); border-color:var(--ok); }
  .b-REVOKED, .b-DISCARDED, .b-SUPERSEDED { color:var(--bad); border-color:var(--bad); }
  .empty { color:var(--muted); text-align:center; padding:40px 0; }
  .toast { position:fixed; right:24px; bottom:24px; background:#111820; border:1px solid var(--line);
           border-radius:10px; padding:12px 16px; font-size:14px; opacity:0; transition:opacity .2s; }
  .toast.show { opacity:1; }
  .toast.err { border-color:var(--bad); color:var(--bad); }
  .hint { font-size:12.5px; color:var(--muted); margin-top:8px; }
  .steps { font-size:12.5px; color:var(--muted); }
  .steps code { color:var(--text); }
</style>
</head>
<body>
<header>
  <h1>Factory Knowledge Hub</h1>
  <nav>
    <a href="/">问答</a>
    <a href="/conflicts">冲突待办</a>
    <a href="/documents" class="active">文档审核</a>
  </nav>
  <span class="tag">本地私域 · 127.0.0.1</span>
</header>
<main>
  <div class="panel">
    <h2>上传文档</h2>
    <div class="grid">
      <div>
        <label for="file">文件（文本，≤5 MiB）</label>
        <input id="file" type="file" accept=".md,.txt,.csv,.json" />
      </div>
      <div>
        <label for="title">标题</label>
        <input id="title" type="text" placeholder="例：2023 产品目录" />
      </div>
      <div>
        <label for="domain">业务域</label>
        <select id="domain">
          <option value="product_catalog">产品目录（会抽目录条目）</option>
          <option value="quality">质量</option>
          <option value="production_process">生产工艺</option>
          <option value="glass_materials">玻璃材料</option>
          <option value="owner_experience">业主经验</option>
          <option value="equipment_safety">设备安全</option>
          <option value="foreign_trade">外贸</option>
          <option value="training">培训</option>
        </select>
      </div>
      <div>
        <label for="dept">归属部门</label>
        <input id="dept" type="text" value="sales" />
      </div>
      <div>
        <label for="scope">数据范围（required_scope）</label>
        <select id="scope">
          <option value="internal_product">internal_product（产品）</option>
          <option value="internal_training">internal_training（培训）</option>
          <option value="customer_public">customer_public（对外）</option>
        </select>
      </div>
      <div>
        <label for="audience">受众</label>
        <select id="audience">
          <option value="internal">内部</option>
          <option value="customer_public">客户可见</option>
        </select>
      </div>
      <div>
        <label for="key">knowledge_key（同租户唯一）</label>
        <input id="key" type="text" placeholder="留空按文件名生成" />
      </div>
      <div>
        <label for="classification">分级（对外发布仅 C0）</label>
        <select id="classification">
          <option value="0">C0（可对外）</option>
          <option value="1">C1</option>
          <option value="2" selected>C2（内部）</option>
          <option value="3">C3</option>
          <option value="4">C4</option>
        </select>
      </div>
      <div>
        <label for="currency">目录货币（仅 product_catalog）</label>
        <select id="currency">
          <option value="CNY">CNY</option>
          <option value="">不给（不抽目录）</option>
        </select>
      </div>
    </div>
    <div class="row-actions">
      <button class="primary" id="upload">上传并入库</button>
      <span class="hint">入库后状态是 DRAFT：不可被检索，必须通过审核并发布才生效。</span>
    </div>
  </div>

  <div class="bar">
    <span>文档 <b id="total">—</b></span>
    <select id="status" style="min-width:290px">
      <option value="DRAFT">待审（DRAFT）</option>
      <option value="REVIEWED">已审（REVIEWED）</option>
      <option value="APPROVED_INTERNAL">内部可用（APPROVED_INTERNAL）</option>
      <option value="PUBLISHED_PUBLIC">已对外（PUBLISHED_PUBLIC）</option>
      <option value="">全部状态</option>
    </select>
    <button id="refresh">刷新</button>
  </div>

  <div id="list"></div>
  <div id="empty" class="empty" style="display:none">这个状态下没有文档。</div>

  <div class="hint steps">
    审批流转：<code>DRAFT</code> --通过审核--> <code>REVIEWED</code> --发布（内部）--> <code>APPROVED_INTERNAL</code>（可被检索）。
    驳回会进入 <code>DISCARDED</code>，不可原地复用。
  </div>
</main>
<div id="toast" class="toast"></div>

<script>
const state = { items: [], status: "DRAFT" };

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

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, body };
}

async function load() {
  const q = state.status ? "?status=" + encodeURIComponent(state.status) + "&limit=100" : "?limit=100";
  const { ok, body } = await api("/api/documents" + q);
  if (!ok) {
    document.getElementById("total").textContent = "加载失败：" + (body.error?.code || "?");
    return;
  }
  state.items = body.items || [];
  render();
}

function render() {
  document.getElementById("total").textContent = state.items.length;
  document.getElementById("empty").style.display = state.items.length ? "none" : "block";
  document.getElementById("list").innerHTML = state.items.map(d => {
    const actions = [];
    if (d.status === "DRAFT") {
      actions.push(`<button class="ok" data-pass="${esc(d.version_id)}">通过审核</button>`);
      actions.push(`<button class="bad" data-reject="${esc(d.version_id)}">驳回</button>`);
    }
    if (d.status === "REVIEWED") {
      actions.push(`<button class="primary" data-publish="${esc(d.version_id)}">发布（内部可用）</button>`);
    }
    if (d.status === "APPROVED_INTERNAL" || d.status === "PUBLISHED_PUBLIC") {
      actions.push(`<button class="bad" data-revoke="${esc(d.version_id)}">撤下（立即失效）</button>`);
    }
    if (d.status === "DRAFT" || d.status === "REVIEWED") {
      actions.push(`<button class="bad" data-discard="${esc(d.version_id)}">作废</button>`);
    }
    if (d.status === "APPROVED_INTERNAL" && d.audience === "customer_public") {
      actions.push(
        `<button class="ok" data-publish-public="${esc(d.version_id)}">发布为对外（客户可见）</button>`
      );
    }
    return `
      <div class="doc">
        <div class="doc-head">
          <span class="doc-title">${esc(d.title)}</span>
          <span class="badge b-${esc(d.status)}">${esc(d.status)}</span>
          <span class="badge">v${esc(d.version_no)}</span>
          <span class="badge">${esc(d.domain)}</span>
        </div>
        <div class="meta">
          key <code>${esc(d.knowledge_key)}</code> · 部门 <code>${esc(d.owner_department)}</code>
          · 范围 <code>${esc(d.required_scope)}</code> · 受众 <code>${esc(d.audience)}</code>
          · 分级 <code>${esc(d.classification)}</code> · 切片 <code>${esc(d.chunk_count)}</code>
        </div>
        <div class="row-actions">${actions.join("")}</div>
      </div>`;
  }).join("");
}

async function act(versionId, kind) {
  let path, payload, okText;
  if (kind === "pass") {
    path = "/api/documents/" + encodeURIComponent(versionId) + "/review";
    payload = { decision: "PASS" };
    okText = "已通过审核，下一步可发布";
  } else if (kind === "reject") {
    const reason = prompt("驳回原因（会进审计）：", "格式不符合目录规范");
    if (reason === null) return;
    path = "/api/documents/" + encodeURIComponent(versionId) + "/review";
    payload = { decision: "REJECT", reason: reason || undefined };
    okText = "已驳回";
  } else if (kind === "revoke") {
    const reason = prompt("撤下原因（会进审计）：", "内容已过时，需重新出版本");
    if (reason === null) return;
    path = "/api/documents/" + encodeURIComponent(versionId) + "/revoke?reason=" +
           encodeURIComponent(reason || "未填写原因");
    payload = null;
    okText = "已撤下：立即从检索与客户视角移除";
  } else if (kind === "discard") {
    const reason = prompt("作废原因（会进审计）：", "上传内容有误");
    if (reason === null) return;
    path = "/api/documents/" + encodeURIComponent(versionId) + "/discard?reason=" +
           encodeURIComponent(reason || "未填写原因");
    payload = null;
    okText = "已作废";
  } else if (kind === "publish_public") {
    if (!confirm("对外发布后，客户视角可以检索到这份内容。确认？")) return;
    path = "/api/documents/" + encodeURIComponent(versionId) + "/publish";
    payload = { audience: "customer_public" };
    okText = "已对外发布（四眼原则：由对外发布审核员执行）";
  } else {
    path = "/api/documents/" + encodeURIComponent(versionId) + "/publish";
    payload = { audience: "internal" };
    okText = "已发布为内部可用（现在可被检索）";
  }
  const { ok, body } = await api(path, payload === null
    ? { method: "POST" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!ok) {
    toast("操作失败：" + (body.error?.code || "?"), true);
    return;
  }
  toast(okText);
  load();
}

async function upload() {
  const fileInput = document.getElementById("file");
  const file = fileInput.files?.[0];
  if (!file) { toast("先选一个文件", true); return; }
  if (file.size > 5 * 1024 * 1024) { toast("文件超过 5 MiB 上限", true); return; }

  const title = document.getElementById("title").value.trim() || file.name;
  const key = document.getElementById("key").value.trim() ||
              (file.name.replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9\-]+/g, "-") + "-" + Date.now().toString(36).slice(-4));
  const domain = document.getElementById("domain").value;
  const buf = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < buf.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  }
  const payload = {
    filename: file.name,
    title: title,
    domain: domain,
    owner_department: document.getElementById("dept").value.trim() || "sales",
    classification: parseInt(document.getElementById("classification").value || "2", 10),
    audience: document.getElementById("audience").value,
    required_scope: document.getElementById("scope").value,
    acl_allow_roles: ["staff"],
    acl_deny_roles: [],
    knowledge_key: key,
    content_base64: btoa(binary),
  };
  const currency = document.getElementById("currency").value;
  if (domain === "product_catalog" && currency) payload.catalog_currency = currency;

  const btn = document.getElementById("upload");
  btn.disabled = true;
  const { ok, body } = await api("/api/documents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  btn.disabled = false;
  if (!ok) {
    toast("入库失败：" + (body.error?.code || "?"), true);
    return;
  }
  toast("已入库：" + body.status + "，切片 " + body.child_chunks);
  document.getElementById("status").value = "DRAFT";
  state.status = "DRAFT";
  load();
}

document.getElementById("upload").addEventListener("click", upload);
document.getElementById("refresh").addEventListener("click", load);
document.getElementById("status").addEventListener("change", ev => {
  state.status = ev.target.value;
  load();
});

document.addEventListener("click", ev => {
  const pass = ev.target.closest("[data-pass]");
  if (pass) { act(pass.dataset.pass, "pass"); return; }
  const reject = ev.target.closest("[data-reject]");
  if (reject) { act(reject.dataset.reject, "reject"); return; }
  const revoke = ev.target.closest("[data-revoke]");
  if (revoke) { act(revoke.dataset.revoke, "revoke"); return; }
  const discard = ev.target.closest("[data-discard]");
  if (discard) { act(discard.dataset.discard, "discard"); return; }
  const publishPublic = ev.target.closest("[data-publish-public]");
  if (publishPublic) { act(publishPublic.dataset.publishPublic, "publish_public"); return; }
  const publish = ev.target.closest("[data-publish]");
  if (publish) { act(publish.dataset.publish, "publish"); }
});

const params = new URLSearchParams(location.search);
if (params.get("status") !== null) {
  state.status = params.get("status");
  document.getElementById("status").value = state.status;
}

load();
</script>
</body>
</html>
"""
