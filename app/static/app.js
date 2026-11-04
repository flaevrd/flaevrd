/* FLAEVRD Network — no-build single-page UI. All external text is escaped via esc()/url(). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const drawer = $("#drawer");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const url = (u) => (/^https?:\/\//i.test(u || "") ? esc(u) : "#");
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
};
const ROLE = {
  main_artist: "Main artist", featured_artist: "Featured", producer: "Producer", co_producer: "Co-producer",
  additional_producer: "Add. producer", executive_producer: "Exec. producer", writer: "Songwriter", composer: "Composer",
  lyricist: "Lyricist", recording_engineer: "Recording eng.", mixing_engineer: "Mixing eng.", mastering_engineer: "Mastering eng.",
  engineer: "Engineer", assistant_engineer: "Asst. engineer", programming: "Programming", arranger: "Arranger", vocals: "Vocals",
  instrument: "Musician", remixer: "Remixer", a_and_r: "A&R", management: "Management", label: "Label", publisher: "Publisher",
  studio: "Studio", other: "Other",
};
const GROUP_OF = {
  main_artist: "artist", featured_artist: "artist", vocals: "artist", producer: "producer", co_producer: "producer",
  additional_producer: "producer", executive_producer: "producer", programming: "producer", remixer: "producer",
  writer: "writer", composer: "writer", lyricist: "writer", recording_engineer: "engineer", mixing_engineer: "engineer",
  mastering_engineer: "engineer", engineer: "engineer", assistant_engineer: "engineer", instrument: "musician",
  arranger: "musician", a_and_r: "business", management: "business", label: "business", publisher: "business", studio: "studio",
};
const GROUP_NAME = { artist: "Artists", producer: "Producers", writer: "Songwriters", engineer: "Engineers", musician: "Musicians", business: "Labels & business", studio: "Studios", other: "Other" };
const STATUS_TEXT = { verified: "Verified", publicly_listed: "Publicly listed", unverified: "Unverified", unavailable: "Unavailable", multi_source: "Multi-source", single_source: "Single source" };
const CRM = ["discovered", "to_research", "potential_contact", "contacted", "in_conversation", "collaborated"];
const CRM_TEXT = { discovered: "Discovered", to_research: "To research", potential_contact: "Potential contact", contacted: "Contacted", in_conversation: "In conversation", collaborated: "Collaborated" };
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const roleChip = (r, n) => `<span class="chip g-${GROUP_OF[r] || "other"}">${esc(ROLE[r] || r)}${n > 1 ? ` ×${n}` : ""}</span>`;
const roleChips = (roles) => Object.entries(roles || {}).sort((a, b) => b[1] - a[1]).map(([r, n]) => roleChip(r, n)).join("");
const status = (s) => `<span class="status ${esc(s)}">${esc(STATUS_TEXT[s] || s)}</span>`;
const provLinks = (sources) => (sources || []).map((s) => `<a href="${url(s.url)}" target="_blank" rel="noopener">${esc(s.provider)}</a>`).join(" · ");
const fmtN = (n) => Number(n).toLocaleString();
const day = (iso) => (iso || "").slice(0, 10);
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch {} } };

let poll = null;
let cy = null;
let current = { sid: null, data: null, tab: "overview" };

/* ---------------- router ---------------- */
async function route() {
  clearInterval(poll);
  drawer.classList.add("hidden");
  const h = location.hash || "#/";
  const [_, page, id, sub] = h.split("/");
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active",
    (a.dataset.nav === "search" && (page === "" || (page === "r" && !["network", "discover"].includes(sub)))) || a.dataset.nav === page ||
    (a.dataset.nav === "explore" && sub === "network") || (a.dataset.nav === "discover" && sub === "discover") ||
    (a.dataset.nav === "people" && page === "p")));
  try {
    if (!page) return renderSearch();
    if (page === "r") return renderResearch(+id, sub || "overview");
    if (page === "p") return renderProfile(+id);
    if (page === "people") return renderPeople();
    if (page === "history") return renderHistory();
    if (page === "explore" || page === "discover") {
      const last = store.get("lastSession");
      if (last) { location.hash = `#/r/${last}/${page === "explore" ? "network" : "discover"}`; return; }
      view.innerHTML = `<div class="hero"><h1>No research <em>yet</em></h1><p class="lede">Run a search first; the ${page === "explore" ? "network explorer" : "discovery view"} opens on your latest research.</p><div><a class="btn primary" href="#/">Start a search</a></div></div>`;
      return;
    }
  } catch (e) {
    view.innerHTML = `<div class="warn">Error: ${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

/* ---------------- search ---------------- */
function renderSearch() {
  const kind = store.get("kind") || "auto";
  const depth = store.get("depth") || "2";
  view.innerHTML = `
  <section class="hero">
    <div class="eyebrow">Music industry intelligence</div>
    <h1>Search one name.<br><em>Discover the network.</em></h1>
    <p class="lede">Artist, producer, engineer, songwriter, song or album. Every credit, contact and connection is traced to its source.</p>
    <form class="searchbox" id="sf"><input id="q" autocomplete="off" placeholder="e.g. an artist, a producer, “Song – Artist”, an album…" autofocus><button class="btn primary">Search</button></form>
    <div class="search-opts">
      <span>Type</span><div class="seg" id="kind">${["auto", "artist", "song", "album"].map((k) => `<button type="button" data-k="${k}" class="${k === kind ? "on" : ""}">${{ auto: "Auto", artist: "Person / artist", song: "Song", album: "Album" }[k]}</button>`).join("")}</div>
      <span>Depth</span><div class="seg" id="depth">${["1", "2"].map((d) => `<button type="button" data-d="${d}" class="${d === depth ? "on" : ""}">${d === "1" ? "Direct only" : "+ 2nd degree"}</button>`).join("")}</div>
    </div>
    <div id="sugg" class="suggest"></div>
  </section>`;
  $("#kind").onclick = (e) => { const k = e.target.dataset.k; if (!k) return; store.set("kind", k); [...$("#kind").children].forEach((b) => b.classList.toggle("on", b.dataset.k === k)); };
  $("#depth").onclick = (e) => { const d = e.target.dataset.d; if (!d) return; store.set("depth", d); [...$("#depth").children].forEach((b) => b.classList.toggle("on", b.dataset.d === d)); };
  $("#sf").onsubmit = async (e) => {
    e.preventDefault();
    const q = $("#q").value.trim();
    if (q.length < 2) return;
    const box = $("#sugg");
    box.innerHTML = `<div class="muted"><span class="spinner"></span>Identifying “${esc(q)}” across sources…</div>`;
    try {
      const res = await api(`/api/suggest?q=${encodeURIComponent(q)}&kind=${store.get("kind") || "auto"}`);
      renderSuggestions(res);
    } catch (err) { box.innerHTML = `<div class="warn">Search failed: ${esc(err.message)}</div>`; }
  };
}

function renderSuggestions(res) {
  const box = $("#sugg");
  if (!res.candidates.length) {
    box.innerHTML = `<div class="warn">No matches found in the connected sources${res.errors.length ? ` (unreachable: ${esc(res.errors.join(", "))})` : ""}.</div>`;
    return;
  }
  const groups = { artist: [], song: [], album: [] };
  res.candidates.forEach((c, i) => groups[c.kind]?.push([c, i]));
  const order = res.detected ? [res.detected, ...Object.keys(groups).filter((k) => k !== res.detected)] : Object.keys(groups);
  const label = { artist: "People & artists", song: "Songs", album: "Albums & EPs" };
  box.innerHTML = (res.detected ? `<div class="muted">Best match looks like a <b>${esc(res.detected === "artist" ? "person/artist" : res.detected)}</b>. Pick the exact entity to research — homonyms are listed separately on purpose.</div>` : `<div class="muted">Pick the exact entity to research.</div>`) +
    (res.errors.length ? `<div class="warn">Not reachable right now: ${esc(res.errors.join(", "))}</div>` : "") +
    order.filter((k) => groups[k].length).map((k) => `<div><h4>${label[k]}</h4>${groups[k].map(([c, i]) => `
      <div class="cand" data-i="${i}">
        <div><div class="t">${esc(c.title)}</div><div class="s">${esc(c.subtitle)}</div></div>
        <span class="kind prov">${esc(c.provider)}</span>
        ${c.url ? `<a class="prov" href="${url(c.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">source ↗</a>` : ""}
      </div>`).join("")}</div>`).join("");
  box.querySelectorAll(".cand").forEach((el) => el.onclick = async () => {
    const c = res.candidates[+el.dataset.i];
    const body = { query: res.query, kind: c.kind, provider: c.provider, external_id: c.external_id, depth: +(store.get("depth") || 2) };
    const r = await api("/api/research", { method: "POST", body: JSON.stringify(body) });
    location.hash = `#/r/${r.session_id}`;
  });
}

/* ---------------- research ---------------- */
async function renderResearch(sid, tab) {
  store.set("lastSession", sid);
  const load = async () => {
    const d = await api(`/api/research/${sid}`);
    if (d.status === "queued" || d.status === "running") {
      view.innerHTML = `<div class="rhead"><div><div class="eyebrow">Researching</div><h1>${esc(d.query)}</h1></div></div>
        <p class="muted"><span class="spinner"></span>Collecting credits and following connections. Sources are rate-limited (MusicBrainz: 1 request/second), so a deep search can take a few minutes. You can leave this page; it keeps running.</p>
        <div class="log">${d.log.map((l) => `<div class="${esc(l.level)}">${esc(day(l.t) === day(new Date().toISOString()) ? l.t.slice(11, 19) : l.t)} · ${esc(l.msg)}</div>`).join("")}</div>`;
      const lg = $(".log"); if (lg) lg.scrollTop = lg.scrollHeight;
      return false;
    }
    if (d.status === "failed") {
      view.innerHTML = `<div class="rhead"><div><div class="eyebrow">Research failed</div><h1>${esc(d.query)}</h1></div></div>
        <div class="log">${d.log.map((l) => `<div class="${esc(l.level)}">${esc(l.msg)}</div>`).join("")}</div>`;
      return true;
    }
    current = { sid, data: d, tab };
    drawResearch();
    return true;
  };
  if (!(await load())) poll = setInterval(async () => { if (await load()) clearInterval(poll); }, 2500);
}

function drawResearch() {
  const { data: d, tab, sid } = current;
  const r = d.result;
  const c = r.counts;
  const center = r.center ? `<div class="muted">${esc(r.center.kind === "song" ? "Song" : "Album")}: <b>${esc(r.center.title)}</b> — ${esc(r.center.artist)}</div>` : "";
  view.innerHTML = `
  <div class="rhead">
    <div><div class="eyebrow">${d.saved ? `Saved research · ${esc(d.title)}` : `Research · ${esc(day(d.created_at))}`}</div>
      <h1>${r.root_entity_id ? `<a href="#/p/${r.root_entity_id}">${esc(r.center ? r.center.title : r.root_name)}</a>` : esc(d.query)}</h1>${center}</div>
    <div class="stats">
      <div class="stat"><b>${c.tracks}</b><span>Tracks analysed</span></div>
      <div class="stat"><b>${c.direct}</b><span>Direct connections</span></div>
      <div class="stat"><b>${c.second}</b><span>2nd degree</span></div>
      <div class="stat"><b>${c.sources}</b><span>Sources</span></div>
    </div>
    <div style="display:flex;gap:8px">${d.saved ? "" : `<button class="btn small" id="save-r">Save as research project</button>`}</div>
  </div>
  <div class="tabs">${[["overview", "Overview"], ["people", "People & Credits"], ["network", "Network Graph"], ["discover", "Similar Network"], ["contacts", "Contact Information"], ["sources", "Sources"]].map(([k, l]) => `<button data-t="${k}" class="${k === tab ? "on" : ""}">${l}</button>`).join("")}</div>
  <div id="tab"></div>`;
  view.querySelector(".tabs").onclick = (e) => { const t = e.target.dataset.t; if (t) location.hash = `#/r/${sid}/${t}`; };
  const sv = $("#save-r");
  if (sv) sv.onclick = async () => {
    const title = prompt("Name this research project", r.root_name ? `${r.root_name} network` : d.query);
    if (!title) return;
    await api(`/api/research/${sid}/save`, { method: "POST", body: JSON.stringify({ title }) });
    route();
  };
  ({ overview: tabOverview, people: tabPeople, network: tabNetwork, discover: tabDiscover, contacts: tabContacts, sources: tabSources }[tab] || tabOverview)(r);
}

function personRow(p, opts = {}) {
  const colorVar = `var(--${p.group || "other"})`;
  return `<div class="person-row">
    <div class="score">${p.strength ?? ""}<small>${esc(p.tier || (p.degree === 2 ? "2nd" : ""))}</small><div class="bar"><i style="width:${p.strength || 0}%"></i></div></div>
    <div>
      <a class="name" href="#/p/${p.id}"><span class="dot" style="background:${colorVar};margin:0 6px 0 0;width:8px;height:8px"></span>${esc(p.name)}</a>
      ${p.type && p.type !== "person" && p.type !== "unknown" ? `<span class="chip">${esc(p.type)}</span>` : ""}
      ${p.relationship ? `<span class="chip">${esc(p.relationship)}</span>` : ""}
      <div class="roles">${roleChips(p.roles)}</div>
      <div class="why">${esc(p.summary || p.why || "")}</div>
      ${p.why && p.summary ? `<div class="why faint">${esc(p.why)}</div>` : ""}
      ${p.evidence ? `<details class="ev"><summary>Evidence: ${p.evidence.length} track${p.evidence.length !== 1 ? "s" : ""}</summary><ul>${p.evidence.map((e) => `
        <li><b>${esc(e.title)}</b> <span class="muted">${esc(e.artist || "")}${e.year ? ` · ${e.year}` : ""}${e.release ? ` · ${esc(e.release)}` : ""}</span><br>
        <span class="prov">confirmed by ${esc((e.confirmed_by || []).join(" + "))} · ${provLinks(e.sources)}</span></li>`).join("")}</ul></details>` : ""}
      ${p.paths ? `<details class="ev"><summary>${p.path_count} connection path${p.path_count !== 1 ? "s" : ""}</summary><ul>${p.paths.map((x) => `
        <li><span class="chip">${esc(x.type)}</span> via <a class="name" href="#/p/${x.via_id}">${esc(x.via_name)}</a><br><span class="muted">${esc(x.explanation)}</span><br>
        <span class="prov">${x.evidence.map((e) => provLinks(e.sources)).filter(Boolean).slice(0, 3).join(" · ")}</span></li>`).join("")}</ul></details>` : ""}
    </div>
    <div>${opts.actions !== false ? `<button class="ghost small" data-research="${p.id}" title="Start a new search from this person">Research →</button>` : ""}</div>
  </div>`;
}

function wireResearchButtons(root) {
  root.querySelectorAll("[data-research]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await api(`/api/people/${b.dataset.research}/research`, { method: "POST" });
      location.hash = `#/r/${r.session_id}`;
    } catch (e) { b.textContent = "No ID to research"; }
  });
}

async function tabOverview(r) {
  const t = $("#tab");
  const top = r.direct.filter((p) => !["label", "studio"].includes(p.type)).slice(0, 8);
  t.innerHTML = `<div class="grid2">
    <div>
      <div class="panel" id="root-card"><h3>Who this is</h3><div class="muted"><span class="spinner"></span>Loading profile…</div></div>
      ${r.recurring.length ? `<div class="panel"><h3>Recurring collaborators across the album</h3>${r.recurring.slice(0, 12).map((x) => `
        <div class="person-row" style="grid-template-columns:44px 1fr"><div class="score">${x.tracks}<small>tracks</small></div>
        <div><a class="name" href="#/p/${x.id}">${esc(x.name)}</a><div class="roles">${roleChips(x.roles)}</div></div></div>`).join("")}</div>` : ""}
      <div class="panel"><h3>Strongest direct connections</h3>${top.map((p) => personRow(p)).join("") || `<div class="empty">No credited collaborators found in the connected sources.</div>`}</div>
    </div>
    <div>
      <div class="panel"><h3>Who to know next</h3>${(r.discover?.new_collaborators || []).slice(0, 6).map((x) => `
        <div style="padding:8px 0;border-top:1px solid var(--line)"><a class="name" href="#/p/${x.id}">${esc(x.name)}</a> <span class="chip g-${esc(x.group)}">${esc(x.group)}</span><div class="why">${esc(x.why)}</div></div>`).join("") || `<div class="empty">Second-degree discovery found nobody yet (try depth “+ 2nd degree”).</div>`}
        <p><a href="#/r/${r.session_id}/discover">All discovery categories →</a></p></div>
      <div class="panel"><h3>Data coverage</h3>
        <div class="mono">${Object.entries(r.providers_used).map(([k, v]) => `${esc(k)}: ${v} source records`).join("<br>") || "No sources"}</div>
        ${r.warnings.length ? `<details class="ev"><summary>${r.warnings.length} notes (configuration, unreachable sources, name-only matches)</summary><ul>${r.warnings.map((w) => `<li class="muted">${esc(w)}</li>`).join("")}</ul></details>` : ""}
      </div>
    </div></div>`;
  wireResearchButtons(t);
  if (r.root_entity_id) {
    const p = await api(`/api/people/${r.root_entity_id}`);
    $("#root-card").innerHTML = `<h3>Who this is</h3><h2><a class="name" href="#/p/${p.id}">${esc(p.name)}</a></h2>${profileSummary(p)}`;
  }
}

function factVal(p, ...fields) {
  for (const f of fields) if (p.facts[f]) return { field: f, ...p.facts[f] };
  return null;
}

function profileSummary(p) {
  const loc = factVal(p, "area", "residence", "formed_in", "begin_area", "born_in");
  const country = factVal(p, "country");
  const bio = factVal(p, "bio", "summary");
  const locLabel = { area: "Associated area", residence: "Residence", formed_in: "Formed in", begin_area: "Origin", born_in: "Born in" };
  return `<dl class="kv">
    <dt>Role</dt><dd>${esc(p.primary_role || "—")} ${p.roles.slice(1, 5).map((r) => roleChip(r.role, 1)).join(" ")}</dd>
    <dt>${esc(loc ? locLabel[loc.field] : "Based in")}</dt><dd>${loc ? loc.values.map((v) => `${esc(v.value)} <span class="prov">${provLinks(v.sources)}</span>`).join("; ") + (loc.discrepancy ? ` <span class="status unverified">Sources disagree</span>` : "") : `<span class="faint">Not found in connected sources</span>`}</dd>
    ${country ? `<dt>Country</dt><dd>${country.values.map((v) => `${esc(v.value)} <span class="prov">${provLinks(v.sources)}</span>`).join("; ")}${country.discrepancy ? ` <span class="status unverified">Sources disagree</span>` : ""}</dd>` : ""}
    ${p.facts.genre ? `<dt>Genres</dt><dd>${p.facts.genre.values.slice(0, 6).map((v) => esc(v.value)).join(", ")}</dd>` : ""}
    ${p.active_years ? `<dt>Credits</dt><dd>${p.credit_count} tracks in database · ${p.active_years[0]}–${p.active_years[1]}</dd>` : ""}
  </dl>
  ${bio ? `<p class="bio" style="margin-top:14px">${esc(bio.values[0].value.slice(0, 600))}${bio.values[0].value.length > 600 ? "…" : ""} <span class="prov">${provLinks(bio.values[0].sources)}</span></p>` : ""}`;
}

function tabPeople(r) {
  const t = $("#tab");
  const mode = store.get("peopleMode") || "people";
  const groups = {};
  r.direct.forEach((p) => (groups[p.group] = groups[p.group] || []).push(p));
  t.innerHTML = `<div style="display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap">
      <div class="seg" id="pm"><button data-m="people" class="${mode === "people" ? "on" : ""}">By person</button><button data-m="tracks" class="${mode === "tracks" ? "on" : ""}">By track</button><button data-m="second" class="${mode === "second" ? "on" : ""}">2nd degree (${r.second.length})</button></div>
      <input id="pf" placeholder="Filter by name…" style="max-width:260px">
    </div><div id="pl"></div>`;
  $("#pm").onclick = (e) => { if (e.target.dataset.m) { store.set("peopleMode", e.target.dataset.m); tabPeople(r); } };
  const draw = () => {
    const f = ($("#pf").value || "").toLowerCase();
    const m = store.get("peopleMode") || "people";
    if (m === "people") {
      $("#pl").innerHTML = ["producer", "engineer", "writer", "artist", "musician", "business", "studio", "other"].filter((g) => groups[g]).map((g) => {
        const items = groups[g].filter((p) => p.name.toLowerCase().includes(f));
        return items.length ? `<div class="panel"><h3>${GROUP_NAME[g]} · ${items.length}</h3>${items.map((p) => personRow(p)).join("")}</div>` : "";
      }).join("") || `<div class="empty">No people.</div>`;
    } else if (m === "second") {
      const items = r.second.filter((p) => p.name.toLowerCase().includes(f));
      $("#pl").innerHTML = `<div class="panel"><h3>Second-degree connections · ${items.length}</h3>${items.map((p) => personRow(p)).join("") || `<div class="empty">None found. Run with depth “+ 2nd degree”.</div>`}</div>`;
    } else {
      const items = r.tracks.filter((tr) => !f || tr.title.toLowerCase().includes(f) || tr.credits.some((c) => (c.name || "").toLowerCase().includes(f)));
      $("#pl").innerHTML = `<div class="panel"><h3>Tracks · ${items.length}</h3>${items.map((tr) => {
        const g = {};
        tr.credits.forEach((c) => (g[c.group] = g[c.group] || []).push(c));
        return `<div class="track"><h4>${esc(tr.title)}</h4><div class="meta">${esc(tr.artist || "")}${tr.release ? ` · ${esc(tr.release)}` : ""}${tr.date ? ` · ${esc(tr.date)}` : ""} · <span class="prov">${provLinks(tr.sources)}</span></div>
          <div class="credit-groups">${Object.entries(g).map(([k, cs]) => `<div class="cg"><b>${GROUP_NAME[k] || k}</b>${cs.map((c) => `<div><a class="name" style="font-weight:500" href="#/p/${c.entity_id}">${esc(c.name)}</a> <span class="faint">${esc(ROLE[c.role] || c.role)}</span> <span class="prov" title="${esc(c.providers.join(", "))}">${c.providers.length > 1 ? "✓✓" : esc(c.providers[0])}</span></div>`).join("")}</div>`).join("")}</div></div>`;
      }).join("")}</div>`;
    }
    wireResearchButtons($("#pl"));
  };
  $("#pf").oninput = draw;
  draw();
}

/* ---------------- network graph ---------------- */
function tabNetwork(r) {
  const t = $("#tab");
  if (!r.graph || !window.cytoscape) { t.innerHTML = `<div class="empty">${window.cytoscape ? "No graph for this research." : "Graph library failed to load (offline?)."}</div>`; return; }
  const g = r.graph;
  const groups = [...new Set(g.nodes.map((n) => n.group))];
  const locs = [...new Set(g.nodes.map((n) => n.location).filter(Boolean))].sort();
  const genres = [...new Set(g.nodes.flatMap((n) => n.genres || []))].sort();
  t.innerHTML = `<div class="graph-wrap">
    <div class="graph-side">
      <h3>Search in network</h3><input type="text" id="gs" placeholder="Name…">
      <h3>Layers</h3>
      <label><input type="checkbox" id="g2" ${g.nodes.some((n) => n.degree === 2) ? "" : "disabled"}> Show 2nd-degree</label>
      <h3>Roles</h3>${groups.map((gr) => `<label><input type="checkbox" class="gr" value="${esc(gr)}" checked><span class="legend-dot" style="background:var(--${esc(gr)})"></span>${esc(GROUP_NAME[gr] || gr)}</label>`).join("")}
      <h3>Min. strength <span id="msv" class="mono">0</span></h3><input type="range" id="ms" min="0" max="100" value="0" style="width:100%">
      <h3>Location</h3><select id="gl"><option value="">Any</option>${locs.map((l) => `<option>${esc(l)}</option>`).join("")}</select>
      <h3>Genre</h3><select id="gg"><option value="">Any</option>${genres.map((l) => `<option>${esc(l)}</option>`).join("")}</select>
      <p class="faint" style="font-size:12px;margin-top:16px">Solid edges: direct credits. Dashed: second-degree, through the person they connect via. Location/genre filters only apply to people whose profile has that data.</p>
    </div>
    <div id="cy"></div>
    <div class="graph-info" id="gi"><h3 style="font:500 10px var(--mono);letter-spacing:.12em;color:var(--muted);text-transform:uppercase;margin:0 0 8px">Selection</h3><div class="muted">Click a person or a connection to see why it exists.</div></div>
  </div>`;
  const color = (gr) => css(`--${gr}`) || css("--other");
  cy = cytoscape({
    container: $("#cy"),
    elements: [
      ...g.nodes.map((n) => ({ data: { ...n, id: String(n.id), size: n.degree === 0 ? 46 : 14 + (n.strength || 0) / 5, color: color(n.group) } })),
      ...g.edges.map((e, i) => ({ data: { ...e, id: `e${i}`, source: String(e.source), target: String(e.target), w: 0.5 + (e.strength || 0) / 30 } })),
    ],
    style: [
      { selector: "node", style: { "background-color": "data(color)", width: "data(size)", height: "data(size)", label: "data(label)", color: "#ece8de", "font-size": 10, "font-family": "Inter", "text-valign": "bottom", "text-margin-y": 4, "text-outline-width": 2, "text-outline-color": "#0d0d0b", "min-zoomed-font-size": 7 } },
      { selector: "node[degree = 0]", style: { "font-size": 14, "font-weight": 600, "border-width": 3, "border-color": "#e9b949" } },
      { selector: "node[degree = 2]", style: { opacity: 0.85, "border-width": 1, "border-color": "#3a3832" } },
      { selector: "node[type = 'label'], node[type = 'studio']", style: { shape: "round-rectangle" } },
      { selector: "edge", style: { width: "data(w)", "line-color": "#3a3832", "curve-style": "bezier", opacity: 0.8 } },
      { selector: "edge[kind = 'indirect']", style: { "line-style": "dashed", opacity: 0.5 } },
      { selector: ".hl", style: { "border-width": 3, "border-color": "#e9b949" } },
      { selector: "edge.hl", style: { "line-color": "#e9b949", opacity: 1 } },
      { selector: ".dim", style: { opacity: 0.12 } },
    ],
    layout: { name: "concentric", concentric: (n) => 3 - n.data("degree"), levelWidth: () => 1, minNodeSpacing: 14, animate: false },
    wheelSensitivity: 0.25, minZoom: 0.2, maxZoom: 1.6,
  });
  const apply = () => {
    const show2 = $("#g2").checked;
    const okG = new Set([...document.querySelectorAll(".gr:checked")].map((x) => x.value));
    const ms = +$("#ms").value; $("#msv").textContent = ms;
    const loc = $("#gl").value, gen = $("#gg").value;
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const d = n.data();
        const vis = d.degree === 0 || ((show2 || d.degree === 1) && okG.has(d.group) && (d.strength || 0) >= ms &&
          (!loc || d.location === loc) && (!gen || (d.genres || []).includes(gen)));
        n.style("display", vis ? "element" : "none");
      });
      cy.nodes().filter((n) => n.data("degree") === 2 && n.style("display") === "element").forEach((n) => {
        if (!n.incomers("node").some((p) => p.style("display") === "element")) n.style("display", "none");
      });
    });
    cy.layout({ name: "concentric", concentric: (n) => 3 - n.data("degree"), levelWidth: () => 1, minNodeSpacing: 14, animate: true, animationDuration: 300, eles: cy.elements(":visible"), fit: true, padding: 40 }).run();
  };
  ["g2", "ms", "gl", "gg"].forEach((id) => $("#" + id).addEventListener("input", apply));
  apply();
  document.querySelectorAll(".gr").forEach((x) => x.addEventListener("change", apply));
  $("#gs").oninput = (e) => {
    const q = e.target.value.toLowerCase();
    cy.elements().removeClass("hl dim");
    if (!q) return;
    const hit = cy.nodes().filter((n) => n.data("label").toLowerCase().includes(q));
    cy.elements().not(hit).addClass("dim"); hit.addClass("hl");
  };
  cy.on("tap", "node", (ev) => {
    const d = ev.target.data();
    cy.elements().removeClass("hl dim");
    const nb = ev.target.closedNeighborhood();
    cy.elements().not(nb).addClass("dim"); nb.addClass("hl");
    const p = [...r.direct, ...r.second].find((x) => String(x.id) === d.id);
    $("#gi").innerHTML = `<div class="eyebrow">${d.degree === 0 ? "Centre" : d.degree === 1 ? "Direct connection" : "Second degree"}</div>
      <h2 style="font:400 26px var(--serif);margin:4px 0 8px">${esc(d.label)}</h2>
      ${p ? `<div class="roles">${roleChips(p.roles)}</div><p class="why">${esc(p.summary || p.why || "")}</p>` : ""}
      ${d.location ? `<p class="muted">📍 ${esc(d.location)}</p>` : ""}
      ${p?.paths ? p.paths.map((x) => `<p class="why"><span class="chip">${esc(x.type)}</span> ${esc(x.explanation)}</p>`).join("") : ""}
      ${p?.evidence ? `<ul style="padding-left:16px">${p.evidence.slice(0, 6).map((e) => `<li>${esc(e.title)} <span class="faint">${e.year || ""}</span></li>`).join("")}</ul>` : ""}
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px"><a class="btn small" href="#/p/${esc(d.id)}">Open profile</a><button class="btn small" data-research="${esc(d.id)}">Research from here →</button></div>`;
    wireResearchButtons($("#gi"));
  });
  cy.on("tap", "edge", (ev) => {
    const d = ev.target.data();
    $("#gi").innerHTML = `<div class="eyebrow">${d.kind === "direct" ? "Direct relationship" : "Second-degree link"}</div><p><span class="chip">${esc(d.label)}</span> <span class="mono">strength ${d.strength}</span></p><p class="why">${esc(d.text)}</p>`;
  });
  cy.on("tap", (ev) => { if (ev.target === cy) cy.elements().removeClass("hl dim"); });
}

function tabDiscover(r) {
  const t = $("#tab");
  const d = r.discover || {};
  const card = (title, items, hint) => `<div class="panel"><h3>${title}</h3>${hint ? `<p class="faint" style="margin:0 0 8px;font-size:12px">${hint}</p>` : ""}${(items || []).map((x) => `
    <div style="padding:9px 0;border-top:1px solid var(--line)"><a class="name" href="#/p/${x.id}">${esc(x.name)}</a> <span class="chip g-${esc(x.group)}">${esc(x.group)}</span>
    <span class="mono faint" style="float:right">${x.score}</span><div class="why">${esc(x.why)}</div></div>`).join("") || `<div class="empty">Nothing in this category from the researched credits.</div>`}</div>`;
  t.innerHTML = `<p class="muted">${esc(d.basis || "")} Rankings use shared collaborators and credit evidence only — never follower counts.</p>
  <div class="cards">
    ${card("Similar network", d.similar, "Same role, overlapping collaborators. Score = collaborator overlap (cosine, 0–100).")}
    ${card("New collaborators to explore", d.new_collaborators, "Second-degree people, strongest connection path first.")}
    ${card("Emerging artists in this network", d.emerging_artists, "Earliest release found within the last 3 years.")}
    ${card("Frequently shared engineers", d.shared_engineers)}
    ${card("Connected songwriters", d.connected_songwriters)}
    ${card("Cross-network connections", d.cross_network, "People linked to several of the searched person's collaborators.")}
  </div>`;
}

function tabContacts(r) {
  const t = $("#tab");
  const byE = {};
  r.contacts.forEach((c) => (byE[c.entity_id] = byE[c.entity_id] || { name: c.name, list: [] }).list.push(c));
  const people = [r.root_entity_id, ...r.direct.slice(0, 12).map((p) => p.id)];
  t.innerHTML = `<p class="muted">Only contact details literally published on official websites or credible profiles. <b>Verified</b> = published on the person's official website. <b>Publicly listed</b> = listed on a credible profile/database (MusicBrainz, Wikidata, Genius, Discogs). Nothing is guessed.</p>
  <div class="panel" style="overflow:auto"><table class="data"><thead><tr><th>Person</th><th>Type</th><th>Contact</th><th>Purpose</th><th>Status</th><th>Source</th><th>Checked</th></tr></thead><tbody>
  ${people.map((id) => {
    const e = byE[id];
    const name = e?.name || r.direct.find((p) => p.id === id)?.name || r.root_name;
    if (!e) return `<tr><td><a class="name" href="#/p/${id}">${esc(name)}</a></td><td colspan="6">${status("unavailable")} <span class="faint">Not profiled yet or nothing published — open the profile and press “Refresh profile”.</span></td></tr>`;
    return e.list.map((c, i) => `<tr>${i === 0 ? `<td rowspan="${e.list.length}"><a class="name" href="#/p/${id}">${esc(name)}</a></td>` : ""}
      <td class="mono">${esc(c.contact_type)}</td><td>${contactValue(c)}</td><td class="muted">${esc(c.purpose || "")}</td><td>${status(c.status)}</td>
      <td class="prov">${c.sources.map((s) => `<a href="${url(s.url)}" target="_blank" rel="noopener">${esc(s.provider)}</a>`).join(" · ")}</td><td class="mono faint">${esc(day(c.last_checked_at))}</td></tr>`).join("");
  }).join("")}</tbody></table></div>`;
}

function contactValue(c) {
  if (c.contact_type === "email") return `<a href="mailto:${esc(c.value)}">${esc(c.value)}</a>`;
  if (c.contact_type === "phone") return `<a href="tel:${esc(c.value)}">${esc(c.value)}</a>`;
  return `<a href="${url(c.value)}" target="_blank" rel="noopener">${esc(c.value.replace(/^https?:\/\/(www\.)?/, ""))}</a>`;
}

function tabSources(r) {
  const by = {};
  r.sources.forEach((s) => (by[s.provider] = by[s.provider] || []).push(s));
  $("#tab").innerHTML = `<p class="muted">Every record used in this research. Credits and facts link back to these pages individually throughout the results.</p>` +
    Object.entries(by).map(([p, list]) => `<div class="panel"><h3>${esc(p)} · ${list.length}</h3>${list.slice(0, 200).map((s) => `<div class="mono" style="padding:2px 0"><a href="${url(s.url)}" target="_blank" rel="noopener">${esc(s.url)}</a>${s.retrieved_at ? ` <span class="faint">· ${esc(day(s.retrieved_at))}</span>` : ""}</div>`).join("")}</div>`).join("");
}

/* ---------------- profile ---------------- */
async function renderProfile(id) {
  view.innerHTML = `<div class="muted"><span class="spinner"></span>Loading profile…</div>`;
  const p = await api(`/api/people/${id}`);
  const loc = (f, label) => p.facts[f] ? `<dt>${label}</dt><dd>${p.facts[f].values.map((v) => `${esc(v.value)} <span class="prov">${provLinks(v.sources)}</span> ${status(v.confidence)}`).join("<br>")}${p.facts[f].discrepancy ? ` <div class="warn" style="margin-top:4px">Sources disagree — check the linked records.</div>` : ""}</dd>` : "";
  const bios = [...(p.facts.bio?.values || []), ...(p.facts.summary?.values || [])];
  const crm = p.crm;
  view.innerHTML = `
  <div class="profile-head">
    <div>
      <div class="eyebrow">${esc(p.type)}${p.primary_role ? ` · ${esc(p.primary_role)}` : ""}${p.disambiguation ? ` · ${esc(p.disambiguation)}` : ""}</div>
      <h1>${esc(p.name)}</h1>
      ${p.aliases.length ? `<div class="muted">Also known as: ${esc(p.aliases.slice(0, 8).join(", "))}</div>` : ""}
      <div class="roles" style="margin:10px 0 16px">${p.roles.map((r) => roleChip(r.role, r.count)).join("")}</div>
      <dl class="kv">
        ${loc("area", "Associated area")}${loc("residence", "Residence")}${loc("formed_in", "Formed in")}${loc("begin_area", "Origin")}${loc("born_in", "Born in")}${loc("country", "Country")}
        ${p.facts.genre ? `<dt>Genres</dt><dd>${p.facts.genre.values.map((v) => `${esc(v.value)}<span class="prov"> ${esc(v.sources.map((s) => s.provider).join("+"))}</span>`).join(", ")}</dd>` : ""}
        ${p.facts.occupation ? `<dt>Occupation</dt><dd>${p.facts.occupation.values.map((v) => esc(v.value)).join(", ")} <span class="prov">wikidata</span></dd>` : ""}
        ${p.facts.record_label ? `<dt>Labels</dt><dd>${p.facts.record_label.values.map((v) => esc(v.value)).join(", ")} <span class="prov">wikidata</span></dd>` : ""}
        ${p.active_years ? `<dt>Active (credits)</dt><dd>${p.active_years[0]}–${p.active_years[1]} · ${p.credit_count} tracks in your database</dd>` : ""}
        ${p.profiled_at ? `<dt>Profile checked</dt><dd class="mono">${esc(day(p.profiled_at))}</dd>` : `<dt>Profile</dt><dd class="muted">Background/contacts not fetched yet</dd>`}
      </dl>
      <div style="display:flex;gap:8px;margin-top:16px;flex-wrap:wrap">
        <button class="btn" id="refresh">${p.profiled_at ? "Refresh profile" : "Fetch background, contacts & metrics"}</button>
        <button class="btn primary" data-research="${p.id}">Research this network →</button>
      </div>
    </div>
    <div class="panel">
      <h3>Networking</h3>
      <label class="muted" style="font-size:12px">Status</label>
      <select id="crm-status" style="width:100%;margin:4px 0 10px">${CRM.map((s) => `<option value="${s}" ${crm?.status === s ? "selected" : ""}>${CRM_TEXT[s]}</option>`).join("")}</select>
      <label class="muted" style="font-size:12px">Tags / lists (comma-separated)</label>
      <input id="crm-tags" style="width:100%;margin:4px 0 10px" value="${esc((crm?.tags || []).join(", "))}" placeholder="Producers to Contact, UK Network">
      <label class="muted" style="font-size:12px">Follow-up date</label>
      <input id="crm-fu" type="date" style="width:100%;margin:4px 0 10px" value="${esc(crm?.follow_up || "")}">
      <label class="muted" style="font-size:12px">Notes</label>
      <textarea id="crm-notes" rows="4" style="width:100%;margin:4px 0 10px">${esc(crm?.notes || "")}</textarea>
      <div style="display:flex;gap:8px"><button class="btn primary small" id="crm-save">${crm ? "Update" : "Save to database"}</button>${crm ? `<button class="ghost small" id="crm-del">Remove</button>` : ""}<span id="crm-msg" class="muted" style="font-size:12px;align-self:center"></span></div>
    </div>
  </div>
  <div class="grid2">
    <div>
      ${bios.length ? `<div class="panel"><h3>Background</h3>${bios.map((b) => `<p class="bio">${esc(b.value)}</p><p class="prov">${provLinks(b.sources)} · ${esc(day(b.sources[0].retrieved_at))}</p>`).join("")}</div>` : ""}
      <div class="panel"><h3>Works with · ${p.collaborators.length}</h3>${p.collaborators.slice(0, 25).map((c) => personRow(c)).join("") || `<div class="empty">No collaborators in the database yet.</div>`}</div>
      <div class="panel" style="overflow:auto"><h3>Credits · ${p.credit_count}</h3><table class="data"><thead><tr><th>Track</th><th>Artist</th><th>Year</th><th>Role</th><th>Source</th></tr></thead><tbody>
        ${p.credits.slice(0, 200).map((c) => `<tr><td>${esc(c.title)}${c.release ? `<div class="faint" style="font-size:12px">${esc(c.release)}</div>` : ""}</td><td class="muted">${esc(c.artist || "")}</td><td class="mono">${c.year || ""}</td>
          <td>${c.roles.map((r) => `${roleChip(r.role, 1)}<span class="prov"> ${esc(r.providers.join("+"))}</span>`).join("<br>")}</td><td class="prov">${provLinks(c.sources)}</td></tr>`).join("")}</tbody></table></div>
    </div>
    <div>
      <div class="panel"><h3>Professional contact</h3>${p.contacts.length ? p.contacts.map((c) => `
        <div style="padding:8px 0;border-top:1px solid var(--line)"><span class="mono faint">${esc(c.contact_type)}${c.purpose ? ` · ${esc(c.purpose)}` : ""}</span> ${status(c.status)}<br>${contactValue(c)}
        <div class="prov">${c.sources.map((s) => `<a href="${url(s.url)}" target="_blank" rel="noopener">${esc(s.provider)}</a> ${esc(day(s.last_checked_at))}`).join(" · ")}</div>
        ${c.context ? `<div class="faint" style="font-size:12px">“${esc(c.context)}”</div>` : ""}</div>`).join("") : `<div>${status("unavailable")} <span class="faint">No public professional contact found${p.profiled_at ? "" : " yet — fetch the profile"}.</span></div>`}
        ${p.unavailable.filter((u) => u.topic.startsWith("contacts")).map((u) => `<p class="faint" style="font-size:12px">${esc(u.reason)} (${esc(day(u.checked_at))})</p>`).join("")}
      </div>
      <div class="panel"><h3>Audience metrics</h3>${p.metrics.map((m) => `
        <div style="padding:8px 0;border-top:1px solid var(--line)"><span class="mono faint">${esc(m.platform)} · ${esc(m.metric.replace("_", " "))}</span><div style="font:400 24px var(--serif)">${fmtN(m.latest.value)}</div>
        <div class="prov"><a href="${url(m.latest.url)}" target="_blank" rel="noopener">${esc(m.latest.description)}</a> · live value retrieved ${esc(day(m.latest.retrieved_at))}</div>
        ${m.history.length ? `<div class="faint mono">History: ${m.history.map((h) => `${fmtN(h.value)} (${esc(day(h.retrieved_at))})`).join(", ")}</div>` : ""}</div>`).join("") || `<div class="empty">No metrics collected.</div>`}
        ${p.unavailable.filter((u) => !u.topic.startsWith("contacts")).map((u) => `<div style="padding:6px 0;border-top:1px solid var(--line)">${status("unavailable")} <span class="mono faint">${esc(u.topic)}</span><div class="faint" style="font-size:12px">${esc(u.reason)}</div></div>`).join("")}
      </div>
      <div class="panel"><h3>Identity & sources</h3>
        ${p.identifiers.map((i) => `<div class="mono" style="padding:2px 0">${esc(i.provider)}: ${i.url ? `<a href="${url(i.url)}" target="_blank" rel="noopener">${esc(i.external_id)}</a>` : esc(i.external_id)} <span class="faint">${esc(i.match_method.replaceAll("_", " "))}</span></div>`).join("")}
        ${p.name_matched_ids.length ? `<div class="warn" style="margin-top:8px">Matched by name only: ${p.name_matched_ids.map((i) => esc(i.provider)).join(", ")}. Check that this is the same person.</div>` : ""}
        ${p.identity_questions.map((q) => `<div class="warn" style="margin-top:8px">Possible duplicate: <a class="name" href="#/p/${q.id}">${esc(q.name)}</a> — ${esc(q.reason)}</div>`).join("")}
      </div>
    </div>
  </div>`;
  wireResearchButtons(view);
  $("#refresh").onclick = async (e) => {
    e.target.disabled = true; e.target.innerHTML = `<span class="spinner"></span>Fetching from all linked sources…`;
    try { await api(`/api/people/${id}/refresh`, { method: "POST" }); } finally { renderProfile(id); }
  };
  $("#crm-save").onclick = async () => {
    await api(`/api/people/${id}/crm`, { method: "PUT", body: JSON.stringify({ status: $("#crm-status").value, notes: $("#crm-notes").value, tags: $("#crm-tags").value.split(","), follow_up: $("#crm-fu").value || null }) });
    $("#crm-msg").textContent = "Saved";
    setTimeout(() => renderProfile(id), 500);
  };
  const del = $("#crm-del");
  if (del) del.onclick = async () => { await api(`/api/people/${id}/crm`, { method: "DELETE" }); renderProfile(id); };
}

/* ---------------- people db & history ---------------- */
async function renderPeople() {
  const rows = await api("/api/saved");
  const tags = [...new Set(rows.flatMap((r) => r.tags))].sort();
  const sel = store.get("tagFilter") || "";
  const st = store.get("statusFilter") || "";
  const list = rows.filter((r) => (!sel || r.tags.includes(sel)) && (!st || r.status === st));
  view.innerHTML = `<div class="rhead"><div><div class="eyebrow">Your network</div><h1>People Database</h1></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap"><select id="tf"><option value="">All lists</option>${tags.map((t) => `<option ${t === sel ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>
    <select id="sf2"><option value="">All statuses</option>${CRM.map((s) => `<option value="${s}" ${s === st ? "selected" : ""}>${CRM_TEXT[s]}</option>`).join("")}</select>
    <a class="btn" href="/api/saved/export.csv">Export CSV</a></div></div>
    <div class="panel" style="overflow:auto;margin-top:20px"><table class="data"><thead><tr><th>Name</th><th>Role</th><th>Based</th><th>Status</th><th>Lists</th><th>Follow-up</th><th>Best contact</th></tr></thead><tbody>
    ${list.map((r) => `<tr><td><a class="name" href="#/p/${r.id}">${esc(r.name)}</a><div class="faint" style="font-size:12px">${esc(r.notes.slice(0, 80))}</div></td><td>${esc(r.primary_role || "")}</td><td class="muted">${esc(r.location || "")}</td>
      <td>${esc(CRM_TEXT[r.status])}</td><td>${r.tags.map((t) => `<span class="chip">${esc(t)}</span>`).join(" ")}</td><td class="mono ${r.follow_up && r.follow_up <= day(new Date().toISOString()) ? "" : "faint"}">${esc(r.follow_up || "")}</td>
      <td>${r.best_contact ? `${contactValue(r.best_contact)} ${status(r.best_contact.status)}` : `<span class="faint">—</span>`}</td></tr>`).join("") || `<tr><td colspan="7" class="empty">Nobody saved yet. Open any profile and press “Save to database”.</td></tr>`}
    </tbody></table></div>`;
  $("#tf").onchange = (e) => { store.set("tagFilter", e.target.value); renderPeople(); };
  $("#sf2").onchange = (e) => { store.set("statusFilter", e.target.value); renderPeople(); };
}

async function renderHistory() {
  const rows = await api("/api/research");
  const row = (r) => `<tr><td><a class="name" href="#/r/${r.id}">${esc(r.title || r.root_name || r.query)}</a><div class="faint" style="font-size:12px">“${esc(r.query)}” · ${esc(r.target_kind)}</div></td>
    <td class="mono">${esc(day(r.created_at))}</td><td>${esc(r.status)}</td><td class="mono">${r.counts ? `${r.counts.tracks} tracks · ${r.counts.direct} direct · ${r.counts.second} 2nd` : ""}</td>
    <td><button class="ghost small" data-del="${r.id}">Delete</button></td></tr>`;
  const saved = rows.filter((r) => r.saved), recent = rows.filter((r) => !r.saved);
  view.innerHTML = `<div class="rhead"><div><div class="eyebrow">Research</div><h1>Saved Research</h1></div></div>
    <div class="panel" style="margin-top:20px"><h3>Research projects</h3><table class="data"><tbody>${saved.map(row).join("") || `<tr><td class="empty">Save a research session as a project from its results page.</td></tr>`}</tbody></table></div>
    <div class="panel"><h3>History</h3><table class="data"><tbody>${recent.map(row).join("") || `<tr><td class="empty">No searches yet.</td></tr>`}</tbody></table></div>`;
  view.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => { if (confirm("Delete this research session? People and credits stay in the database.")) { await api(`/api/research/${b.dataset.del}`, { method: "DELETE" }); renderHistory(); } });
}

/* ---------------- provider status drawer ---------------- */
$("#providers-btn").onclick = async () => {
  const s = await api("/api/status");
  drawer.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center"><h2 style="font:400 30px var(--serif);margin:0">Data sources</h2><button class="ghost small" id="dc">Close</button></div>
    <p class="muted">Keys live in <span class="mono">.env</span> on the server and are never sent to the browser. See <span class="mono">docs/PROVIDERS.md</span> for how to get each one.</p>
    ${s.contact_email_set ? "" : `<div class="warn">Set FLAEVRD_CONTACT_EMAIL — MusicBrainz and Wikidata require a contact address in the User-Agent.</div>`}
    ${s.providers.map((p) => `<div style="padding:12px 0;border-top:1px solid var(--line)"><b>${esc(p.provider)}</b> ${p.configured ? status("verified").replace("Verified", "Ready") : `<span class="status unverified">Waiting for configuration</span>`}
      <div class="muted" style="font-size:13px">${esc(p.purpose)}</div>${!p.configured && p.requires.length ? `<div class="mono faint">needs ${esc(p.requires.join(", "))}</div>` : ""}</div>`).join("")}
    <p class="faint mono">Limits: ${s.limits.max_releases} releases · ${s.limits.max_credit_recordings} credited recordings · ${s.limits.max_expansions} expansions per search</p>`;
  drawer.classList.remove("hidden");
  $("#dc").onclick = () => drawer.classList.add("hidden");
};

route();
