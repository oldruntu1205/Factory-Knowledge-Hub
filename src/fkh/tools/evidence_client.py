"""证据查看器（前端共享组件）。

两个页面（问答页、冲突待办页）都要"点引用/点候选看原文"，逻辑只能有一份，
所以做成独立 JS 资源由演示服务托管，页面用 <script src="/assets/evidence.js"> 引入。

做法与边界：
- 弹层 DOM 按需注入，不污染页面结构；
- 定位靠两个锚点（规格里含数字的最长 token + 单价）同时命中同一行才高亮；
- 定位不到就明说，不假装成功；
- 只调 /api/evidence/{chunk_id}，令牌在服务端，浏览器里没有。
"""

EVIDENCE_JS = r"""/* Factory Knowledge Hub · 证据查看器（共享） */
(function () {
  const CSS = `
  .fkh-ev-overlay { position:fixed; inset:0; background:rgba(4,8,12,.72); display:none;
                    align-items:center; justify-content:center; padding:24px; z-index:50; }
  .fkh-ev-overlay.open { display:flex; }
  .fkh-ev-modal { background:#161d26; border:1px solid #243040; border-radius:14px;
                  width:min(860px,100%); max-height:86vh; display:flex; flex-direction:column;
                  color:#e6edf3; font:15px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  .fkh-ev-head { display:flex; align-items:center; gap:12px; padding:14px 18px;
                 border-bottom:1px solid #243040; }
  .fkh-ev-head h2 { font-size:15px; margin:0; font-weight:600; }
  .fkh-ev-head .sp { flex:1; }
  .fkh-ev-head button { cursor:pointer; border-radius:8px; border:1px solid #243040;
                        background:transparent; color:#e6edf3; padding:6px 12px; font-size:13px; }
  .fkh-ev-body { padding:16px 18px; overflow:auto; }
  .fkh-ev-meta { font-size:12.5px; color:#8b9bb0; margin-bottom:12px;
                 position:sticky; top:-16px; background:#161d26; padding:8px 0 6px;
                 z-index:1; }
  .fkh-ev-meta code { color:#e6edf3; }
  .fkh-ev-text { background:#0b1016; border:1px solid #243040; border-radius:10px; padding:14px; }
  .fkh-ev-line { white-space:pre-wrap; word-break:break-word;
                 font:13px/1.7 "Consolas","Microsoft YaHei",monospace; margin:0; }
  .fkh-ev-line.hit { background:#1b2430; border-left:2px solid #d29922; padding-left:8px;
                     margin-left:-10px; }
  .fkh-ev-line.hit mark { background:#3b2f0a; color:#ffd479; border-radius:3px; padding:0 2px; }
  .fkh-ev-warn { color:#d29922; font-size:12.5px; margin-bottom:10px; }`;

  function esc(s) {
    return String(s ?? "").replace(/[&<>"]/g,
      c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  }

  function specToken(spec) {
    const tokens = String(spec || "").split(/\s+/).filter(Boolean);
    const withDigit = tokens.filter(t => /\d/.test(t));
    const pool = withDigit.length ? withDigit : tokens;
    return pool.sort((a, b) => b.length - a.length)[0] || "";
  }

  function render(text, price, spec) {
    const anchor = specToken(spec);
    let hits = 0;
    const html = String(text || "").split("\n").map(line => {
      const norm = line.replace(/\s+/g, " ");
      const hit = price && anchor && norm.includes(String(price)) && norm.includes(anchor);
      if (hit) { hits += 1; return '<div class="fkh-ev-line hit"><mark>' + esc(line) + "</mark></div>"; }
      return '<div class="fkh-ev-line">' + esc(line) + "</div>";
    }).join("");
    return { html, hits, anchor };
  }

  function ensureDom() {
    if (document.getElementById("fkh-ev-overlay")) return;
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
    const overlay = document.createElement("div");
    overlay.id = "fkh-ev-overlay";
    overlay.className = "fkh-ev-overlay";
    overlay.innerHTML =
      '<div class="fkh-ev-modal">' +
        '<div class="fkh-ev-head"><h2 id="fkh-ev-title">证据原文</h2><span class="sp"></span>' +
        '<button id="fkh-ev-close">关闭</button></div>' +
        '<div class="fkh-ev-body"><div class="fkh-ev-meta" id="fkh-ev-meta"></div>' +
        '<div class="fkh-ev-text" id="fkh-ev-text">加载中…</div></div>' +
      "</div>";
    document.body.appendChild(overlay);
    overlay.addEventListener("click", ev => { if (ev.target === overlay) close(); });
    document.getElementById("fkh-ev-close").addEventListener("click", close);
    document.addEventListener("keydown", ev => { if (ev.key === "Escape") close(); });
  }

  function close() {
    const overlay = document.getElementById("fkh-ev-overlay");
    if (overlay) overlay.classList.remove("open");
  }

  async function open(chunkId, price, spec) {
    ensureDom();
    const overlay = document.getElementById("fkh-ev-overlay");
    const textEl = document.getElementById("fkh-ev-text");
    document.getElementById("fkh-ev-title").textContent = "证据原文";
    document.getElementById("fkh-ev-meta").textContent = "";
    textEl.textContent = "加载中…";
    overlay.classList.add("open");
    try {
      const res = await fetch("/api/evidence/" + encodeURIComponent(chunkId));
      const body = await res.json();
      if (!res.ok) {
        textEl.textContent = "取不到原文：" + (body.error?.code || res.status);
        return;
      }
      const loc = body.locator || {};
      const heading = (body.heading_path || []).join(" / ") || "—";
      const pageFromHeading = (heading.match(/第\s*(\d+)\s*页/) || [])[1];
      const where = loc.page_start ? "第 " + loc.page_start + " 页"
                  : (pageFromHeading ? "第 " + pageFromHeading + " 页"
                  : (loc.block_index !== undefined ? "第 " + (Number(loc.block_index) + 1) + " 块" : "—"));
      document.getElementById("fkh-ev-title").textContent = "证据原文 · " + where;
      document.getElementById("fkh-ev-meta").innerHTML =
        "版本 <code>" + esc(String(body.version_id).slice(0, 8)) + "</code>" +
        " · 状态 <code>" + esc(body.status) + "</code>" +
        " · 正文层级 <code>" + esc(body.text_level) + "</code><br>章节：" + esc(heading);
      const { html, hits, anchor } = render(body.text || "", price, spec);
      textEl.innerHTML = html || "（空）";
      if (price && spec && !hits) {
        textEl.insertAdjacentHTML("afterbegin",
          '<div class="fkh-ev-warn">未能在本块原文中定位到该候选行（锚点：' +
          esc(anchor) + " / " + esc(price) + "）——可能是父块合并或格式差异，请人工核对。</div>");
      } else if (hits) {
        // 只滚动内容区，不用 scrollIntoView：那会把外层容器一起滚走，
        // 顶上「版本/状态/章节」这一行就被顶出视野（实测踩过）。
        const first = textEl.querySelector(".fkh-ev-line.hit");
        const scroller = textEl.parentElement;
        if (first && scroller) {
          // 用相对视口矩形算偏移：offsetTop 在非定位祖先下是整页坐标，直接用会滚过头
          const lineBox = first.getBoundingClientRect();
          const box = scroller.getBoundingClientRect();
          scroller.scrollTop +=
            lineBox.top - box.top - scroller.clientHeight / 2 + lineBox.height / 2;
        }
      }
    } catch (e) {
      textEl.textContent = "请求失败：" + e;
    }
  }

  window.fkhEvidence = { open, close };
})();
"""
