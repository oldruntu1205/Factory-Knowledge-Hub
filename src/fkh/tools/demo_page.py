"""本地私域演示页（单文件 HTML，由 demo_app 的内置 HTTP 服务托管）。

刻意不放进 FastAPI 路由：演示页不属于对外契约，挂在别处也避免污染 OpenAPI。
页面只做两件事：把问题发给本地 API、把「状态 / 答案 / 引用 / 审计」摊开给看的人。
"""

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Factory Knowledge Hub · 本地演示</title>
<style>
  :root {
    --bg:#0f1419; --panel:#161d26; --line:#243040; --text:#e6edf3; --muted:#8b9bb0;
    --ok:#3fb950; --warn:#d29922; --bad:#f85149; --accent:#4493f8;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  header { padding:20px 28px; border-bottom:1px solid var(--line); display:flex;
           align-items:baseline; gap:14px; flex-wrap:wrap; }
  h1 { font-size:19px; margin:0; font-weight:600; }
  .tag { font-size:12px; color:var(--muted); border:1px solid var(--line);
         border-radius:999px; padding:2px 10px; }
  main { max-width:960px; margin:0 auto; padding:26px 20px 60px; }
  .search { display:flex; gap:10px; margin-bottom:14px; }
  input[type=text] { flex:1; padding:12px 14px; border-radius:10px; border:1px solid var(--line);
                     background:var(--panel); color:var(--text); font-size:15px; }
  input[type=text]:focus { outline:none; border-color:var(--accent); }
  button { padding:12px 18px; border-radius:10px; border:1px solid var(--line);
           background:var(--accent); color:#fff; font-size:15px; cursor:pointer; }
  button:disabled { opacity:.55; cursor:default; }
  .examples { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:22px; }
  .example { font-size:13px; padding:6px 12px; border-radius:999px; cursor:pointer;
             border:1px solid var(--line); color:var(--muted); background:transparent; }
  .example:hover { color:var(--text); border-color:var(--accent); }
  .stats { display:flex; gap:18px; flex-wrap:wrap; font-size:13px; color:var(--muted);
           margin-bottom:18px; }
  .stats b { color:var(--text); font-weight:600; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
          padding:18px 20px; margin-bottom:14px; }
  .card h2 { font-size:14px; margin:0 0 10px; color:var(--muted); font-weight:600;
             letter-spacing:.04em; text-transform:uppercase; }
  .badge { display:inline-block; font-size:12px; padding:2px 10px; border-radius:999px;
           border:1px solid; margin-right:8px; }
  .b-ANSWERED { color:var(--ok); border-color:var(--ok); }
  .b-REFUSED { color:var(--warn); border-color:var(--warn); }
  .b-HUMAN_REVIEW_REQUIRED { color:var(--bad); border-color:var(--bad); }
  .answer { font-size:16px; white-space:pre-wrap; }
  .meta { font-size:12.5px; color:var(--muted); margin-top:10px; }
  .cite { font-size:13px; border-left:2px solid var(--accent); padding:4px 0 4px 10px;
          margin-top:8px; color:var(--muted); }
  .cite code { color:var(--text); font-size:12px; }
  .cite-open { margin-right:8px; padding:2px 8px; font-size:12px; border-radius:6px;
               border:1px solid var(--line); background:transparent; color:var(--accent); cursor:pointer; }
  .cite-open:hover { border-color:var(--accent); }
  details { margin-top:10px; }
  summary { cursor:pointer; font-size:12.5px; color:var(--muted); }
  pre { background:#0b1016; border:1px solid var(--line); border-radius:8px; padding:12px;
        overflow:auto; font-size:12px; max-height:280px; }
  .empty { color:var(--muted); font-size:14px; }
</style>
</head>
<body>
<header>
  <h1>Factory Knowledge Hub</h1>
  <nav style="display:flex;gap:8px"><a href="/" style="color:#e6edf3;text-decoration:none;font-size:14px;padding:4px 10px;border:1px solid #243040;border-radius:8px">问答</a><a href="/conflicts" style="color:#8b9bb0;text-decoration:none;font-size:14px;padding:4px 10px;border:1px solid transparent;border-radius:8px">冲突待办</a><a href="/documents" style="color:#8b9bb0;text-decoration:none;font-size:14px;padding:4px 10px;border:1px solid transparent;border-radius:8px">文档审核</a></nav><span class="tag">本地私域模拟</span>
  <span class="tag">127.0.0.1 · 无外网</span>
  <span class="tag" id="provider-tag">provider: …</span>
</header>
<main>
  <div class="search">
    <input id="q" type="text" placeholder="输入货号或问题，例如：A112420 的单价是多少？" autocomplete="off" />
    <button id="go">提问</button>
  </div>
  <div class="examples" id="examples"></div>

  <div class="stats">
    <span>租户 <b id="s-tenant">—</b></span>
    <span>切片 <b id="s-chunks">—</b></span>
    <span>目录条目 <b id="s-items">—</b></span>
    <span>冲突待办 <b id="s-conflicts">—</b></span>
  </div>

  <div id="out" class="empty">还没有提问。</div>
</main>
<script>
const EXAMPLES = [
  "A112420 的单价是多少？",
  "A112430 的规格是什么？",
  "冷凝管的标口范围是多少？",
  "有哪些规格的试管？",
  "你们的售后保修政策是什么？"
];
const ex = document.getElementById("examples");
EXAMPLES.forEach(text => {
  const b = document.createElement("button");
  b.className = "example";
  b.textContent = text;
  b.onclick = () => { document.getElementById("q").value = text; ask(); };
  ex.appendChild(b);
});

async function loadStatus() {
  try {
    const r = await fetch("/api/status");
    const s = await r.json();
    document.getElementById("s-tenant").textContent = s.tenant_label || "—";
    document.getElementById("s-chunks").textContent = s.chunks ?? "—";
    document.getElementById("s-items").textContent = s.items ?? "—";
    document.getElementById("s-conflicts").textContent = s.conflicts_open ?? "—";
    document.getElementById("provider-tag").textContent = "provider: " + (s.provider || "none");
  } catch (e) {
    document.getElementById("provider-tag").textContent = "provider: 连不上";
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
}

async function ask() {
  const q = document.getElementById("q").value.trim();
  if (!q) return;
  const go = document.getElementById("go");
  go.disabled = true; go.textContent = "查询中…";
  document.getElementById("out").className = "";
  document.getElementById("out").innerHTML = '<div class="card">查询中，本地模型可能需要几秒…</div>';
  try {
    const r = await fetch("/api/answer", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({query: q, knowledge_scope: "internal_product",
                            language: "zh-CN", max_evidence: 5})
    });
    const body = await r.json();
    render(q, body, r.status);
  } catch (e) {
    document.getElementById("out").innerHTML =
      '<div class="card"><span class="badge b-HUMAN_REVIEW_REQUIRED">请求失败</span>' + esc(e) + '</div>';
  } finally {
    go.disabled = false; go.textContent = "提问";
  }
}

function render(q, body, status) {
  const st = body.status || (body.error?.code || ("HTTP " + status));
  if (body.error) {
    document.getElementById("out").innerHTML =
      '<div class="card"><h2>问题</h2><div class="answer">' + esc(q) + '</div></div>' +
      '<div class="card"><h2>回答</h2><span class="badge b-HUMAN_REVIEW_REQUIRED">' +
      esc(st) + '</span><div class="meta">' + esc(body.error.message || "") +
      '（trace ' + esc(body.error.trace_id || "-").slice(0, 8) + '）</div>' +
      '<div class="meta">这是能力故障，不是"知识不足"的拒答；恢复推理服务后重试即可。</div></div>';
    return;
  }
  let html = '<div class="card"><h2>问题</h2><div class="answer">' + esc(q) + '</div></div>';
  html += '<div class="card"><h2>回答</h2>';
  html += '<span class="badge b-' + esc(st) + '">' + esc(st) + '</span>';
  if (body.refusal_code) html += '<span class="meta">reason: ' + esc(body.refusal_code) + '</span>';
  html += '<div class="answer" style="margin-top:10px">' + esc(body.answer || "（空）") + '</div>';
  if (body.unsupported) {
    html += '<div class="meta">一致性检查未支持数量：code ' + esc(body.unsupported.codes ?? "不适用") +
            ' / 数值 ' + esc(body.unsupported.numbers ?? "不适用") + '</div>';
  }
  const cites = body.citations || [];
  if (cites.length) {
    html += '<details open><summary>引用 ' + cites.length + ' 条（系统侧）</summary>';
    cites.forEach((c, i) => {
      html += '<div class="cite">[' + (i + 1) + '] ' +
              '<button class="cite-open" data-ev="' + esc(c.evidence_id) + '">查看原文</button>' +
              ' version <code>' + esc(String(c.version_id)).slice(0, 8) +
              '</code> · locator <code>' + esc(JSON.stringify(c.locator)) + '</code></div>';
    });
    html += '</details>';
  }
  html += '<details><summary>原始响应（审计字段）</summary><pre>' +
          esc(JSON.stringify(body, null, 2)) + '</pre></details>';
  html += '</div>';
  document.getElementById("out").innerHTML = html;
}

document.addEventListener("click", ev => {
  const btn = ev.target.closest("[data-ev]");
  if (btn) window.fkhEvidence.open(btn.dataset.ev);
});

document.getElementById("go").onclick = ask;
document.getElementById("q").addEventListener("keydown", e => { if (e.key === "Enter") ask(); });
loadStatus();

// 支持 ?q=... 直接带问题打开（演示/截图用）
const preset = new URLSearchParams(location.search).get("q");
if (preset) {
  document.getElementById("q").value = preset;
  ask().then(() => {
    // 演示/截图：?openfirst=1 自动打开第一条引用
    if (new URLSearchParams(location.search).get("openfirst")) {
      const btn = document.querySelector("[data-ev]");
      if (btn) window.fkhEvidence.open(btn.dataset.ev);
    }
  });
}
</script>
<script src="/assets/evidence.js"></script>
</body>
</html>
"""
