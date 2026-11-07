"""经验采集演示页（第五片业务前端）：授权 → 访谈 → 转写 → 草稿 → 人工审核。

演示口径（写在页面上，不隐瞒）：
- 授权只允许本人：页面用当前演示身份作为受访者（subject = 令牌 sub）；
- 同一个演示身份既采集又审核，**不代表生产允许自审**（生产需要 `interview.review` 的独立审核人）；
- 转写是人工粘贴的文本（ADR-05 未签，不接真实 ASR）。
"""

EXPERIENCE_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>经验采集 · Factory Knowledge Hub</title>
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
  main { max-width:1120px; margin:0 auto; padding:24px 20px 80px; display:grid;
         grid-template-columns: 380px 1fr; gap:20px; align-items:start; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px 18px; }
  .panel h2 { font-size:14px; margin:0 0 12px; font-weight:600; color:var(--muted); }
  label { display:block; font-size:12.5px; color:var(--muted); margin:10px 0 4px; }
  input, textarea, select { width:100%; padding:9px 11px; border-radius:9px;
    border:1px solid var(--line); background:#111820; color:var(--text); font-size:14px; }
  textarea { min-height:150px; font-family:inherit; resize:vertical; }
  button { cursor:pointer; border-radius:8px; border:1px solid var(--line); background:transparent;
           color:var(--text); padding:7px 13px; font-size:13px; margin-top:10px; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button:disabled { opacity:.5; cursor:default; }
  .block { border-left:2px solid var(--line); padding:6px 0 6px 14px; margin-bottom:14px; }
  .block h3 { font-size:13px; margin:0 0 6px; color:var(--muted); font-weight:500; }
  .block pre { margin:0; white-space:pre-wrap; word-break:break-word; font:14px/1.8 inherit; }
  .badge { font-size:11.5px; padding:1px 8px; border-radius:999px; border:1px solid var(--line); }
  .b-DRAFT { color:var(--muted); } .b-APPROVED { color:var(--ok); border-color:var(--ok); }
  .b-REJECTED, .b-WITHDRAWN { color:var(--bad); border-color:var(--bad); }
  .b-DRAFT_READY, .b-TRANSCRIBED { color:var(--warn); border-color:var(--warn); }
  .meta { font-size:12.5px; color:var(--muted); margin-top:6px; }
  .toast { position:fixed; right:24px; bottom:24px; background:#111820; border:1px solid var(--line);
           border-radius:10px; padding:12px 16px; font-size:14px; opacity:0; transition:opacity .2s; }
  .toast.show { opacity:1; } .toast.err { border-color:var(--bad); color:var(--bad); }
  .hint { font-size:12px; color:var(--muted); border-top:1px dashed var(--line);
          margin-top:14px; padding-top:10px; }
</style>
</head>
<body>
<header>
  <h1>Factory Knowledge Hub</h1>
  <nav>
    <a href="/">问答</a>
    <a href="/conflicts">冲突待办</a>
    <a href="/documents">文档审核</a>
    <a href="/training">培训</a>
    <a href="/experience" class="active">经验采集</a>
  </nav>
  <span class="tag">本地私域 · 127.0.0.1</span>
</header>
<main>
  <section class="panel">
    <h2>① 授权与访谈</h2>
    <label>受访者（演示身份）</label>
    <input id="subject" readonly />
    <label>授权形式</label>
    <select id="method">
      <option value="ORAL_WITH_RECEIPT">口头告知 + 电子回执</option>
      <option value="WRITTEN">书面授权</option>
    </select>
    <label>保留期（天）</label>
    <input id="retention" type="number" value="90" min="1" max="3650" />
    <button class="primary" id="grantBtn">登记授权</button>
    <div class="meta" id="consentState">尚未登记授权</div>

    <label>转写文本（人工粘贴；支持 [mm:ss] 标记）</label>
    <textarea id="transcript" placeholder="[00:12] 打磨这道工序如果砂轮不平，会出现橘皮纹。&#10;[00:48] 条件：转速控制在 1800 转以下。"></textarea>
    <button id="startBtn" disabled>建立访谈并登记转写</button>
    <div class="hint">说明：授权只允许本人；演示身份同时做审核，生产环境需要独立审核人。转写不发往任何外部服务（ADR-05 未签，不接 ASR）。</div>
  </section>

  <section class="panel">
    <h2>② 草稿与人工审核</h2>
    <div id="drafts"><div class="meta">左侧完成授权与转写后，这里会显示访谈状态与草稿。</div></div>
  </section>
</main>
<div id="toast" class="toast"></div>

<script>
const state = { consentId: null, interviewId: null, subject: null };

function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function toast(msg, isErr) {
  const el = document.getElementById("toast");
  el.textContent = msg; el.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(el._t); el._t = setTimeout(() => { el.className = "toast"; }, 3200);
}

async function loadMe() {
  const res = await fetch("/api/status");
  const body = await res.json();
  state.subject = body.subject || "";
  document.getElementById("subject").value = state.subject || "(未知)";
}

async function grant() {
  const payload = {
    subject_id: state.subject,
    method: document.getElementById("method").value,
    retention_days: Number(document.getElementById("retention").value || 90),
    receipt_uri: "consent://demo-notice-v1",
  };
  const res = await fetch("/api/interviews/consents", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
  });
  const body = await res.json();
  if (!res.ok) { toast("授权失败：" + (body.error?.code || res.status), true); return; }
  state.consentId = body.consent_id;
  document.getElementById("consentState").textContent = "已登记授权 " + body.consent_id.slice(0, 8) + "…";
  document.getElementById("startBtn").disabled = false;
  toast("授权已登记");
}

async function start() {
  const res = await fetch("/api/interviews", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({subject_id: state.subject, consent_id: state.consentId}),
  });
  const body = await res.json();
  if (!res.ok) { toast("建立访谈失败：" + (body.error?.code || res.status), true); return; }
  state.interviewId = body.interview_id;
  const text = document.getElementById("transcript").value.trim();
  if (text) {
    const t = await fetch(`/api/interviews/${state.interviewId}/transcript`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({content: text, kind: "HUMAN_EDITED"}),
    });
    if (!t.ok) { toast("登记转写失败", true); }
    const d = await fetch(`/api/interviews/${state.interviewId}/drafts`, {method: "POST"});
    if (!d.ok) { toast("生成草稿失败", true); }
  }
  await refresh();
}

async function refresh() {
  if (!state.interviewId) return;
  const res = await fetch(`/api/interviews/${state.interviewId}`);
  const body = await res.json();
  const box = document.getElementById("drafts");
  if (!res.ok) { box.innerHTML = '<div class="meta">读取失败：' + esc(body.error?.code || res.status) + "</div>"; return; }
  const drafts = (body.drafts || []).map(d => `
    <div class="block">
      <h3>草稿 <span class="badge b-${esc(d.status)}">${esc(d.status)}</span></h3>
      <pre>${esc(d.claim || "(已清除内容)")}</pre>
      <div class="meta">证据位：${(d.evidence_spans || []).map(s =>
        esc(Math.round((s.start_ms||0)/1000) + "s") + (s.quote ? "「" + esc(s.quote.slice(0, 24)) + "…」" : "")).join("　")}</div>
      <button data-review="${esc(d.draft_id)}" data-decision="APPROVE">通过</button>
      <button data-review="${esc(d.draft_id)}" data-decision="REJECT">驳回</button>
      <button data-promote="${esc(d.draft_id)}">晋升为知识草稿</button>
    </div>`).join("");
  box.innerHTML = `<div class="meta">访谈 <code>${esc(body.interview_id.slice(0, 8))}</code> ·
    <span class="badge b-${esc(body.status)}">${esc(body.status)}</span> ·
    转写 ${body.transcripts} 条</div>` + (drafts || '<div class="meta">暂无草稿</div>');
}

async function review(draftId, decision) {
  const res = await fetch(`/api/interviews/drafts/${draftId}/review`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({decision}),
  });
  const body = await res.json();
  toast(res.ok ? "已" + (decision === "APPROVE" ? "通过" : "驳回") : "审核失败：" + (body.error?.code || res.status), !res.ok);
  await refresh();
}

async function promote(draftId) {
  const res = await fetch(`/api/interviews/drafts/${draftId}/promote`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({domain: "owner_experience", required_scope: "internal_training"}),
  });
  const body = await res.json();
  toast(res.ok ? "已晋升为知识草稿（DRAFT，仍需审核）" : "晋升失败：" + (body.error?.code || res.status), !res.ok);
  await refresh();
}

document.addEventListener("click", ev => {
  const r = ev.target.closest("[data-review]");
  if (r) return review(r.dataset.review, r.dataset.decision);
  const p = ev.target.closest("[data-promote]");
  if (p) return promote(p.dataset.promote);
});
document.getElementById("grantBtn").addEventListener("click", grant);
document.getElementById("startBtn").addEventListener("click", start);
loadMe().then(() => {
  const preset = new URLSearchParams(location.search).get("interview");
  if (preset) { state.interviewId = preset; refresh(); }
});
</script>
</body>
</html>
"""
