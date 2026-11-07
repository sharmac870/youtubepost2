// Trending Posts console: dashboard, review queue, posts, health. No third-party code; all text from the
// API is inserted as text (never as HTML), because titles and scripts come from untrusted sources.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const state = { view: "dashboard", days: 30, me: null, health: null, filter: { q: "", state: "", platform: "" }, chartTable: false };

const STATE_INFO = {
  DISCOVERED: ["Discovered", "info"], SCRIPTED: ["Scripted", "info"], AWAITING_APPROVAL: ["Awaiting approval", "warning"],
  APPROVED: ["Approved", "info"], RENDERING: ["Rendering", "info"], RENDERED: ["Rendered", "info"],
  QA_PASSED: ["QA passed", "info"], PUBLISHING: ["Publishing", "info"], PUBLISHED: ["Published", "good"],
  PARTIALLY_PUBLISHED: ["Partly published", "serious"], FAILED: ["Failed", "critical"], REJECTED: ["Rejected", ""],
  EXPIRED: ["Expired", ""], SKIPPED: ["Skipped", ""],
};
const PUB_INFO = { LIVE: ["Live", "good"], PENDING: ["Queued", "info"], UPLOADING: ["Uploading", "info"],
  PROCESSING: ["Processing", "info"], FAILED: ["Failed", "critical"], REMOVED: ["Removed", ""], "": ["Not started", ""] };
const PLATFORM = { YOUTUBE: "YouTube", INSTAGRAM: "Instagram" };

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style") el.style.cssText = v; // CSSOM, allowed by the strict CSP (style attributes are not)
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const svg = (tag, attrs = {}, ...children) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  for (const c of children.flat(Infinity)) if (c) el.append(c);
  return el;
};

async function api(path, options = {}) {
  const init = { headers: { "X-Requested-With": "ytp" }, ...options };
  if (options.body && typeof options.body !== "string") {
    init.body = JSON.stringify(options.body);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  if (!res.ok) {
    let msg = `${res.status}`;
    try { const j = await res.json(); msg = j.detail || j.error || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 3500);
}

const nf = new Intl.NumberFormat("en-IN");
const compact = new Intl.NumberFormat("en-IN", { notation: "compact", maximumFractionDigits: 1 });
function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 0) { const m = Math.round(-s / 60); return m < 90 ? `in ${m} min` : `in ${Math.round(m / 60)} h`; }
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function pill(label, tone) {
  return h("span", { class: `pill ${tone || ""}` }, h("span", { class: "dot", "aria-hidden": "true" }), label);
}
const statePill = (s) => pill(...(STATE_INFO[s] || [s, ""]));
function pubPills(platforms) {
  return h("span", { class: "plat" }, Object.entries(platforms || {}).map(([p, v]) => {
    const [label, tone] = PUB_INFO[v.state] || [v.state, ""];
    const el = pill(`${PLATFORM[p]}: ${label}`, tone);
    if (v.url && v.state === "LIVE") return h("a", { href: v.url, target: "_blank", rel: "noopener", onclick: (e) => e.stopPropagation() }, el);
    return el;
  }));
}

// ----- navigation -----

function setView(view) {
  state.view = view;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === view)));
  try { localStorage.setItem("ytp.view", view); } catch {}
  render();
}

async function render() {
  const main = $("#main");
  main.replaceChildren(h("div", { class: "empty" }, "Loading…"));
  try {
    if (state.view === "dashboard") await renderDashboard(main);
    else if (state.view === "review") await renderReview(main);
    else if (state.view === "posts") await renderPosts(main);
    else await renderHealth(main);
  } catch (e) {
    main.replaceChildren(h("div", { class: "card empty" }, `Couldn't load this page: ${e.message}`));
  }
}

async function refreshChrome() {
  try {
    const health = await api("/api/health");
    state.health = health;
    const banner = $("#banner");
    const msgs = [];
    if (health.kill_switch) msgs.push("Kill switch is ON: nothing will be published until an admin turns it off.");
    for (const [p, s] of Object.entries(health.credentials)) if (s === "REVOKED") msgs.push(`${PLATFORM[p]} sign-in failed: publishing to ${PLATFORM[p]} is stopped (see Health).`);
    const dead = Object.values(health.queues).reduce((a, q) => a + Math.max(0, q.dead || 0), 0);
    if (dead) msgs.push(`${dead} message(s) dead-lettered: see Health.`);
    banner.textContent = msgs.join("  ·  ");
    banner.hidden = !msgs.length;
  } catch {}
  try {
    const r = await api("/api/items?state=AWAITING_APPROVAL&limit=1");
    $("#review-count").textContent = r.total ? String(r.total) : "";
  } catch {}
}

// ----- dashboard -----

async function renderDashboard(main) {
  const s = await api(`/api/summary?days=${state.days}`);
  const range = h("div", { class: "seg", role: "group", "aria-label": "Time range" },
    [7, 30, 90].map((d) => h("button", { "aria-pressed": String(state.days === d), onclick: () => { state.days = d; render(); } }, `${d} days`)));
  const toolbar = h("div", { class: "toolbar" }, h("h1", {}, "Dashboard"), h("div", { class: "spacer" }), range,
    h("button", { class: "btn", onclick: discoverNow }, "Find trends now"));

  const total = s.published.YOUTUBE + s.published.INSTAGRAM;
  const views = s.views.YOUTUBE + s.views.INSTAGRAM;
  const tile = (label, value, sub, split) => h("div", { class: "card tile" },
    h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub ? h("div", { class: "sub" }, sub) : null, split || null);
  const split = (a, b) => h("div", { class: "split" },
    h("span", { class: "key" }, h("span", { class: "swatch yt" }), `YouTube ${a}`),
    h("span", { class: "key" }, h("span", { class: "swatch ig" }), `Instagram ${b}`));
  const kpis = h("div", { class: "grid kpis" },
    tile("Posts published", nf.format(total), `last ${s.days} days`, split(s.published.YOUTUBE, s.published.INSTAGRAM)),
    tile("Views", compact.format(views), "on posts from this period", split(compact.format(s.views.YOUTUBE), compact.format(s.views.INSTAGRAM))),
    tile("Awaiting your approval", nf.format(s.awaiting_approval), "drafts expire after 6 hours"),
    tile("In progress", nf.format(s.in_flight), "scripting, rendering or publishing"),
    tile("Need attention", nf.format(s.needs_attention), "failed or published on one platform only"),
  );

  const chartCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Posts per day"),
      h("div", { class: "legend" }, h("span", { class: "key" }, h("span", { class: "swatch yt" }), "YouTube Shorts"),
        h("span", { class: "key" }, h("span", { class: "swatch ig" }), "Instagram Reels")),
      h("button", { class: "btn", onclick: () => { state.chartTable = !state.chartTable; render(); } }, state.chartTable ? "Show chart" : "Show table")),
    state.chartTable ? postsTable(s) : barChart(s));

  const cats = s.by_category;
  const maxCat = Math.max(1, ...cats.map((c) => c[1]));
  const catCard = h("div", { class: "card" }, h("h2", {}, "Published by category"),
    cats.length ? h("div", { class: "bars-h" }, cats.map(([name, n]) => [
      h("div", { class: "row" }, h("span", {}, name), h("span", { class: "secondary" }, String(n))),
      h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(n / maxCat) * 100}%` }))])) : h("div", { class: "empty" }, "Nothing published yet."));

  const funnelOrder = ["AWAITING_APPROVAL", "APPROVED", "RENDERING", "PUBLISHING", "PUBLISHED", "PARTIALLY_PUBLISHED", "FAILED", "REJECTED", "EXPIRED"];
  const funnel = h("div", { class: "card" }, h("h2", {}, "Pipeline right now"),
    h("table", {}, h("tbody", {}, funnelOrder.filter((k) => s.states[k]).map((k) =>
      h("tr", { class: "click", onclick: () => { state.filter = { q: "", state: k, platform: "" }; setView("posts"); } },
        h("td", {}, statePill(k)), h("td", { class: "num" }, String(s.states[k])))))),
    Object.keys(s.states).length ? null : h("div", { class: "empty" }, "No items yet. Discovery runs every 6 hours."));

  const top = h("div", { class: "card" }, h("h2", {}, "Top posts by views"),
    s.top.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Post"), h("th", { class: "num" }, "YouTube views"), h("th", { class: "num" }, "Instagram views"))),
      h("tbody", {}, s.top.map((t) => h("tr", { class: "click", onclick: () => openItem(t.id) },
        h("td", { class: "title-cell" }, h("div", { class: "t" }, t.title)),
        h("td", { class: "num" }, nf.format(t.platforms.YOUTUBE?.views || 0)),
        h("td", { class: "num" }, nf.format(t.platforms.INSTAGRAM?.views || 0))))))) : h("div", { class: "empty" }, "Views appear once posts are live (refreshed every 30 minutes)."));

  main.replaceChildren(toolbar, kpis, h("div", { class: "grid two" }, chartCard, catCard), h("div", { class: "grid two", style: "margin-top:16px" }, top, funnel),
    await channelSection());
}

// Whole-channel analytics: every upload on the channel, not only this pipeline's. Refreshed by the sweep job.
async function channelSection() {
  let c;
  try { c = await api(`/api/channel?days=${state.days}`); } catch (e) { return h("div", { class: "card empty", style: "margin-top:16px" }, `Channel analytics unavailable: ${e.message}`); }
  if (!c.ready) return h("div", { class: "card empty", style: "margin-top:16px" }, "Channel analytics appear after the first sweep run with a YouTube API key (every 3 hours).");
  const ch = c.channel || {};
  const tile = (label, value, sub) => h("div", { class: "card tile" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub ? h("div", { class: "sub" }, sub) : null);
  const calendar = c.per_day.map((d) => d.date);
  const perDay = Object.fromEntries(c.per_day.map((d) => [d.date, d.count]));
  const chart = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Channel uploads per day"),
    h("div", { class: "legend" }, h("span", { class: "key" }, h("span", { class: "swatch yt" }), "All uploads"))),
    barChart({ calendar, days: c.days, posts_per_day: { YOUTUBE: perDay, INSTAGRAM: {} } }, true));
  const top = h("div", { class: "card" }, h("h2", {}, "Most viewed uploads"),
    c.top.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Video"), h("th", {}, "Type"), h("th", { class: "num" }, "Views"), h("th", { class: "num" }, "Likes"))),
      h("tbody", {}, c.top.map((v) => h("tr", {},
        h("td", { class: "title-cell" }, h("a", { href: v.url, target: "_blank", rel: "noopener noreferrer" }, v.title), h("div", { class: "muted" }, when(v.published_at))),
        h("td", {}, v.is_short ? "Short" : "Video"),
        h("td", { class: "num" }, nf.format(v.views)), h("td", { class: "num" }, nf.format(v.likes))))))) : h("div", { class: "empty" }, "No uploads in this period."));
  return h("section", { style: "margin-top:24px" },
    h("div", { class: "toolbar" }, h("h1", {}, `Channel: ${ch.title || ""}`), h("span", { class: "muted" }, `${ch.handle || ""} · updated ${ago(c.fetched_at)}`)),
    h("div", { class: "grid kpis" },
      tile("Subscribers", ch.subscribers == null ? "hidden" : compact.format(ch.subscribers), `${compact.format(ch.total_views || 0)} views all time`),
      tile("Uploads", nf.format(c.published), `${c.shorts} Shorts · ${c.long_videos} videos, last ${c.days} days`),
      tile("Views on these uploads", compact.format(c.views), `${nf.format(c.average_views)} on average`),
      tile("Likes and comments", compact.format(c.likes + c.comments), `${nf.format(c.likes)} likes · ${nf.format(c.comments)} comments`)),
    h("div", { class: "grid two", style: "align-items:start" }, chart, top));
}

function postsTable(s) {
  return h("div", { class: "table-wrap", style: "max-height:300px;overflow:auto" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Day"), h("th", { class: "num" }, "YouTube"), h("th", { class: "num" }, "Instagram"))),
    h("tbody", {}, s.calendar.slice().reverse().map((d) => h("tr", {}, h("td", {}, d),
      h("td", { class: "num" }, String(s.posts_per_day.YOUTUBE[d] || 0)), h("td", { class: "num" }, String(s.posts_per_day.INSTAGRAM[d] || 0)))))));
}

function barChart(s, single = false) {
  const W = 720, H = 240, L = 28, B = 22, T = 8;
  const days = s.calendar;
  const yt = days.map((d) => s.posts_per_day.YOUTUBE[d] || 0);
  const ig = days.map((d) => s.posts_per_day.INSTAGRAM[d] || 0);
  const max = Math.max(2, ...days.map((_, i) => yt[i] + ig[i]));
  const step = Math.ceil(max / 4);
  const top = step * 4;
  const plotW = W - L, plotH = H - B - T;
  const slot = plotW / days.length, bw = Math.max(2, Math.min(28, slot * 0.62));
  const y = (v) => T + plotH - (v / top) * plotH;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": single ? `Channel uploads per day for the last ${s.days} days` : `Posts per day for the last ${s.days} days, YouTube and Instagram stacked` });
  const grid = svg("g", { class: "axis" });
  for (let v = 0; v <= top; v += step) {
    grid.append(svg("line", { class: "gridline", x1: L, x2: W, y1: y(v), y2: y(v) }));
    const t = svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }); t.textContent = String(v); grid.append(t);
  }
  const labelEvery = Math.ceil(days.length / 8);
  days.forEach((d, i) => {
    if ((i % labelEvery === 0 && days.length - 1 - i >= labelEvery / 2) || i === days.length - 1) {
      const t = svg("text", { x: L + slot * i + slot / 2, y: H - 6, "text-anchor": "middle" });
      t.textContent = new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" });
      grid.append(t);
    }
  });
  root.append(grid);
  const bars = svg("g");
  days.forEach((d, i) => {
    const x = L + slot * i + (slot - bw) / 2;
    const g = svg("g", { class: "col" });
    const gap = 2;
    // Rounded data-end only on the top segment; a 2px surface gap separates stacked fills.
    const seg = (v0, v1, cls, roundTop) => {
      if (v1 <= v0) return null;
      const y0 = y(v1), hgt = Math.max(1, y(v0) - y(v1) - (v0 > 0 ? gap : 0));
      const r = roundTop ? Math.min(4, bw / 2, hgt) : 0;
      const path = `M${x},${y0 + hgt} L${x},${y0 + r} Q${x},${y0} ${x + r},${y0} L${x + bw - r},${y0} Q${x + bw},${y0} ${x + bw},${y0 + r} L${x + bw},${y0 + hgt} Z`;
      return svg("path", { d: path, class: `bar ${cls}` });
    };
    g.append(...[seg(0, yt[i], "bar-yt", !ig[i]), seg(yt[i], yt[i] + ig[i], "bar-ig", true)].filter(Boolean));
    const hit = svg("rect", { class: "hit", x: L + slot * i, y: T, width: slot, height: plotH });
    hit.addEventListener("mousemove", (e) => showTip(e, new Date(d + "T00:00:00").toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" }),
      single ? [["yt", "Uploads", yt[i]]] : [["yt", "YouTube", yt[i]], ["ig", "Instagram", ig[i]]]));
    hit.addEventListener("mouseleave", hideTip);
    bars.append(g, hit);
  });
  root.append(bars);
  return h("div", { class: "chart" }, root);
}

function showTip(e, title, rows) {
  const tip = $("#tooltip");
  tip.replaceChildren(h("div", { class: "t" }, title), ...rows.map(([k, label, v]) =>
    h("div", { class: "row" }, h("span", { class: "key" }, h("span", { class: `swatch ${k}` }), label), h("b", {}, String(v)))));
  tip.hidden = false;
  const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  tip.style.left = `${x}px`;
  tip.style.top = `${e.clientY + 14}px`;
}
function hideTip() { $("#tooltip").hidden = true; }

async function discoverNow() {
  try {
    const r = await api("/api/discover", { method: "POST" });
    toast(`Discovery started (${r.run_key}). New drafts appear in Review within a few minutes.`);
  } catch (e) { toast(`Couldn't start discovery: ${e.message}`); }
}

// ----- review -----

// "Create from theme" (from the YoutubePosts Studio): the worker finds recent coverage of the theme and
// writes a draft, which then waits here for approval like any other.
function openThemeForm() {
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const terms = String(fd.get("terms") || "").split(",").map((t) => t.trim()).filter(Boolean).slice(0, 4);
    try {
      await api("/api/create", { method: "POST", body: { theme: fd.get("theme"), terms, hours: Number(fd.get("hours")) } });
      toast("Looking for coverage now. The draft appears in Review within a few minutes.");
      closeDrawer();
    } catch (err) { toast(`Couldn't start: ${err.message}`); }
  } },
    h("h1", { id: "drawer-title" }, "Create from theme"),
    h("p", { class: "muted" }, "Describe what the Short should be about. Recent news on it is gathered for facts and sources, then Claude writes an original script. It still goes through the policy check and your approval before anything is posted."),
    h("div", { class: "field" }, h("label", { for: "f-theme" }, "Theme"),
      h("input", { type: "text", id: "f-theme", name: "theme", required: true, minlength: 3, maxlength: 200, placeholder: "e.g. Why UPI Lite is suddenly everywhere" })),
    h("div", { class: "field" }, h("label", { for: "f-terms" }, "News search terms (optional, comma separated, up to 4)"),
      h("input", { type: "text", id: "f-terms", name: "terms", maxlength: 320, placeholder: "UPI Lite, NPCI" })),
    h("div", { class: "field" }, h("label", { for: "f-hours" }, "Look back"),
      h("select", { id: "f-hours", name: "hours" }, [["24", "24 hours"], ["48", "2 days"], ["168", "7 days"]].map(([v, l]) =>
        h("option", { value: v, selected: v === "48" || null }, l)))),
    h("div", { class: "actions" }, h("button", { class: "btn primary", type: "submit" }, "Create draft"),
      h("button", { class: "btn", type: "button", onclick: closeDrawer }, "Cancel")));
  $("#drawer-body").replaceChildren(form);
  $("#drawer").hidden = false;
  $("#f-theme").focus();
}

async function renderReview(main) {
  const r = await api("/api/items?state=AWAITING_APPROVAL,EXPIRED&limit=50");
  const head = h("div", { class: "toolbar" }, h("h1", {}, "Review"),
    h("span", { class: "muted" }, "Watch the preview, then approve to publish. Drafts expire 6 hours after they're written; expired ones can still be approved."),
    h("div", { class: "spacer" }), h("button", { class: "btn", onclick: openThemeForm }, "Create from theme"),
    h("button", { class: "btn", onclick: discoverNow }, "Find trends now"));
  if (!r.items.length) {
    main.replaceChildren(head, h("div", { class: "card empty" }, "Nothing waiting for review. New drafts arrive after each discovery run."));
    return;
  }
  const details = await Promise.all(r.items.map((i) => api(`/api/items/${i.id}`)));
  main.replaceChildren(head, h("div", { class: "review-list" }, details.map(reviewCard)));
}

function reviewCard(d) {
  const it = d.item, sc = d.script?.script || {};
  const policy = d.policy || {};
  const trend = d.trend || {};
  const card = h("div", { class: "card review" });
  const actions = h("div", { class: "actions" },
    h("button", { class: "btn primary", onclick: () => act(it.id, "approve", null, "Approved: publishing in a few minutes") }, "Approve"),
    h("button", { class: "btn", onclick: () => openItem(it.id, true) }, "Edit"),
    h("button", { class: "btn danger", onclick: () => { const reason = prompt("Why reject? (kept in the audit log)"); if (reason !== null) act(it.id, "reject", { reason }, "Rejected"); } }, "Reject"),
    h("button", { class: "btn", onclick: () => openItem(it.id) }, "Details"));
  card.append(
    d.has_video
      ? h("div", { class: "poster" }, h("video", { src: `/api/items/${it.id}/video`, controls: true, preload: "metadata", poster: `/api/items/${it.id}/thumb` }))
      : h("div", { class: "poster" }, h("div", {}, h("div", { style: "font-weight:700;font-size:15px;color:var(--text-primary)" }, sc.hook_text || ""), h("div", { style: "margin-top:8px" }, "Preview video is being made. Refresh in a few minutes."))),
    h("div", {},
      h("h2", {}, sc.title || it.title),
      h("div", { class: "meta" }, statePill(it.state), pill(it.category || "Uncategorised", ""),
        it.state === "AWAITING_APPROVAL" ? pill(`expires ${ago(it.expires_at)}`, "warning") : null),
      h("div", { class: "secondary" }, signalText(trend)),
      (policy.reasons || []).length ? h("div", { class: "reasons" }, h("b", {}, "Why it needs review: "), policy.reasons.join("; ")) : null,
      h("ol", { class: "scenes" }, h("li", {}, h("b", {}, "Hook: "), sc.hook_text || ""),
        (sc.scenes || []).map((s) => h("li", {}, h("b", {}, `${s.on_screen_text}: `), s.narration)),
        h("li", {}, h("b", {}, "Close: "), sc.call_to_action || "")),
      (sc.sources || []).length ? h("div", { class: "muted" }, "Sources: ", (sc.sources || []).map((u, i) => [i ? ", " : "", h("a", { href: u, target: "_blank", rel: "noopener noreferrer" }, new URL(u).hostname)])) : h("div", { class: "muted" }, "No sources cited."),
      actions));
  return card;
}

function signalText(t) {
  if (t.origin === "theme") return `Your theme: “${t.theme || ""}” · story: “${t.title || ""}”`;
  if (t.origin === "news") return `News story (${t.channel_title || "news"}): “${t.title || ""}” · ${Math.floor(t.velocity_score || 0)} outlets`;
  return `Trending video: “${t.title || ""}” · ${compact.format(t.view_count || 0)} views`;
}

async function act(id, action, body, done) {
  try {
    await api(`/api/items/${id}/${action}`, { method: "POST", body: body || {} });
    toast(done);
    closeDrawer();
    render();
    refreshChrome();
  } catch (e) { toast(`Couldn't ${action}: ${e.message}`); }
}

// ----- posts -----

async function renderPosts(main) {
  const f = state.filter;
  const params = new URLSearchParams({ limit: "500" });
  if (f.state) params.set("state", f.state);
  if (f.q) params.set("q", f.q);
  if (f.platform) params.set("platform", f.platform);
  const r = await api(`/api/items?${params}`);
  const stateSel = h("select", { "aria-label": "State", onchange: (e) => { f.state = e.target.value; render(); } },
    h("option", { value: "" }, "All states"), Object.entries(STATE_INFO).map(([k, [label]]) => h("option", { value: k, selected: f.state === k }, label)));
  const platSel = h("select", { "aria-label": "Platform", onchange: (e) => { f.platform = e.target.value; render(); } },
    h("option", { value: "" }, "Any platform"), h("option", { value: "YOUTUBE", selected: f.platform === "YOUTUBE" }, "Live on YouTube"),
    h("option", { value: "INSTAGRAM", selected: f.platform === "INSTAGRAM" }, "Live on Instagram"));
  const search = h("input", { type: "search", placeholder: "Search titles", value: f.q, "aria-label": "Search",
    onkeydown: (e) => { if (e.key === "Enter") { f.q = e.target.value; render(); } } });
  const head = h("div", { class: "toolbar" }, h("h1", {}, "All posts"), h("div", { class: "spacer" }), search, stateSel, platSel);
  if (!r.items.length) { main.replaceChildren(head, h("div", { class: "card empty" }, "No posts match.")); return; }
  const rows = r.items.map((i) => {
    const views = Object.values(i.platforms).reduce((a, p) => a + (p.views || 0), 0);
    const likes = Object.values(i.platforms).reduce((a, p) => a + (p.likes || 0), 0);
    return h("tr", { class: "click", onclick: () => openItem(i.id) },
      h("td", { class: "title-cell" }, h("div", { class: "t" }, i.title), h("div", { class: "muted" }, `${i.category} · created ${when(i.created_at)}`)),
      h("td", {}, statePill(i.state)), h("td", {}, pubPills(i.platforms)),
      h("td", { class: "num" }, nf.format(views)), h("td", { class: "num" }, nf.format(likes)),
      h("td", { class: "muted" }, i.error ? i.error.slice(0, 80) : ""));
  });
  main.replaceChildren(head, h("div", { class: "card table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Post"), h("th", {}, "State"), h("th", {}, "Platforms"), h("th", { class: "num" }, "Views"), h("th", { class: "num" }, "Likes"), h("th", {}, "Note"))),
    h("tbody", {}, rows)), h("div", { class: "muted", style: "margin-top:8px" }, `${r.total} item(s)`)));
}

// ----- item drawer -----

async function openItem(id, editing = false) {
  const body = $("#drawer-body");
  body.replaceChildren(h("div", { class: "empty" }, "Loading…"));
  $("#drawer").hidden = false;
  try {
    const d = await api(`/api/items/${id}`);
    body.replaceChildren(editing && d.can_edit ? editForm(d) : itemView(d));
  } catch (e) { body.replaceChildren(h("div", { class: "empty" }, e.message)); }
}
function closeDrawer() { $("#drawer").hidden = true; }

function itemView(d) {
  const it = d.item, sc = d.script?.script || {}, admin = state.me?.admin;
  const media = d.has_video
    ? h("div", { class: "poster" }, h("video", { src: `/api/items/${it.id}/video`, controls: true, preload: "metadata", poster: `/api/items/${it.id}/thumb` }))
    : h("div", { class: "poster" }, ["AWAITING_APPROVAL", "EXPIRED"].includes(it.state) ? "Preview video is being made. Refresh in a few minutes." : "No video yet");
  const actions = h("div", { class: "actions" });
  if (["AWAITING_APPROVAL", "EXPIRED"].includes(it.state)) {
    actions.append(h("button", { class: "btn primary", onclick: () => act(it.id, "approve", null, "Approved") }, "Approve"),
      h("button", { class: "btn", onclick: () => openItem(it.id, true) }, "Edit script"),
      h("button", { class: "btn danger", onclick: () => { const reason = prompt("Why reject?"); if (reason !== null) act(it.id, "reject", { reason }, "Rejected"); } }, "Reject"));
  }
  if (it.state === "REJECTED") actions.append(h("button", { class: "btn", onclick: () => act(it.id, "reopen", null, "Back in review") }, "Reopen for review"));
  if (admin && ["FAILED", "PARTIALLY_PUBLISHED"].includes(it.state)) {
    for (const [p, v] of Object.entries(it.platforms)) if (v.state === "FAILED") actions.append(h("button", { class: "btn", onclick: () => act(it.id, "retry", { platform: p }, `Retrying ${PLATFORM[p]}`) }, `Retry ${PLATFORM[p]}`));
    if (it.state === "FAILED" && !Object.values(it.platforms).some((v) => v.state === "FAILED")) actions.append(h("button", { class: "btn", onclick: () => act(it.id, "retry", {}, "Rendering again") }, "Render again"));
  }
  if (admin) for (const [p, v] of Object.entries(it.platforms)) if (v.state === "LIVE") actions.append(h("button", { class: "btn danger", onclick: () => {
    const reason = prompt(`Unpublish from ${PLATFORM[p]}? Give a reason (audit log).${p === "INSTAGRAM" ? " Instagram Reels must be removed in the app; this records it." : " The YouTube video is set to private."}`);
    if (reason) act(it.id, "unpublish", { platform: p, reason }, "Unpublish requested");
  } }, `Unpublish ${PLATFORM[p]}`));

  const pubRows = Object.entries(it.platforms).map(([p, v]) => h("tr", {},
    h("td", {}, PLATFORM[p]), h("td", {}, pill(...(PUB_INFO[v.state] || [v.state, ""]))),
    h("td", {}, v.url ? h("a", { href: v.url, target: "_blank", rel: "noopener" }, "Open") : ""),
    h("td", { class: "num" }, nf.format(v.views)), h("td", { class: "num" }, nf.format(v.likes)), h("td", { class: "muted" }, v.error || "")));

  return h("div", {},
    h("h1", { id: "drawer-title" }, sc.title || it.title),
    h("div", { class: "meta", style: "display:flex;gap:8px;margin-top:8px;flex-wrap:wrap" }, statePill(it.state), pill(it.category || "", ""), it.error ? pill(it.error.slice(0, 90), "serious") : null),
    actions,
    h("div", { class: "detail-grid" }, media, h("div", {},
      h("dl", { class: "kv" },
        h("dt", {}, "Created"), h("dd", {}, when(it.created_at)),
        h("dt", {}, "Approved by"), h("dd", {}, it.approved_by || "—"),
        h("dt", {}, "Signal"), h("dd", {}, signalText(d.trend || {})),
        h("dt", {}, "Policy"), h("dd", {}, d.policy ? `${d.policy.outcome}: ${(d.policy.reasons || []).join("; ")}` : "—"),
        h("dt", {}, "Video"), h("dd", {}, d.render ? `${d.render.seconds}s · ${(d.render.size / 1e6).toFixed(1)} MB · QA ${d.qa?.passed ? "passed" : (d.qa ? "failed: " + d.qa.problems.join("; ") : "pending")}` : "not rendered"),
        h("dt", {}, "Reference"), h("dd", {}, it.ref || "")),
      h("div", { class: "section" }, h("h3", {}, "Publications"), h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Platform"), h("th", {}, "State"), h("th", {}, "Link"), h("th", { class: "num" }, "Views"), h("th", { class: "num" }, "Likes"), h("th", {}, "Note"))),
        h("tbody", {}, pubRows.length ? pubRows : h("tr", {}, h("td", { colspan: 6, class: "muted" }, "Not published yet")))))))),
    h("div", { class: "section" }, h("h3", {}, "Script"),
      h("ol", { class: "scenes" }, h("li", {}, h("b", {}, "Hook: "), sc.hook_text || ""), (sc.scenes || []).map((s) => h("li", {}, h("b", {}, `${s.on_screen_text}: `), s.narration)), h("li", {}, h("b", {}, "Close: "), sc.call_to_action || "")),
      h("div", { class: "secondary" }, h("b", {}, "YouTube description: "), sc.description || ""),
      h("div", { class: "secondary", style: "margin-top:6px" }, h("b", {}, "Instagram caption: "), sc.caption || "", " ", (sc.hashtags || []).map((t) => `#${t}`).join(" ")),
      h("div", { class: "muted", style: "margin-top:6px" }, `Written by ${d.script?.model || "?"} · version ${d.script_versions}`)),
    h("div", { class: "section" }, h("h3", {}, "History"), h("ul", { class: "timeline" },
      d.audit.slice().reverse().map((e) => h("li", {}, h("span", { class: "muted" }, when(e.at)), h("span", {}, `${e.action} · ${e.actor}`))))),
  );
}

function editForm(d) {
  const sc = d.script?.script || {};
  const field = (label, name, value, rows = 0, max = 0) => h("div", { class: "field" }, h("label", { for: `f-${name}` }, label),
    rows ? h("textarea", { id: `f-${name}`, name, rows, maxlength: max || null }, value || "") : h("input", { type: "text", id: `f-${name}`, name, value: value || "", maxlength: max || null }));
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const scenes = (sc.scenes || []).map((s, i) => ({ ...s, narration: fd.get(`scene-${i}`), on_screen_text: fd.get(`caption-${i}`) }));
    const body = { title: fd.get("title"), hook_text: fd.get("hook_text"), call_to_action: fd.get("call_to_action"),
      description: fd.get("description"), caption: fd.get("caption"), scenes };
    try {
      await api(`/api/items/${d.item.id}/edit`, { method: "POST", body });
      toast("Saved as a new script version");
      openItem(d.item.id);
      render();
    } catch (err) { toast(`Not saved: ${err.message}`); }
  } },
    h("h1", { id: "drawer-title" }, "Edit script"),
    h("p", { class: "muted" }, "Edits are saved as a new version and stay in review until approved. Narration must stay between 60 and 175 words."),
    field("Title (YouTube adds #Shorts)", "title", sc.title, 0, 95),
    field("Hook (first 3 seconds)", "hook_text", sc.hook_text, 2, 120),
    (sc.scenes || []).map((s, i) => h("div", { class: "card", style: "margin-bottom:10px" },
      field(`Scene ${i + 1} caption`, `caption-${i}`, s.on_screen_text, 0, 60),
      field(`Scene ${i + 1} narration`, `scene-${i}`, s.narration, 3, 320))),
    field("Closing line", "call_to_action", sc.call_to_action, 2, 120),
    field("YouTube description", "description", sc.description, 4, 4500),
    field("Instagram caption", "caption", sc.caption, 3, 2000),
    h("div", { class: "actions" }, h("button", { class: "btn primary", type: "submit" }, "Save version"), h("button", { class: "btn", type: "button", onclick: () => openItem(d.item.id) }, "Cancel")));
  return form;
}

// ----- health and settings -----

async function renderHealth(main) {
  const hl = await api("/api/health");
  state.health = hl;
  const admin = hl.admin;
  const q = hl.quota, used = q.used / q.limit;
  const quota = h("div", { class: "card" }, h("h2", {}, "YouTube API quota today"),
    h("div", { class: "tile" }, h("div", { class: "value" }, `${nf.format(q.used)} / ${nf.format(q.limit)}`),
      h("div", { class: "sub" }, `Uploads pause at ${q.stop_pct}% and resume after the reset at 13:30 IST. An upload costs about 1,600 units.`)),
    h("div", { class: `meter ${used * 100 >= q.stop_pct ? "hot" : ""}` }, h("div", { style: `width:${Math.min(100, used * 100)}%` })));
  const sp = hl.spend;
  const spend = h("div", { class: "card" }, h("h2", {}, "Claude spend today"),
    h("div", { class: "tile" }, h("div", { class: "value" }, `₹${sp.inr.toFixed(2)}`), h("div", { class: "sub" }, `cap ₹${sp.cap_inr} a day · ${nf.format(sp.tokens)} tokens · model ${hl.settings.model}`)),
    h("div", { class: "meter" }, h("div", { style: `width:${Math.min(100, (sp.inr / sp.cap_inr) * 100)}%` })));

  const creds = h("div", { class: "card" }, h("h2", {}, "Platform sign-ins"),
    h("table", {}, h("tbody", {}, Object.entries(hl.credentials).map(([p, s]) => h("tr", {},
      h("td", {}, PLATFORM[p]), h("td", {}, s === "REVOKED" ? pill("Failed: publishing stopped", "critical") : pill("OK", "good")),
      h("td", { class: "num" }, s === "REVOKED" && admin ? h("button", { class: "btn", onclick: () => saveSettings({ clear_credential: p }, `${PLATFORM[p]} marked OK; publishing resumes`) }, "I've fixed it") : ""))))),
    h("p", { class: "muted" }, "If a sign-in fails, renew the token (README, runbook R1), then press “I've fixed it”."));

  const kill = h("label", { class: "switch" }, h("input", { type: "checkbox", checked: hl.kill_switch, disabled: !admin,
    onchange: (e) => saveSettings({ kill_switch: e.target.checked }, e.target.checked ? "Kill switch ON" : "Kill switch off") }), "Kill switch: stop all publishing");
  const privacy = h("select", { disabled: !admin, onchange: (e) => saveSettings({ yt_privacy_status: e.target.value }, "YouTube privacy saved") },
    ["private", "unlisted", "public"].map((p) => h("option", { value: p, selected: hl.yt_privacy_status === p }, p)));
  const autoBoxes = Object.entries(hl.categories).map(([id, name]) => h("label", { class: "switch" },
    h("input", { type: "checkbox", value: id, checked: hl.auto_approve_categories.includes(id), disabled: !admin, class: "auto-cat" }), name));
  const settings = h("div", { class: "card" }, h("h2", {}, "Publishing controls"), kill,
    h("div", { class: "field", style: "margin-top:12px" }, h("label", {}, "New YouTube uploads are"), privacy),
    h("div", { class: "field" }, h("label", {}, "Auto-approve these categories (only after the review period in the LLD, Section 11.3)"), h("div", { class: "checks" }, autoBoxes)),
    admin ? h("button", { class: "btn", onclick: () => saveSettings({ auto_approve_categories: [...document.querySelectorAll(".auto-cat:checked")].map((c) => c.value) }, "Auto-approve saved") }, "Save auto-approve") : h("p", { class: "muted" }, "Only admins can change these."));

  const queues = h("div", { class: "card" }, h("h2", {}, "Queues"), h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Queue"), h("th", { class: "num" }, "Waiting"), h("th", { class: "num" }, "Dead-lettered"), h("th", {}))),
    h("tbody", {}, Object.entries(hl.queues).map(([name, v]) => h("tr", {}, h("td", {}, name), h("td", { class: "num" }, String(v.depth)),
      h("td", { class: "num" }, v.dead ? pill(String(v.dead), "critical") : "0"),
      h("td", { class: "num" }, v.dead && admin ? h("button", { class: "btn", onclick: async () => { const r = await api("/api/dlq/replay", { method: "POST", body: { queue: name } }); toast(`Replayed ${r.replayed}`); render(); } }, "Replay") : ""))))));

  const runs = h("div", { class: "card" }, h("h2", {}, "Recent discovery runs"), hl.runs.length ? h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Run"), h("th", {}, "Status"), h("th", {}, "Started"), h("th", { class: "num" }, "Trends"), h("th", { class: "num" }, "Drafts"), h("th", { class: "num" }, "Skipped"))),
    h("tbody", {}, hl.runs.map((r) => h("tr", {}, h("td", {}, r.RowKey), h("td", {}, pill(r.status, r.status === "SUCCEEDED" ? "good" : r.status === "FAILED" ? "critical" : "info")),
      h("td", {}, when(r.started_at)), h("td", { class: "num" }, String(r.found ?? "")), h("td", { class: "num" }, String(r.created ?? "")), h("td", { class: "num" }, String(r.skipped ?? ""))))))) : h("div", { class: "empty" }, "No runs yet."));

  main.replaceChildren(h("div", { class: "toolbar" }, h("h1", {}, "Health & settings")),
    h("div", { class: "grid kpis" }, quota, spend, creds), h("div", { class: "grid two" }, settings, queues), h("div", { style: "margin-top:16px" }, runs));
}

async function saveSettings(body, done) {
  try { await api("/api/settings", { method: "POST", body }); toast(done); refreshChrome(); render(); }
  catch (e) { toast(`Not saved: ${e.message}`); }
}

// ----- boot -----

document.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeDrawer(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));

(async () => {
  try { state.me = await api("/api/me"); $("#who").textContent = `${state.me.name} · ${state.me.channel} · @${state.me.instagram}`; }
  catch (e) { $("#main").replaceChildren(h("div", { class: "card empty" }, `You're not allowed to use this console (${e.message}).`)); return; }
  let saved = "dashboard";
  try { saved = localStorage.getItem("ytp.view") || saved; } catch {}
  setView(["dashboard", "review", "posts", "health"].includes(saved) ? saved : "dashboard");
  refreshChrome();
  setInterval(refreshChrome, 60000);
})();
