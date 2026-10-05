"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ESC[c]);
const PORTS = Array.from({ length: 10 }, (_, i) => "ge-0/0/" + i);

let S = null;                                   // last /api/state
let sel = localStorage.getItem("lf_sel") || ""; // selected sandbox
let focusItem = null;                           // {t:"node",id} | {t:"cable",id}
let busy = false, creating = false, authNeeded = false;
let token = localStorage.getItem("simrack_token") || "";
const log = [];
const consoleOut = {};
let moveFrom = {};                              // {bridge,node}: which end the move form is moving
let returnTo = null, lastCanvasW = 0, canvasRO = null;
let view = localStorage.getItem("lf_view") || ""; // "" (sandboxes) | "setup" | "import" | "shape:<name>"
let prevView = "", introPending = false;
let setupPage = null, setupErr = "", setupLoading = false; // the setup page as last read
let autoSetup = false;                          // a SimRack with no lab profile opens setup once
const picks = {};                               // shape name -> {set, touched}: the switches a build would make
const rootPw = {};                              // sandbox name -> root password, held only after Reveal and only until the view changes
function setView(v) {
  view = v || "";
  if (view) localStorage.setItem("lf_view", view); else localStorage.removeItem("lf_view");
}

/* ---------- api ---------- */
async function api(path, body, method) {
  method = method || (body === undefined ? "GET" : "POST");
  const headers = { "Accept": "application/json" };
  if (method !== "GET") headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(path, { method, headers, body: method === "GET" ? undefined : JSON.stringify(body || {}) });
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch { data = { error: text.slice(0, 300) || res.statusText }; }
  if (res.status === 401) { authNeeded = true; renderBanner(); }
  if (!res.ok) { const e = new Error(data.error || ("HTTP " + res.status)); e.detail = data.detail || ""; throw e; }
  return data;
}

/* ---------- activity log ---------- */
function note(kind, text, detail) {
  const t = new Date().toTimeString().slice(0, 8);
  log.unshift({ kind, text, detail, t });
  if (log.length > 60) log.pop();
  renderLog();
}
function renderLog() {
  const el = $("#log"); if (!el) return;
  el.innerHTML = log.length ? log.map(l => `<li><time>${esc(l.t)}</time><div><span class="${l.kind}">${esc(l.text)}</span>${
    l.detail ? `<details${l.kind === "err" ? " open" : ""}><summary>Details</summary><pre>${esc(typeof l.detail === "string" ? l.detail : JSON.stringify(l.detail, null, 2))}</pre></details>` : ""}</div></li>`).join("")
    : `<li class="empty muted">Nothing yet. Actions and their results show up here.</li>`;
}

/* ---------- running an action ---------- */
async function run(btn, label, fn, after) {
  if (busy) return;
  if (btn && btn.dataset.ask && !(await ask(btn, label))) return;
  if (busy) return;
  busy = true;
  const text = btn ? btn.innerHTML : "";
  if (btn) { btn.setAttribute("aria-busy", "true"); if (btn.dataset.busyLabel) btn.textContent = btn.dataset.busyLabel; }
  setWriteDisabled();
  note("run", label + "…");
  try {
    const result = await fn();
    note("ok", label + " done", result);
    if (after) after(result);
  } catch (e) {
    note("err", label + " failed: " + e.message, e.detail || "");
  } finally {
    busy = false;
    if (btn && btn.isConnected) { btn.removeAttribute("aria-busy"); btn.innerHTML = text; }
    await refresh(true);
  }
}

/* ---------- the yes/no box: every change asks first ----------
   1 No (also Enter and Esc), 2 Yes this once, 3 Yes for this session. A session yes covers one kind of change
   (data-ask) until the tab closes; a change that destroys something (data-ask-always) asks every time. */
const KINDS = { build: "builds", cable: "cable changes", power: "power changes", snapshot: "saved revert points",
  console: "console commands", mist: "Mist changes", repair: "cabling repairs" };
function ask(btn, label) {
  const kind = btn.dataset.ask, always = btn.hasAttribute("data-ask-always"), box = $("#ask");
  if (!always && sessionStorage.getItem("simrack_ok:" + kind)) return Promise.resolve(true);
  $("#ask-q").textContent = label + "?";
  const why = $("#ask-why"); why.textContent = btn.dataset.askWhy || ""; why.hidden = !why.textContent;
  $("#ask-once").className = always || btn.classList.contains("danger") ? "danger solid" : "primary";
  $("#ask-session").hidden = always;
  $("#ask-hint").textContent = "Enter or Esc means No." + (always ? " This one asks every time." : ` 3 stops asking about ${KINDS[kind] || "these"} until this tab closes.`);
  box.returnValue = "";
  box.showModal();
  $('#ask [value="no"]').focus();
  return new Promise(done => box.addEventListener("close", () => {
    if (box.returnValue === "session") sessionStorage.setItem("simrack_ok:" + kind, "1");
    done(box.returnValue === "once" || box.returnValue === "session");
  }, { once: true }));
}
$("#ask").addEventListener("keydown", e => {
  const pick = { 1: "no", 2: "once", 3: "session" }[e.key];
  if (!pick || (pick === "session" && $("#ask-session").hidden)) return;
  e.preventDefault(); $("#ask").close(pick);
});
async function togglePause(btn) {
  const pause = !S.paused;
  btn.disabled = true;
  try {
    await api("/api/pause", { paused: pause });
    note("ok", pause ? "Changes are paused. A change already running stops at its next step." : "Changes are back on.");
  } catch (e) { note("err", (pause ? "Couldn't pause changes: " : "Couldn't resume changes: ") + e.message); }
  btn.disabled = false;
  refresh(true);
}

function writesOn() { return !!(S && S.writes_enabled); }
function mistWritesOn() { return writesOn() && !!(S && S.mist && S.mist.writes_enabled); }
/* one pass so a button's reasons never overwrite each other: host first, then Mist, then its own (data-why), then busy.
   data-read marks an action that needs none of the gates, only a free hand. */
function setWriteDisabled() {
  const mistOn = !!(S && S.mist && S.mist.configured);
  for (const b of $$("[data-write], [data-mist-read], [data-read]")) {
    if (b.getAttribute("aria-busy") === "true") continue;
    const why = b.hasAttribute("data-write") && !writesOn() ? ((S && S.read_only_reason) || "Read-only")
      : (b.hasAttribute("data-mist-write") || b.hasAttribute("data-mist-read")) && !mistOn ? "No Mist token yet"
      : b.hasAttribute("data-mist-write") && !mistWritesOn() ? ((S && S.mist.read_only_reason) || "Mist changes are off")
      : b.dataset.why || (busy ? "Another action is running" : "");
    b.disabled = !!why;
    b.title = why || b.dataset.tip || "";
  }
}

/* replace a region's HTML but keep what the user typed, picked and focused (a file input cannot be refilled) */
function swap(el, html) {
  if (!el || el._html === html) return;
  const kept = {};
  for (const f of $$("input, select, textarea", el)) if (f.name && f.type !== "file") kept[f.name] = f.type === "checkbox" ? f.checked : f.value;
  const ae = document.activeElement, back = ae && ae !== el && el.contains(ae) ? reselect(ae) : null;
  const scrolled = $$(".scroll-y", el).map(x => x.scrollTop);
  el.innerHTML = html; el._html = html;
  $$(".scroll-y", el).forEach((x, i) => { if (scrolled[i]) x.scrollTop = scrolled[i]; });
  for (const f of $$("input, select, textarea", el)) {
    if (f.type === "file" || !(f.name in kept)) continue;
    if (f.type === "checkbox") f.checked = kept[f.name];
    else if (f.tagName === "SELECT") { if ($$("option", f).some(o => o.value === kept[f.name] && !o.disabled)) f.value = kept[f.name]; }
    else f.value = kept[f.name];
  }
  if (back) { const f = el.querySelector(back); if (f) f.focus({ preventScroll: true }); }
}
function fillSelect(select, html) {
  if (!select || select._html === html || document.activeElement === select) return;
  const v = select.value; select.innerHTML = html; select._html = html;
  if ($$("option", select).some(o => o.value === v && !o.disabled)) select.value = v;
}

/* ---------- helpers on state ---------- */
const sandbox = () => (S && S.sandboxes.find(s => s.name === sel)) || null;
const shapeOf = name => (S && (S.shapes || []).find(x => x.name === name)) || null;
const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
const gb = mb => (mb / 1024).toFixed(1) + "\u00a0GB";
function headroomMb() { return S && S.host.free_ram_mb != null ? S.host.free_ram_mb - S.limits.min_free_ram_mb : null; }
const switchMb = () => S.limits.switch_mem_mb;
function roomNow() { const head = headroomMb(); return head == null ? null : Math.max(0, Math.floor(head / switchMb())); }
/* a free sandbox name from a base: campus, then campus-2, campus-3 */
function freeName(base) {
  base = String(base || "").toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "sandbox";
  const taken = new Set(S.sandboxes.map(s => s.name));
  for (let i = 1; ; i++) {
    const name = i === 1 ? base : base.slice(0, 31 - String(i).length).replace(/-+$/, "") + "-" + i;
    if (!taken.has(name)) return name;
  }
}
const SWITCH_IMAGE = /vjunos.?(switch|ex)/i;
function bootPreferred() {
  const img = S.images.find(i => i.kind === "import" && SWITCH_IMAGE.test(i.volid));
  if (img) return "image:" + img.volid;
  const t = S.templates.find(t => SWITCH_IMAGE.test(t.name));
  return t ? "tmpl:" + t.vmid : "";
}
function bootOptions() {
  const imp = S.images.filter(i => i.kind === "import"), iso = S.images.filter(i => i.kind === "iso"), pref = bootPreferred();
  const opt = (v, label) => `<option value="${esc(v)}"${v === pref ? " selected" : ""}>${label}</option>`;
  let h = "";
  if (imp.length) h += `<optgroup label="Disk images">${imp.map(i => opt("image:" + i.volid, esc(i.volid.split("/").pop()) + (i.size_gb ? " · " + i.size_gb + " GB" : ""))).join("")}</optgroup>`;
  if (S.templates.length) h += `<optgroup label="Templates">${S.templates.map(t => opt("tmpl:" + t.vmid, `${t.vmid} ${esc(t.name)}`)).join("")}</optgroup>`;
  if (iso.length) h += `<optgroup label="ISO installers">${iso.map(i => opt("image:" + i.volid, esc(i.volid.split("/").pop()))).join("")}</optgroup>`;
  return h || `<option value="" disabled selected>No images in local:import or templates found</option>`;
}
function updateBootNote(form) {
  const n = $("[data-boot-note]", form); if (!n) return;
  const v = form.boot.value || "", tmpl = v.startsWith("tmpl:") ? S.templates.find(t => "tmpl:" + t.vmid === v) : null;
  const ok = SWITCH_IMAGE.test(tmpl ? tmpl.name : v);
  n.className = "note" + (ok ? "" : " warn");
  n.innerHTML = ok ? (tmpl ? "Every switch is a full clone of this template." : "A disk image gives every switch its own fresh disk. Importing takes a few minutes per switch.")
    : `This doesn't look like a vJunos-switch disk. Copy <code>vJunos-switch-*.qcow2</code> into <code>/var/lib/vz/import/</code> on ${esc(S.host.node || "the host")}, then Refresh.`;
}
function bootBody(v) {
  if (!v) return {};
  if (v.startsWith("tmpl:")) return { template_vmid: Number(v.slice(5)) };
  return { image: v.slice(6) };
}
function usedPorts(s, node) {
  const used = new Set();
  for (const l of s.links) { if (l.a_node === node) used.add(l.a_port); if (l.b_node === node) used.add(l.b_port); }
  return used;
}
function portOptions(s, node, allow) {
  const used = node ? usedPorts(s, node) : new Set();
  return PORTS.map(p => `<option value="${p}"${used.has(p) && p !== allow ? " disabled" : ""}>${p}${used.has(p) && p !== allow ? " · in use" : ""}</option>`).join("");
}
function nodeOptions(s, placeholder) {
  return `<option value="">${esc(placeholder || "Pick a guest")}</option>` + s.nodes.map(n => `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join("");
}
function moveTargets(s, l, from) {
  const other = from === l.a_node ? l.b_node : l.a_node;
  return `<option value="">Pick a guest</option>` + s.nodes.map(n => n.name === other
    ? `<option value="${esc(n.name)}" disabled>${esc(n.name)} · other end</option>`
    : `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join("");
}

/* ---------- top bar, banner, rail ---------- */
function renderTop() {
  $("#conn").className = "conn on";
  $("#node").textContent = S.host.node;
  document.title = `SimRack · ${S.host.node}`;
  const free = S.host.free_ram_mb, total = S.host.total_ram_mb;
  if (free != null) {
    const head = headroomMb(), per = switchMb();
    $("#ram").innerHTML = `<b>${gb(free)}</b> free`;
    $("#ram").parentElement.title = total ? `Memory new guests can use: ${gb(free)} of ${gb(total)}` : "Memory new guests can use";
    const bar = $("#rambar");
    bar.style.transform = `scaleX(${Math.max(.04, Math.min(1, total ? free / total : 1))})`;
    bar.style.background = head < per ? "var(--bad)" : head < per * 2 ? "var(--warn)" : "var(--ok)";
    const n = Math.max(0, Math.floor(head / per));
    $("#fitstat").innerHTML = `room for <b>${n}</b> more switch${n === 1 ? "" : "es"}`;
  } else { $("#ram").textContent = "memory unknown"; $("#fitstat").textContent = ""; }
  const mode = $("#modechip");
  mode.className = "chip " + (S.writes_enabled ? "accent" : "warn");
  mode.textContent = S.paused ? "Paused" : S.writes_enabled ? "Writes on" : "Read-only";
  const pb = $("#pausebtn");
  pb.hidden = !(S.writes_enabled || S.paused);
  pb.setAttribute("aria-pressed", String(!!S.paused));
  pb.textContent = S.paused ? "Resume changes" : "Pause changes";
  pb.title = S.paused ? "Let SimRack change the lab again" : "Stop every change. One already running stops at its next step.";
  const m = S.mist, mc = $("#mistchip");
  mc.className = "chip " + (!m.configured ? "" : m.writes_enabled ? "mist" : "warn");
  mc.textContent = !m.configured ? "Mist: no token" : m.writes_enabled ? "Mist read/write" : "Mist read-only";
  $("#updated").textContent = "updated " + new Date().toTimeString().slice(0, 5);
  $("#prodvm").textContent = S.production.vmids.join(", ");
  $("#prodbr").textContent = S.production.bridges.join(", ");
  $("#prodmist").textContent = S.production.mist_sites.join(", ") || "none";
  if (view === "setup") $("#setupbtn").setAttribute("aria-current", "page"); else $("#setupbtn").removeAttribute("aria-current");
}
function renderBanner(err) {
  const b = $("#banner");
  if (authNeeded) {
    if ($('[data-form="token"]', b)) return;
    b.className = "banner err"; b.hidden = false;
    b._html = "";
    b.innerHTML = `This SimRack needs a bearer token.
      <form class="actions" data-form="token"><input name="token" type="password" placeholder="SIMRACK_TOKEN" autocomplete="off"><button class="sm" type="submit">Save token</button></form>`;
    return;
  }
  const fix = view === "setup" ? "" : ` <button class="sm" data-act="setup">Open setup</button>`;
  if (err) { setBanner("err", `<span>${esc("Cannot reach the SimRack API: " + err)}</span>`); return; }
  if (S && S.host.inventory_error) { setBanner("err", `<span>${esc("Proxmox inventory failed: " + S.host.inventory_error)}</span>${fix}`); return; }
  if (S && S.paused) { setBanner("", "<span>Changes are paused. You can look around; nothing in the lab changes until you resume them from the top bar.</span>"); return; }
  if (S && !S.writes_enabled) { setBanner("", `<span>${esc((S.read_only_reason || "Read-only.") + " You can look around, but building, cabling and power are off.")}</span>${fix}`); return; }
  b.hidden = true;
}
function setBanner(kind, html) {
  const b = $("#banner");
  b.className = kind ? "banner " + kind : "banner"; b.hidden = false;
  if (b._html !== html) { b.innerHTML = html; b._html = html; }
}
function renderRail() {
  const list = $("#list");
  if (!S.sandboxes.length) swap(list, `<p class="rail-empty">No sandboxes yet.</p>`);
  else swap(list, S.sandboxes.map(s => {
    const up = s.nodes.filter(n => n.running).length;
    return `<button class="sbx" data-act="pick" data-name="${esc(s.name)}" aria-current="${s.name === sel && !creating && !view}">
      <span class="n">${esc(s.name)}</span><span class="c">${up}/${s.nodes.length} up</span>
      <span class="m">${esc(s.recipe.name)} · ${s.links.length} cable${s.links.length === 1 ? "" : "s"}${s.mist_site_id ? " · Mist" : ""}</span></button>`;
  }).join(""));
  const shapes = S.shapes || [];
  swap($("#shapes"), shapes.length ? shapes.map(x => `<button class="sbx" data-act="pick-shape" data-name="${esc(x.name)}" aria-current="${view === "shape:" + x.name}">
      <span class="n">${esc(x.name)}</span><span class="c">${plural(x.nodes.length, "switch", "switches")}</span>
      <span class="m">${esc(x.kind)} · ${plural(x.links.length, "cable")}</span></button>`).join("")
    : `<p class="rail-empty">None yet. Import a fabric from Mist to see what a sandbox of it would take.</p>`);
}

/* ---------- main ---------- */
function renderMain() {
  const main = $("#main");
  const s = sandbox(), sh = view.startsWith("shape:") ? shapeOf(view.slice(6)) : null;
  const want = view === "setup" ? "setup" : view === "import" ? "import" : sh ? "shape:" + sh.name : creating || !s ? "create" : "sbx:" + s.name;
  if (main.dataset.view !== want) {
    const switching = !!main.dataset.view;
    main.dataset.view = want;
    focusItem = null; returnTo = null; lastCanvasW = 0;
    for (const k in rootPw) delete rootPw[k];
    if (want === "setup") main.innerHTML = setupShell();
    else if (want === "import") main.innerHTML = importShell();
    else if (sh) { main.innerHTML = shapeShell(sh); introPending = true; }
    else if (want === "create") {
      main.innerHTML = `<div class="welcome"><h1>${S.sandboxes.length ? "New sandbox" : "Build your first sandbox"}</h1>
        <p>A sandbox is a private copy of the fabric: its own switches, cables and, optionally, its own Mist site. Nothing in it can reach the live lab.</p>
        <section class="panel"><div class="body" id="createhost"></div></section>
        <p class="aside">Already running the fabric in Mist? <button class="link" data-act="import">Import its topology</button> and build a sandbox shaped like it.</p></div>`;
      $("#createhost").appendChild($("#tpl-create").content.cloneNode(true));
      $("[data-cancel]", main).hidden = !S.sandboxes.length;
    } else main.innerHTML = shell(s);
    if (switching && scrollY > 0) scrollTo({ top: 0 });
    watchCanvas();
  }
  if (want === "setup") updateSetup();
  else if (want === "import") updateImport();
  else if (sh) updateShape(sh);
  else if (want === "create") updateCreate(); else updateSandbox(s);
  setWriteDisabled();
}

function updateCreate() {
  const form = $('[data-form="create"]'); if (!form) return;
  fillSelect($("[data-recipes]", form), S.recipes.map(r => `<option value="${esc(r.name)}">${esc(r.name)}</option>`).join(""));
  fillSelect($("[data-boot]", form), bootOptions());
  updateRecipeNote(form);
  updateBootNote(form);
}
function updateRecipeNote(form) {
  const r = S.recipes.find(x => x.name === form.recipe.value); if (!r) return;
  $("[data-recipe-note]", form).textContent = r.description;
  const switches = (r.roles || []).length, need = switches * switchMb(), head = headroomMb();
  const fit = $("[data-fit]", form);
  fit.className = "fit" + (head != null && need > head ? " no" : "");
  fit.innerHTML = `${switches} switch${switches === 1 ? "" : "es"}: ${(r.roles || []).map(x => esc(x.name)).join(", ")}<br>Needs <b>${gb(need)}</b> · ${head == null ? "free memory unknown" : `<b>${gb(Math.max(0, head))}</b> available above the ${gb(S.limits.min_free_ram_mb)} reserve`}`;
}

/* ---------- setup: the tokens, and what in the lab is live ---------- */
const MIST_CLOUDS = [
  ["https://api.mist.com/api/v1", "Global 01"], ["https://api.gc1.mist.com/api/v1", "Global 02"],
  ["https://api.ac2.mist.com/api/v1", "Global 03"], ["https://api.gc2.mist.com/api/v1", "Global 04"],
  ["https://api.gc4.mist.com/api/v1", "Global 05"], ["https://api.eu.mist.com/api/v1", "EMEA 01"],
  ["https://api.gc3.mist.com/api/v1", "EMEA 02"], ["https://api.ac6.mist.com/api/v1", "EMEA 03"],
  ["https://api.gc6.mist.com/api/v1", "EMEA 04"], ["https://api.ac5.mist.com/api/v1", "APAC 01"],
  ["https://api.gc5.mist.com/api/v1", "APAC 02"], ["https://api.gc7.mist.com/api/v1", "APAC 03"],
];
const PROTECT = { vmids: "VMs", lxc: "Containers", bridges: "Bridges", subnets: "Subnets", mist_sites: "Mist sites" };
const bare = u => String(u || "").trim().replace(/\/+$/, "");

function setupShell() {
  return `<div class="intake setup">
    <header class="intake-head setup-head">
      <div><h1 tabindex="-1">Set up SimRack</h1>
        <p>Give SimRack a Proxmox token, and a Mist token if you use Mist. Then tick what in the lab is live: SimRack never touches anything ticked.</p></div>
      <button class="ghost sm" data-act="setup-close">Close</button>
    </header>
    <div id="setup-wait"></div>
    <div id="setup-problems"></div>
    <section id="setup-connect" aria-labelledby="sec-connect" hidden></section>
    <section id="setup-lab" aria-labelledby="sec-lab" hidden></section>
  </div>`;
}
async function loadSetup() {
  if (setupLoading) return;
  setupLoading = true; setupErr = "";
  try { setupPage = await api("/api/setup"); } catch (e) { setupErr = e.message; }
  setupLoading = false;
  if (view === "setup" && S) renderMain();
}
function updateSetup(fresh) {
  const wait = $("#setup-wait"); if (!wait) return;
  if (!setupPage && !setupLoading && !setupErr) loadSetup();
  const page = setupPage, connect = $("#setup-connect"), lab = $("#setup-lab");
  swap(wait, setupErr ? `<div class="form-err" role="alert"><p>Couldn't read the setup page: ${esc(setupErr)}</p></div>
      <div class="actions"><button class="sm" data-act="setup-retry">Try again</button></div>`
    : page ? "" : `<p class="hint" role="status">Asking Proxmox and Mist what is in the lab…</p>`);
  swap($("#setup-problems"), page ? page.problems.map(p => `<div class="form-err warn">${errHtml(p.error, p.detail)}</div>`).join("") : "");
  connect.hidden = lab.hidden = !page;
  if (!page) return;
  if (connect._page !== page) { connect._page = page; keepOpen(connect, () => swap(connect, connectHtml(page))); }
  if (lab._page !== page) { lab._page = page; keepOpen(lab, () => paintLab(lab, page, fresh)); }
}
/* repaint a region and leave each <details> open or shut as the user left it */
function keepOpen(el, paint) {
  const was = {};
  for (const d of $$("details[data-key]", el)) was[d.dataset.key] = d.open;
  paint();
  for (const d of $$("details[data-key]", el)) if (d.dataset.key in was) d.open = was[d.dataset.key];
}
/* when the page is read again, what the user changed stays; a page just saved replaces it all */
function paintLab(el, page, fresh) {
  const kept = fresh ? [] : $$("[data-touched]", el).map(x => [x.name, x.type === "checkbox" ? x.checked : x.value]);
  const ae = document.activeElement, back = ae && el.contains(ae) ? reselect(ae) : null;
  el.innerHTML = labHtml(page);
  const f = $("form", el);
  for (const [name, v] of kept) {
    const x = f.elements.namedItem(name);
    if (!x || !x.tagName) continue;
    if (x.type === "checkbox") x.checked = v; else x.value = v;
    x.setAttribute("data-touched", "");
  }
  if (back) { const x = el.querySelector(back); if (x) x.focus({ preventScroll: true }); }
}
function connectHtml(page) {
  const pve = page.tokens.proxmox, mist = page.tokens.mist, saved = bare(mist.api);
  const clouds = (MIST_CLOUDS.some(([u]) => u === saved) ? MIST_CLOUDS : [[saved, "Saved"], ...MIST_CLOUDS])
    .map(([u, name]) => `<option value="${esc(u)}"${u === saved ? " selected" : ""}>${esc(name)} · ${esc(u.replace(/^https:\/\//, "").replace(/\/api\/v1$/, ""))}</option>`).join("");
  const keep = ` <span class="faint">· blank keeps the saved one</span>`;
  return `<h2 class="sec" id="sec-connect">Connect</h2>
    <form class="fields" data-form="setup-tokens" autocomplete="off">
      <fieldset class="group">
        <legend>Proxmox ${pve.set ? `<span class="chip ok">Token saved</span>` : `<span class="chip warn">No token yet</span>`}</legend>
        ${pve.set ? `<p class="note">SimRack signs in as <span class="mono">${esc(pve.id)}</span>.</p>` : ""}
        <label class="f"><span>Token ID and secret${pve.set ? keep : ""}</span>
          <input name="proxmox_token" type="password" autocomplete="off" spellcheck="false" class="mono" placeholder="simrack@pve!simrack=…"></label>
        <details data-key="pve-addr"><summary>Address</summary>
          <label class="f"><span>Proxmox API</span><input name="proxmox_api" class="mono" spellcheck="false" value="${esc(pve.api)}"></label>
          <p class="note">Change it only if SimRack runs somewhere other than the Proxmox host. A new address needs the token pasted again.</p>
        </details>
        <details data-key="pve-token"${pve.set ? "" : " open"}><summary>Make a token that may do only what SimRack needs</summary>
          <p class="note">Run these as root on the Proxmox host. The last one prints the token's ID and secret: paste that above.
            SimRack may change only guests in the resource pool <span class="mono">simrack</span>, where it makes every guest.
            A template made later needs one more command; a build from it names that command.</p>
          <pre>${esc((page.proxmox_token_commands || []).join("\n"))}</pre>
          <div class="actions"><button type="button" class="sm" data-act="copy-cmds">Copy</button></div>
          <p class="note">A root token works too (<code>pveum user token add root@pam simrack --privsep 0</code>), but it may change anything on the host.</p>
        </details>
      </fieldset>
      <fieldset class="group">
        <legend>Mist ${mist.set ? `<span class="chip ok">Token saved</span>` : `<span class="chip plain">optional</span>`}</legend>
        <label class="f"><span>Cloud</span><select name="mist_api" class="mono">${clouds}</select></label>
        <label class="f"><span>API token${mist.set ? keep : ""}</span>
          <input name="mist_token" type="password" autocomplete="off" spellcheck="false" class="mono"></label>
        <p class="note">Make one in Mist under Organization › Settings › API Token. With Super User or Network Admin, SimRack can build Mist sites; with Observer, it only looks. A new cloud needs the token pasted again.</p>
      </fieldset>
      <div class="form-err" data-setup-tokens-msg role="alert" hidden></div>
      <div class="actions"><button class="primary" type="submit" data-op="save-tokens" data-read data-ask="setup" data-ask-always data-ask-why="SimRack keeps tokens in its state folder, readable by its own user only, and sends each only to the address beside it." data-busy-label="Saving…">Save connection</button><span class="note ok" data-setup-tokens-ok role="status"></span></div>
    </form>`;
}
function saveTokens(f, btn) {
  const page = setupPage; if (!page) return;
  const box = $("[data-setup-tokens-msg]", f), v = n => f.elements[n].value.trim();
  /* an address goes only when it changed; blank keeps the saved one */
  const body = {
    proxmox: v("proxmox_token"), mist: v("mist_token"),
    proxmox_api: bare(v("proxmox_api")) === bare(page.tokens.proxmox.api) ? "" : v("proxmox_api"),
    mist_api: bare(v("mist_api")) === bare(page.tokens.mist.api) ? "" : v("mist_api"),
  };
  showErr(box, ""); $("[data-setup-tokens-ok]", f).textContent = "";
  for (const [k, label] of [["proxmox", "Proxmox"], ["mist", "Mist"]]) {
    if (body[k + "_api"] && !body[k]) {
      showErr(box, `Paste the ${label} token along with its new address.`, "SimRack never sends a saved token to a new address unless it is pasted again.");
      f.elements[k + "_token"].focus(); return;
    }
  }
  if (!body.proxmox && !body.mist) { showErr(box, "Paste a token to save."); f.elements.proxmox_token.focus(); return; }
  const label = body.proxmox && body.mist ? "Save the Proxmox and Mist tokens" : body.proxmox ? "Save the Proxmox token" : "Save the Mist token";
  run(btn, label, async () => {
    try { return await api("/api/setup/tokens", body); } catch (e) { showErr(box, e.message, e.detail); throw e; }
  }, async () => {
    for (const n of ["proxmox_token", "mist_token"]) f.elements[n].value = "";
    if (body.proxmox) { const d = $('details[data-key="pve-token"]', f); if (d) d.open = false; }
    await loadSetup();
    const ok = $("[data-setup-tokens-ok]"); if (ok) ok.textContent = "Saved. SimRack uses it from now on.";
  });
}
function labHtml(page) {
  const p = page.profile || {}, found = page.found || {}, tok = page.tokens;
  const sec = k => (p[k] && typeof p[k] === "object" && !Array.isArray(p[k]) ? p[k] : {});
  const val = v => (v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
  const px = sec("proxmox"), mi = sec("mist"), mg = sec("management"), pr = sec("protected"), sb = sec("sandbox"), as = sec("assistants");
  const field = (name, label, v, more) => `<label class="f"><span>${label}</span><input name="${name}" class="mono" spellcheck="false" value="${esc(val(v))}"${more || ""}></label>`;
  const range = (key, label, v) => {
    const [a, b] = Array.isArray(v) ? v : [v];
    return `<div class="f" role="group" aria-labelledby="lbl-${key}"><span id="lbl-${key}">${label}</span><div class="range">
      <input name="sandbox.${key}" class="mono" inputmode="numeric" aria-label="First" placeholder="default" value="${esc(val(a))}"><span class="faint">to</span>
      <input name="sandbox.${key}.last" class="mono" inputmode="numeric" aria-label="Last" placeholder="default" value="${esc(val(b))}"></div></div>`;
  };
  /* "not found now" only when the source answered; "new" only against a profile the user saved */
  const answered = {
    proxmox: tok.proxmox.set && ["vmids", "lxc", "bridges", "subnets"].some(k => (found[k] || []).length),
    mist: tok.mist.set && (found.mist_sites || []).length > 0,
  };
  const protect = key => {
    const listed = Array.isArray(pr[key]), saved = listed ? pr[key].map(String) : [], src = key === "mist_sites" ? "mist" : "proxmox";
    const items = (found[key] || []).map(x => ({ id: String(x.id), label: x.label || "", found: true }));
    for (const id of saved) if (!items.some(x => x.id === id)) items.push({ id, label: "", found: false });
    const tick = x => {
      const on = saved.includes(x.id);
      const chip = !x.found && answered[src] ? ` <span class="chip warn plain">not found now</span>`
        : x.found && page.done && !on ? ` <span class="chip accent plain">new</span>` : "";
      const text = src === "mist" ? `${esc(x.label || x.id)}${chip}${x.label ? `<span class="id">${esc(x.id)}</span>` : ""}`
        : `<span class="mono">${esc(x.id)}</span> <span class="faint">${esc(x.label)}</span>${chip}`;
      return `<label class="tick"><input type="checkbox" name="tick:${key}:${esc(x.id)}" data-protect="${key}" value="${esc(x.id)}"${on ? " checked" : ""}><span>${text}</span></label>`;
    };
    return `<fieldset class="protect"><legend>${PROTECT[key]}</legend>
      ${items.length ? `<div class="ticks">${items.map(tick).join("")}</div>`
        : `<p class="note">${tok[src].set ? "None found." : `SimRack lists these once it has a ${src === "mist" ? "Mist" : "Proxmox"} token.`}</p>`}
      <label class="f"><span>Also protect <span class="faint">· separate with commas</span></span><input name="also:${key}" class="mono" spellcheck="false" value="${esc(listed ? "" : val(pr[key]))}"></label>
    </fieldset>`;
  };
  return `<h2 class="sec" id="sec-lab">The lab</h2>
    <form class="fields" data-form="setup" autocomplete="off" novalidate>
      <fieldset class="group"><legend>Where SimRack builds</legend>
        <div class="fields two">
          ${field("proxmox.node", "Proxmox node", px.node)}
          ${field("mist.org_id", `Mist org ID <span class="faint">· needed for Mist changes</span>`, mi.org_id)}
        </div>
      </fieldset>
      <fieldset class="group"><legend>Management <span class="faint">· where each switch's fxp0 connects</span></legend>
        <div class="fields two">
          ${field("management.bridge", "Bridge", mg.bridge, ` list="setup-bridges"`)}
          ${field("management.vlan", `VLAN <span class="faint">· blank for untagged</span>`, mg.vlan, ` inputmode="numeric"`)}
          ${field("management.cidr", "Subnet", mg.cidr, ` placeholder="192.0.2.0/24"`)}
          ${field("management.pool", "Addresses the switches get", mg.pool, ` placeholder="192.0.2.200-192.0.2.249"`)}
        </div>
        <datalist id="setup-bridges">${(found.bridges || []).map(b => `<option value="${esc(b.id)}">${esc(b.label)}</option>`).join("")}</datalist>
      </fieldset>
      <fieldset class="group"><legend>Live <span class="faint">· SimRack never touches what is ticked</span></legend>
        ${Object.keys(PROTECT).map(protect).join("")}
      </fieldset>
      <fieldset class="group"><legend>Sandboxes</legend>
        <div class="fields two">${range("vmids", "VM IDs", sb.vmids)}${range("lxc", "Container IDs", sb.lxc)}</div>
        <details data-key="sbx-adv"><summary>Advanced</summary>
          <div class="fields two">
            ${field("sandbox.bridge_prefix", "Bridge name prefix", sb.bridge_prefix, ` placeholder="sbx"`)}
            ${field("sandbox.park_bridge", "Parking bridge", sb.park_bridge, ` placeholder="${esc((sb.bridge_prefix || "sbx") + "park")}"`)}
          </div>
        </details>
      </fieldset>
      <fieldset class="group"><legend>Assistants <span class="faint">· through SimRack's MCP server</span></legend>
        <p class="note">An assistant connected to SimRack can look, build sandboxes and change them. These undo work, so the MCP server leaves them out unless you tick them. The tick only hides tools: anything holding SimRack's token can still use its API.</p>
        <label class="check"><input type="checkbox" name="assistants.risky"${as.risky === true ? " checked" : ""}> Also let it tear down sandboxes, revert, delete switches and type at a switch's console</label>
      </fieldset>
      <div class="form-err" data-setup-msg role="alert" hidden></div>
      <div class="actions">
        <button class="primary" type="submit" data-op="save-lab" data-read data-ask="setup" data-ask-always data-ask-why="SimRack never touches anything ticked, and builds only in the sandbox ranges." data-busy-label="Saving…">Save lab profile</button>
        <button class="ghost" type="button" data-act="setup-export" data-read${page.done ? "" : ` data-why="Nothing is saved yet"`}>Export</button>
        <button class="ghost" type="button" data-act="setup-import" data-read data-ask="setup" data-ask-always data-ask-why="The file replaces the saved lab profile.">Import…</button>
        <span class="note ok" data-setup-ok role="status"></span>
      </div>
      <input type="file" accept=".toml,text/plain" data-setup-file hidden>
    </form>`;
}
/* the form as a profile: a blank is left out, so the server names what is required or takes its default */
function setupBody(f) {
  const t = n => (f.elements[n] ? f.elements[n].value.trim() : "");
  const opt = n => t(n) || null;
  const num = s => (/^\d+$/.test(s) ? Number(s) : s);
  const range = k => { const a = t(`sandbox.${k}`), b = t(`sandbox.${k}.last`); return a || b ? [num(a), num(b)] : null; };
  const live = {};
  for (const key of Object.keys(PROTECT)) {
    const ids = $$(`input[data-protect="${key}"]:checked`, f).map(x => x.value).concat(t(`also:${key}`).split(/[\s,]+/).filter(Boolean));
    live[key] = [...new Set(ids)].map(key === "vmids" || key === "lxc" ? num : String);
  }
  return {
    proxmox: { node: opt("proxmox.node") },
    mist: { org_id: opt("mist.org_id") },
    management: { bridge: opt("management.bridge"), vlan: t("management.vlan") ? num(t("management.vlan")) : null, cidr: opt("management.cidr"), pool: opt("management.pool") },
    protected: live,
    sandbox: { vmids: range("vmids"), lxc: range("lxc"), bridge_prefix: opt("sandbox.bridge_prefix"), park_bridge: opt("sandbox.park_bridge") },
    assistants: { risky: !!(f.elements["assistants.risky"] && f.elements["assistants.risky"].checked) },
  };
}
/* the server names the setting it refused, such as management.pool: mark that field and go to it */
function markSetupErr(f, msg) {
  const m = /\b(proxmox|mist|management|protected|sandbox|assistants)\.(\w+)/.exec(msg || ""); if (!m) return;
  const x = f.elements.namedItem(m[1] === "protected" ? `also:${m[2]}` : `${m[1]}.${m[2]}`);
  if (!x || !x.tagName) return;
  const d = x.closest("details"); if (d) d.open = true;
  x.setAttribute("aria-invalid", "true");
  x.focus();
}
function saveProfile(f, btn) {
  const box = $("[data-setup-msg]", f), body = { profile: setupBody(f) };
  showErr(box, ""); $("[data-setup-ok]", f).textContent = "";
  for (const x of $$("[aria-invalid]", f)) x.removeAttribute("aria-invalid");
  let got = null;
  run(btn, "Save the lab profile", async () => {
    try { got = await api("/api/setup", body); return got.profile; }
    catch (e) { showErr(box, e.message, e.detail); markSetupErr(f, e.message); throw e; }
  }, () => setupSaved(got, "Saved. SimRack uses this profile from now on."));
}
function setupSaved(page, said) {
  if (!page) return;
  setupPage = page; updateSetup(true);
  const out = (page.left_out || []).map((x) => ` Left out ${x.key}: ${x.why}`).join("")
    + (page.partly_protected || []).map((x) => ` ${x.sandbox} is now partly protected, so SimRack leaves ${x.parts.join(", ")} alone.`).join("");
  const ok = $("[data-setup-ok]"); if (ok) ok.textContent = said + out;
  $('[data-op="save-lab"]')?.focus({ preventScroll: true });
}
async function importProfile(input) {
  const file = input.files[0], f = input.form; if (!file || !f) return;
  const btn = $('[data-act="setup-import"]', f), box = $("[data-setup-msg]", f);
  showErr(box, ""); $("[data-setup-ok]", f).textContent = "";
  let text;
  try { text = await file.text(); } catch (e) { showErr(box, `Couldn't read ${file.name}: ${e.message}`); return; } finally { input.value = ""; }
  let got = null;
  run(btn, `Import ${file.name}`, async () => {
    try { got = await api("/api/setup", { toml: text }); return got.profile; } catch (e) { showErr(box, e.message, e.detail); throw e; }
  }, () => setupSaved(got, `Imported ${file.name}. SimRack uses it from now on.`));
}
async function exportProfile(btn) {
  const f = btn.form, box = f && $("[data-setup-msg]", f), ok = f && $("[data-setup-ok]", f);
  showErr(box, ""); if (ok) ok.textContent = "";
  try {
    const { toml } = await api("/api/setup/export");
    const url = URL.createObjectURL(new Blob([toml], { type: "application/toml" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "lab-profile.toml" });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    if (ok) ok.textContent = "Exported lab-profile.toml.";
    note("ok", "Exported the lab profile");
  } catch (e) { showErr(box, "Couldn't export the lab profile: " + e.message, e.detail); note("err", "Couldn't export the lab profile: " + e.message); }
}

/* ---------- import: read a fabric's design from Mist ---------- */
function importShell() {
  return `<div class="intake">
    <header class="intake-head">
      <h1 tabindex="-1">Import a fabric from Mist</h1>
      <p>Give SimRack an EVPN topology from Mist and it drafts a sandbox with the same switches and cabling. You see what a copy would take, then build it. Importing builds nothing and sends nothing to Mist.</p>
    </header>
    <div class="intake-grid">
      <section aria-labelledby="sec-bring">
        <h2 class="sec" id="sec-bring">What to bring</h2>
        <div id="srcs"></div>
      </section>
      <section class="panel drop" data-drop aria-label="Mist JSON">
        <form class="fields" data-form="import" autocomplete="off">
          <label class="f"><span>Paste the JSON <span class="faint">· one response, or several back to back</span></span>
            <textarea name="paste" class="mono" spellcheck="false" placeholder="{ &quot;name&quot;: &quot;campus-fabric&quot;,&#10;  &quot;switches&quot;: [ … ],&#10;  &quot;evpn_options&quot;: { … } }"></textarea></label>
          <div class="or" aria-hidden="true">or</div>
          <label class="f"><span>Choose saved files, or drop them on this panel</span><input type="file" name="upload" multiple accept=".json,application/json"></label>
          <label class="f"><span>Shape name <span class="faint">· optional</span></span><input name="shape" class="mono" pattern="[a-z0-9][a-z0-9\\-]{1,30}[a-z0-9]" title="Lowercase letters, digits and hyphens, 3 to 32 characters" placeholder="taken from the topology name"></label>
          <div class="form-err" data-import-msg role="alert" hidden></div>
          <div class="actions"><button class="primary" type="submit" data-busy-label="Reading…">Read fabric</button><button type="button" class="ghost" data-act="cancel-import">Cancel</button></div>
        </form>
      </section>
    </div>
  </div>`;
}
function updateImport() {
  const base = String((S.mist && S.mist.api_base) || "").replace(/\/+$/, ""), site = S.production.mist_sites[0] || "";
  const link = (path, text) => base && site
    ? `<a href="${esc(`${base}/sites/${site}/${path}`)}" target="_blank" rel="noopener noreferrer"><span class="mono">${esc(text)}</span><span class="ext" aria-hidden="true">↗</span></a>`
    : `<code>${esc(text)}</code>`;
  swap($("#srcs"), `<ul class="srcs">
      <li><span class="what">EVPN topology <span class="chip accent plain">needed</span></span>${link("evpn_topologies", "evpn_topologies")}
        <p>The list shows each topology's id. Open <code>evpn_topologies/&lt;id&gt;</code> for the one you want: the list leaves the switches out.</p></li>
      <li><span class="what">Switches <span class="chip plain">optional</span></span>${link("devices?type=switch", "devices?type=switch")}
        <p>Names the switches, so the plan shows which live switch each one copies. Port stats need the names to match LLDP neighbours.</p></li>
      <li><span class="what">Port stats <span class="chip plain">optional</span></span>${link("stats/ports/search?limit=1000&duration=1d", "stats/ports/search")}
        <p>A day of LLDP neighbours gives the exact cabling. Without it, ports are a best guess from the port config.</p></li>
    </ul>
    <p class="note">${base && site ? "Each link opens the JSON when this browser is signed in to Mist. Save it or copy it. They read the live site; for another site, change the id in the address." : "Fetch these from the Mist API for the site you want."}</p>`);
}

/* several JSON documents pasted back to back: split them where the brackets close */
function splitJson(text) {
  const out = []; let depth = 0, start = -1, inStr = false, escaped = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inStr) { if (escaped) escaped = false; else if (c === "\\") escaped = true; else if (c === '"') inStr = false; continue; }
    if (c === '"') inStr = true;
    else if (c === "{" || c === "[") { if (depth++ === 0) start = i; }
    else if (c === "}" || c === "]") { if (--depth < 0) return []; if (depth === 0) out.push(text.slice(start, i + 1)); }
    else if (depth === 0 && !/[\s,]/.test(c)) return [];
  }
  return depth === 0 && !inStr ? out : [];
}
function readJson(text, label) {
  try { return [JSON.parse(text)]; } catch (first) {
    const parts = splitJson(text);
    if (parts.length < 2) throw new Error(`${label} is not JSON: ${first.message}`);
    return parts.map((p, i) => { try { return JSON.parse(p); } catch (e) { throw new Error(`${label}, part ${i + 1}, is not JSON: ${e.message}`); } });
  }
}
function errHtml(text, detail) {
  return `<p>${esc(text)}</p>${detail ? `<p>${esc(typeof detail === "string" ? detail : JSON.stringify(detail))}</p>` : ""}`;
}
function showErr(box, text, detail) {
  if (!box) return;
  box.hidden = !text;
  box.innerHTML = text ? errHtml(text, detail) : "";
}
async function importShape(f, btn) {
  const box = $("[data-import-msg]", f), docs = [], problems = [];
  showErr(box, "");
  const pasted = f.paste.value.trim();
  if (pasted) { try { docs.push(...readJson(pasted, "The pasted text")); } catch (e) { problems.push(e.message); } }
  for (const file of f.upload.files) {
    try { docs.push(...readJson(await file.text(), file.name)); } catch (e) { problems.push(e.message); }
  }
  if (problems.length) { showErr(box, problems[0], problems.slice(1).join(" ")); return; }
  if (!docs.length) { showErr(box, "Paste the topology JSON, or choose its file, first."); f.paste.focus(); return; }
  const body = { documents: docs }, name = f.shape.value.trim();
  if (name) body.name = name;
  await run(btn, "Import shape", async () => {
    try { return await api("/api/shapes", body); } catch (e) { showErr(box, e.message, e.detail); throw e; }
  }, r => { if (r && r.name) setView("shape:" + r.name); });
}

/* ---------- a shape: a planned fabric, drawn but not built ---------- */
const planOf = sh => ({ name: sh.name, nodes: sh.nodes.map(n => ({ ...n, running: false, kind: "switch" })), links: sh.links });
const when = t => {
  const d = new Date(t || "");
  if (isNaN(d)) return String(t || "").replace("T", " ").slice(0, 16);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
/* With room for fewer switches than the shape has, tick a slice that keeps every tier:
   a switch from each tier in turn, cabled to one already ticked where it can be. */
function defaultPicks(sh, room) {
  const all = sh.nodes.map(n => n.name);
  if (room == null || room >= all.length) return new Set(all);
  const known = new Set(TIERS.flatMap(t => t.roles)), nbr = new Map(all.map(n => [n, []]));
  for (const l of sh.links) if (nbr.has(l.a_node) && nbr.has(l.b_node)) { nbr.get(l.a_node).push(l.b_node); nbr.get(l.b_node).push(l.a_node); }
  const tiers = [...TIERS.map(t => sh.nodes.filter(n => t.roles.includes(n.role))), sh.nodes.filter(n => !known.has(n.role))].filter(g => g.length);
  const set = new Set(), limit = Math.max(1, room);
  for (let more = true; more && set.size < limit;) {
    more = false;
    for (const g of tiers) {
      const left = g.filter(n => !set.has(n.name));
      if (set.size >= limit || !left.length) continue;
      set.add((left.find(n => nbr.get(n.name).some(m => set.has(m))) || left[0]).name); more = true;
    }
  }
  return set;
}
/* the ticked switches; until the user changes a tick they follow the room on the host */
function picksFor(sh) {
  const p = picks[sh.name];
  if (p && p.touched) { for (const x of p.set) if (!sh.nodes.some(n => n.name === x)) p.set.delete(x); return p.set; }
  const set = defaultPicks(sh, roomNow());
  picks[sh.name] = { set, touched: false };
  return set;
}
/* the same rules as shapes.plan_build, so the page shows the cables a build keeps */
function planBuild(sh, chosen) {
  const ports = S.limits.switch_ports || 10, last = "ge-0/0/" + (ports - 1), used = new Set(), kept = [], dropped = [];
  for (const l of sh.links) {
    const a = l.a_node, b = l.b_node;
    if (!chosen.has(a) && !chosen.has(b)) continue;
    let reason = !chosen.has(a) ? `${a} not built` : !chosen.has(b) ? `${b} not built` : a === b ? `${a} is cabled to itself` : "";
    if (!reason) for (const port of [l.a_port, l.b_port]) {
      const m = /^ge-0\/0\/(\d+)$/.exec(String(port));
      if (!m) { reason = `${port} is not a sandbox port`; break; }
      if (Number(m[1]) >= ports) { reason = `${port} is past ${last}`; break; }
    }
    if (!reason) for (const [node, port] of [[a, l.a_port], [b, l.b_port]]) if (used.has(node + " " + port)) { reason = `${node} ${port} already cabled`; break; }
    if (reason) { dropped.push({ link: l, reason }); continue; }
    used.add(a + " " + l.a_port).add(b + " " + l.b_port);
    kept.push(l);
  }
  return { kept, dropped };
}
function shapeDiagram(sh, intro) {
  const chosen = picksFor(sh), pb = planBuild(sh, chosen);
  return diagram(planOf(sh), { plan: true, intro, skip: new Set(sh.nodes.map(n => n.name).filter(x => !chosen.has(x))),
    kept: new Set(pb.kept), why: new Map(pb.dropped.map(d => [d.link, d.reason])) });
}
function shapeShell(sh) {
  return `
  <section class="stage" aria-label="Planned fabric">
    <div class="stage-head">
      <div class="title"><h1 id="h-name"></h1><p id="h-desc"></p></div>
      <div class="meta" id="h-meta"></div>
    </div>
    <div class="canvas roomy" id="canvas"></div>
    <div class="stage-foot">
      <div class="legend"><span><i class="dot ring"></i>planned switch</span><span><i class="plan"></i>cable LLDP saw</span><span><i class="plan guess"></i>port guessed</span><span id="lg-out" hidden><i class="dot ring out"></i>not in this build</span></div>
      <span id="fabsum"></span>
    </div>
  </section>
  <div class="plan-body">
    <div class="plan-main">
      <section aria-labelledby="sec-sw"><h2 class="sec" id="sec-sw">Switches <small id="swcount"></small></h2><div id="swmap"></div></section>
      <section aria-labelledby="sec-cab"><h2 class="sec" id="sec-cab">Cables <small id="cabcount"></small></h2><div id="cabmap"></div></section>
    </div>
    <aside class="plan-side">
      <section aria-labelledby="sec-build"><h2 class="sec" id="sec-build">Build a sandbox</h2>
        <div id="room"></div>
        <form class="fields build" data-form="build" data-shape="${esc(sh.name)}" autocomplete="off">
          <label class="f">Sandbox name<input name="name" class="mono" required maxlength="32" pattern="[a-z0-9][a-z0-9\\-]{1,30}[a-z0-9]" value="${esc(freeName(sh.name))}" title="3 to 32 lowercase letters, digits and hyphens, starting and ending with a letter or digit"></label>
          <label class="f">Boot from<select name="boot" class="mono" data-boot></select></label>
          <p class="note" data-boot-note></p>
          <label class="check"><input type="checkbox" name="start" checked> Start switches after building</label>
          <label class="check"><input type="checkbox" name="mist" disabled> Create a Mist site for it</label>
          <p class="note" data-mist-why hidden></p>
          <div class="form-err" data-build-msg role="alert" hidden></div>
          <div class="actions"><button class="primary" type="submit" data-write data-ask="build" data-build data-busy-label="Building…">Build</button></div>
        </form>
      </section>
      <section aria-labelledby="sec-src"><h2 class="sec" id="sec-src">From Mist</h2><div id="srcfacts"></div></section>
      <section aria-labelledby="sec-notes"><h2 class="sec" id="sec-notes">Notes <small id="notecount"></small></h2><div id="notes"></div></section>
      <div class="forget">
        <p class="note">Deleting forgets SimRack's copy of this plan. Mist is not touched, and you can import it again.</p>
        <div class="form-err" id="shapemsg" role="alert" hidden></div>
        <button class="danger" data-act="del-shape" data-name="${esc(sh.name)}" data-ask="delete" data-ask-always data-ask-why="Only SimRack's saved copy goes. Nothing in the lab or in Mist changes." data-busy-label="Deleting…">Delete shape</button>
      </div>
    </aside>
  </div>`;
}
function updateShape(sh) {
  const src = sh.source || {}, ev = sh.evpn || {}, host = S.host.node || "the host";
  const n = sh.nodes.length, total = sh.links.length, per = switchMb(), head = headroomMb(), room = roomNow();
  const chosen = picksFor(sh), k = chosen.size, need = k * per, short = head == null ? 0 : Math.max(0, need - Math.max(0, head));
  const whole = head == null || n * per <= head, pb = planBuild(sh, chosen), keep = new Set(pb.kept), why = new Map(pb.dropped.map(d => [d.link, d.reason]));
  $("#h-name").textContent = sh.name;
  $("#h-desc").textContent = `${sh.kind}, read from the Mist topology ${src.topology || "(unnamed)"}${src.imported_at ? " on " + when(src.imported_at) : ""}.`;
  const cabling = sh.ports_source === "lldp" ? `<span class="chip ok">Cabling from LLDP</span>`
    : `<span class="chip warn">${sh.ports_source === "mixed" ? "Some ports guessed" : "Ports guessed"}</span>`;
  swap($("#h-meta"), `${cabling}<span class="chip ${head == null ? "" : whole ? "ok" : "warn"}">${head == null ? "Room unknown" : whole ? "Fits " + esc(host) : "Too big for " + esc(host)}</span>`);
  $("#fabsum").textContent = [plural(n, "switch", "switches"), plural(total, "cable"), "about " + gb(n * per)].join(" · ");
  const intro = introPending; introPending = false;
  swap($("#canvas"), shapeDiagram(sh, intro));
  $("#lg-out").hidden = k === n;

  $("#swcount").textContent = k === n ? n : `${k} of ${n} ticked`;
  $("#cabcount").textContent = keep.size === total ? total : `${keep.size} of ${total} made`;
  const livePorts = name => sh.links.flatMap(l => [l.a_node === name && l.a_live_port, l.b_node === name && l.b_live_port]).filter(Boolean);
  swap($("#swmap"), `<div class="scroll-y"><table><thead><tr><th><label class="tick"><input type="checkbox" data-tick="*" aria-label="Tick every switch"${k === n ? " checked" : ""}>Sandbox switch</label></th><th>Copies</th><th class="role">Role</th><th class="pod">PoD</th><th>Fabric ports</th></tr></thead><tbody>${
    sh.nodes.map(x => { const was = livePorts(x.name), on = chosen.has(x.name);
      return `<tr${on ? "" : ` class="off"`}><td class="nm"><label class="tick"><input type="checkbox" data-tick="${esc(x.name)}"${on ? " checked" : ""}><b>${esc(x.name)}</b></label></td>
        <td class="from">${esc(x.from)}</td><td class="role muted">${esc(x.role)}</td><td class="pod muted">${esc(x.pod || "–")}</td>
        <td class="mono">${esc(portRanges(x.ports) || "none")}${was.length ? `<span class="was">live ${esc(was.join(", "))}</span>` : ""}</td></tr>`; }).join("")}</tbody></table></div>`);
  const every = $('[data-tick="*"]'); if (every) every.indeterminate = k > 0 && k < n;
  swap($("#cabmap"), total ? `<ul class="patch plan scroll-y">${sh.links.map(l => {
    const seen = l.via === "lldp", live = l.a_live_port || l.b_live_port, out = !keep.has(l);
    const note = [out ? "left out: " + (why.get(l) || "neither end built") : "", seen || sh.ports_source !== "mixed" ? "" : "ports guessed",
      live ? `live ${l.a_live_port || l.a_port} ↔ ${l.b_live_port || l.b_port}` : ""].filter(Boolean).join(" · ");
    return `<li${out ? ` class="out"` : ""}><i class="wire plan${seen ? "" : " guess"}" aria-hidden="true"></i>
      <span class="ends"><span class="end">${esc(l.a_node)} <span class="mono muted">${esc(l.a_port)}</span></span> <span class="end">↔ ${esc(l.b_node)} <span class="mono muted">${esc(l.b_port)}</span></span></span>${
      note ? `<span class="br">${esc(note)}</span>` : ""}</li>`; }).join("")}</ul>`
    : `<p class="hint">No cables between these switches.</p>`);

  const cut = pb.dropped.filter(d => chosen.has(d.link.a_node) && chosen.has(d.link.b_node)), away = total - keep.size - cut.length, judged = k > 0 && head != null;
  const tip = head == null ? "" : room === 0 ? `There's no room on ${host} for even one switch right now.`
    : short ? `Untick ${plural(k - room, "switch", "switches")} to fit, or free memory on ${host}.`
    : !whole && !picks[sh.name].touched ? `All ${n} won't fit on ${host}, so a slice is ticked: a switch from each tier in turn, cabled to one another where possible. Change the ticks to swap switches in or out.` : "";
  swap($("#room"), `<dl class="ledger">
      <div><dt>Switches</dt><dd><b>${k}</b> of ${n} ticked · room for <b>${room == null ? "?" : room}</b> on ${esc(host)} now</dd>
        <dd class="verdict ${short ? "bad" : "ok"}">${judged ? short ? (k - room) + " over" : "fits" : ""}</dd></div>
      <div><dt>Memory</dt><dd><b>${gb(need)}</b> for ${plural(k, "switch", "switches")} at ${gb(per)} each · ${head == null ? "free memory unknown"
        : `<b>${gb(Math.max(0, head))}</b> free above the ${gb(S.limits.min_free_ram_mb)} reserve`}</dd>
        <dd class="verdict ${short ? "bad" : "ok"}">${judged ? short ? gb(short) + " short" : "fits" : ""}</dd></div>
      <div><dt>Cables</dt><dd>${total ? `<b>${keep.size}</b> of ${total} made${away ? ` · ${away} ${away === 1 ? "goes to a switch" : "go to switches"} left out` : ""}${
        cut.length ? ` · ${cut.length} can't be made` : ""}` : "none in this shape"}</dd>
        <dd class="verdict ok">${total && keep.size === total ? "all" : ""}</dd></div>
    </dl>${tip ? `<p class="note">${esc(tip)}</p>` : ""}${cut.length ? `<p class="subhead">Cables that can't be made</p><ul class="notes">${cut.map(d =>
      `<li>${esc(d.link.a_node)} <span class="mono">${esc(d.link.a_port)}</span> ↔ ${esc(d.link.b_node)} <span class="mono">${esc(d.link.b_port)}</span>: ${esc(d.reason)}</li>`).join("")}</ul>` : ""}`);
  const f = $('[data-form="build"]'), can = mistWritesOn();
  fillSelect(f.boot, bootOptions()); updateBootNote(f);
  if (f.mist.disabled === can) { f.mist.disabled = !can; f.mist.checked = can; }
  const mw = $("[data-mist-why]", f);
  mw.textContent = can || !writesOn() ? "" : !S.mist.configured ? "No Mist token yet, so the sandbox starts without a site."
    : `${S.mist.read_only_reason || "Mist changes are off."} The sandbox starts without a site.`;
  mw.hidden = !mw.textContent;
  const b = $("[data-build]", f);
  b.dataset.why = !k ? "Tick at least one switch" : short ? `Not enough memory: ${gb(short)} short` : "";
  if (b.getAttribute("aria-busy") !== "true") b.textContent = k ? "Build " + plural(k, "switch", "switches") : "Build";

  const site = src.site_id, liveSite = !!site && S.production.mist_sites.includes(site);
  swap($("#srcfacts"), `<dl class="facts">
      ${ev.routed_at ? `<div><dt>Routed at</dt><dd>${esc(ev.routed_at)}</dd></div>` : ""}
      ${ev.overlay_as != null ? `<div><dt>Overlay AS</dt><dd class="mono">${esc(ev.overlay_as)}</dd></div>` : ""}
      ${ev.underlay_as_base != null ? `<div><dt>Underlay AS from</dt><dd class="mono">${esc(ev.underlay_as_base)}</dd></div>` : ""}
      ${sh.pods.length ? `<div><dt>PoDs</dt><dd>${sh.pods.map(p => esc(p.name)).join(", ")}</dd></div>` : ""}
      <div class="wide"><dt>Topology</dt><dd>${esc(src.topology || "unnamed")}${src.topology_id ? ` <span class="mono faint">${esc(src.topology_id)}</span>` : ""}</dd></div>
      ${site ? `<div class="wide"><dt>Mist site</dt><dd><span class="mono">${esc(site)}</span>${liveSite ? ` <span class="faint">· the live lab</span>` : ""}</dd></div>` : ""}
      ${src.modified ? `<div><dt>Changed in Mist</dt><dd>${esc(when(src.modified))}</dd></div>` : ""}
      ${src.imported_at ? `<div><dt>Imported</dt><dd>${esc(when(src.imported_at))}</dd></div>` : ""}
    </dl>`);
  $("#notecount").textContent = sh.notes.length || "";
  swap($("#notes"), sh.notes.length ? `<ul class="notes">${sh.notes.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="hint">Nothing to flag.</p>`);
}

function shell(s) {
  return `
  <section class="stage" aria-label="Fabric">
    <div class="stage-head">
      <div class="title"><h1 id="h-name"></h1><p id="h-desc"></p></div>
      <div class="meta" id="h-meta"></div>
    </div>
    <div class="canvas" id="canvas"></div>
    <div class="stage-foot">
      <div class="legend"><span><i class="dot"></i>running</span><span><i class="dot off"></i>stopped</span><span><i></i>cable, both ends up</span><span><i class="down"></i>an end is down</span></div>
      <span id="fabsum"></span>
    </div>
  </section>
  <section class="strip" id="inspector" aria-label="Selected item" hidden></section>

  <div class="bench">
    <section aria-labelledby="sec-guests">
      <h2 class="sec" id="sec-guests">Guests <small id="guestcount"></small></h2>
      <div id="guests"></div>
      <div class="tray">
        <p class="subhead">Add a guest</p>
        <form class="fields row" data-form="add-node" autocomplete="off">
          <label class="f">Name<input name="node" required placeholder="sbx-acc-03"></label>
          <label class="f">Type<select name="type">
            <option value="switch:access">Access switch</option><option value="switch:distribution">Distribution switch</option>
            <option value="switch:core">Core switch</option><option value="switch:border">Border switch</option>
            <option value="vsrx:vsrx">vSRX</option><option value="image:client">Test host</option></select></label>
          <label class="f">Boot from<select name="boot" class="mono" data-boot></select></label>
          <button type="submit" data-write data-ask="build" data-busy-label="Adding…">Add guest</button>
        </form>
      </div>
    </section>

    <section aria-labelledby="sec-cables">
      <h2 class="sec" id="sec-cables">Cables <small id="cablecount"></small></h2>
      <div id="cables"></div>
      <div class="tray">
        <p class="subhead">Plug in a cable</p>
        <form class="fields" data-form="cable" autocomplete="off">
          <div class="fields pair"><label class="f">From<select name="a_node" data-node-sel data-port-target="a_port"></select></label>
            <label class="f">Port<select name="a_port" class="mono" data-port-for="a_node"></select></label></div>
          <div class="fields pair"><label class="f">To<select name="b_node" data-node-sel data-port-target="b_port"></select></label>
            <label class="f">Port<select name="b_port" class="mono" data-port-for="b_node"></select></label></div>
          <div class="actions"><button type="submit" data-write data-ask="cable" data-busy-label="Plugging in…">Plug in</button></div>
        </form>
        <p class="note" style="margin-top:.75rem">Each cable is its own MTU 9216 bridge on the Proxmox host. LLDP and LACP pass through.</p>
      </div>
    </section>
  </div>

  <section class="join" id="joinsec" aria-labelledby="sec-join" hidden>
    <h2 class="sec" id="sec-join">Join Mist <small id="joincount"></small></h2>
    <ol class="steps" id="join"></ol>
  </section>

  <div class="house">
    <section aria-labelledby="sec-revert">
      <h2 class="sec" id="sec-revert">Revert points</h2>
      <p class="subhead">Proxmox, guest disks</p>
      <form class="fields inline" data-form="pve-snap" autocomplete="off">
        <label class="f">Label<input name="label" class="mono" required pattern="[A-Za-z][A-Za-z0-9_\\-]{0,39}"></label>
        <button type="submit" data-write data-ask="snapshot" data-busy-label="Saving…">Save point</button>
      </form>
      <form class="fields inline" data-form="pve-revert" style="margin-top:.5rem">
        <label class="f">Saved points<select name="label" class="mono" data-pve-snaps></select></label>
        <button type="submit" data-write data-ask="revert" data-ask-always data-ask-why="Every guest goes back to the saved point. What changed since is lost.">Revert guests</button>
      </form>
      <p class="subhead" style="margin-top:1.75rem">Mist, site and device configs</p>
      <form class="fields inline" data-form="mist-snap" autocomplete="off">
        <label class="f">Label<input name="label" class="mono" required pattern="[A-Za-z][A-Za-z0-9_\\-]{0,39}"></label>
        <button type="submit" data-mist-read data-busy-label="Saving…">Save point</button>
      </form>
      <form class="fields inline" data-form="mist-revert" style="margin-top:.5rem">
        <label class="f">Saved points<select name="label" class="mono" data-mist-snaps></select></label>
        <button type="submit" data-write data-mist-write data-ask="revert" data-ask-always data-ask-why="The Mist site goes back to the saved point. What changed in Mist since is lost.">Revert Mist</button>
      </form>
    </section>

    <section aria-labelledby="sec-mist">
      <h2 class="sec" id="sec-mist">Mist <span id="mistsite"></span></h2>
      <div id="mist"></div>
    </section>

    <section class="activity" aria-labelledby="sec-log">
      <h2 class="sec" id="sec-log">Activity <small>this browser only</small></h2>
      <ul class="log" id="log"></ul>
    </section>
  </div>

  <section class="teardown" aria-labelledby="sec-tear">
    <h2 class="sec bad" id="sec-tear">Tear down</h2>
    <p class="note" style="margin-bottom:1rem">Stops and deletes every guest, then removes its bridges. If anything cannot be deleted, the sandbox stays listed so you can retry.</p>
    <form class="fields line" data-form="teardown" autocomplete="off">
      <label class="check"><input type="checkbox" name="keep_mist"> Keep its Mist site</label>
      <button type="submit" class="danger solid" data-write data-ask="teardown" data-ask-always data-busy-label="Tearing down…">Delete sandbox</button>
    </form>
  </section>`;
}

function updateSandbox(s) {
  $("#h-name").textContent = s.name;
  $("#h-desc").textContent = s.recipe.description || "";
  const up = s.nodes.filter(n => n.running).length, sw = s.nodes.filter(n => n.kind === "switch").length;
  swap($("#h-meta"), `<span class="chip plain">${esc(s.recipe.name)}</span>
    <span class="chip ${up && up === s.nodes.length ? "ok" : up ? "warn" : ""}">${up} of ${s.nodes.length} running</span>
    ${s.mist_site_id ? `<span class="chip mist" title="Mist site ${esc(s.mist_site_id)}">Mist site</span>` : `<span class="chip">No Mist site</span>`}`);
  $("#fabsum").textContent = [`${sw} switch${sw === 1 ? "" : "es"}`, `${s.links.length} cable${s.links.length === 1 ? "" : "s"}`,
    `about ${gb(sw * switchMb())}`, `created ${(s.created_at || "").replace("T", " ").slice(0, 16)}`].join(" · ");

  if (focusItem && focusItem.t === "node" && !s.nodes.some(n => n.name === focusItem.id)) focusItem = null;
  if (focusItem && focusItem.t === "cable" && !s.links.some(l => l.bridge === focusItem.id)) focusItem = null;
  swap($("#canvas"), diagram(s));
  const strip = $("#inspector"), detail = inspector(s);
  swap(strip, detail); strip.hidden = !detail;
  $("#guestcount").textContent = s.nodes.length;
  $("#cablecount").textContent = s.links.length;
  swap($("#guests"), s.nodes.length ? `<div class="scroll-y"><table><thead><tr><th>Guest</th><th class="vmid">VMID</th><th>Mgmt IP</th><th>State</th><th></th></tr></thead><tbody>${
    s.nodes.map(n => { const on = isSel("node", n.name); return `<tr class="clickable${on ? " sel" : ""}" data-act="focus-node" data-id="${esc(n.name)}">
      <td class="nm"><button class="link" data-act="focus-node" data-id="${esc(n.name)}" aria-expanded="${on}" aria-controls="inspector">${esc(n.name)}</button> <span class="faint role">${esc(n.role)}</span></td>
      <td class="mono num vmid">${n.vmid}</td><td class="mono">${esc(n.mgmt_ip || "–")}${planned(n)}</td>
      <td><span class="chip ${n.running ? "ok" : ""}">${n.running ? "running" : "stopped"}</span>${n.adopted_at ? ` <span class="chip mist" title="Adopted ${esc(when(n.adopted_at))}">in Mist</span>` : ""}</td>
      <td class="right">${n.running
        ? `<button class="sm" data-act="power" data-node="${esc(n.name)}" data-action="shutdown" data-write data-ask="power" data-busy-label="Stopping">Shut down</button>`
        : `<button class="sm" data-act="power" data-node="${esc(n.name)}" data-action="start" data-write data-ask="power" data-busy-label="Starting">Start</button>`}</td></tr>`; }).join("")}</tbody></table></div>`
    : `<p class="hint">No guests. Add one below.</p>`);
  swap($("#cables"), s.links.length ? `<ul class="patch scroll-y">${s.links.map(l => { const on = isSel("cable", l.bridge), ok = linkUp(s, l); return `<li class="${on ? "sel" : ""}" data-act="focus-cable" data-id="${esc(l.bridge)}">
      <i class="wire${ok ? "" : " down"}" aria-hidden="true"></i>
      <button class="link" data-act="focus-cable" data-id="${esc(l.bridge)}" aria-expanded="${on}" aria-controls="inspector"><span class="end">${esc(l.a_node)} <span class="mono muted">${esc(l.a_port)}</span></span> <span class="end">↔ ${esc(l.b_node)} <span class="mono muted">${esc(l.b_port)}</span></span></button>
      <span class="br">${esc(l.bridge)} · ${ok ? "up" : "an end is down"}</span>
      <button class="sm danger" data-act="unplug" data-bridge="${esc(l.bridge)}" data-write data-ask="cable">Unplug</button></li>`; }).join("")}</ul>`
    : `<p class="hint">No cables. Plug one in below, or select a switch in the diagram.</p>`);
  const sws = s.nodes.filter(n => n.kind === "switch");
  $("#joinsec").hidden = !sws.length;
  $("#joincount").textContent = sws.length ? `${sws.filter(n => n.adopted_at).length} of ${plural(sws.length, "switch", "switches")} in Mist` : "";
  swap($("#join"), sws.length ? joinSteps(s, sws) : "");

  for (const f of $$("[data-boot]")) fillSelect(f, bootOptions());
  for (const f of $$("[data-node-sel]")) fillSelect(f, nodeOptions(s));
  for (const f of $$("[data-port-for]")) {
    const nodeSel = f.form.elements[f.dataset.portFor];
    fillSelect(f, portOptions(s, nodeSel && nodeSel.value, null));
  }
  const pve = Object.keys(s.proxmox_snapshots || {}), mist = Object.keys(s.mist_snapshots || {});
  fillSelect($("[data-pve-snaps]"), pve.length ? pve.map(l => `<option>${esc(l)}</option>`).join("") : `<option value="">None saved yet</option>`);
  fillSelect($("[data-mist-snaps]"), mist.length ? mist.map(l => `<option>${esc(l)}</option>`).join("") : `<option value="">None saved yet</option>`);
  const stamp = "before-" + new Date().toISOString().slice(0, 16).replace(/[-:]/g, "").replace("T", "-");
  for (const f of $$('[data-form="pve-snap"] input, [data-form="mist-snap"] input')) if (!f.value && document.activeElement !== f) f.value = stamp;

  $("#mistsite").innerHTML = s.mist_site_id ? `<span class="chip mist">linked</span>` : "";
  swap($("#mist"), !S.mist.configured ? `<p class="hint">No Mist token yet, so Mist actions are off.</p>`
    : s.mist_site_id ? `<p class="hint" style="margin-bottom:.75rem">Site <span class="mono">${esc(s.mist_site_id)}</span></p>
      <div class="actions">
        <button data-act="mist" data-op="fabric" data-write data-mist-write data-ask="mist" data-busy-label="Building…">Build fabric</button>
        <button data-act="mist-get" data-op="health" data-mist-read data-busy-label="Checking…">Check health</button>
        <button data-act="mist-get" data-op="adopt" data-mist-read>Adoption command</button></div>
      <p class="note" style="margin-top:.75rem">Build fabric makes the site match the cables, after saving a revert point. Join Mist above walks through it. Results land in Activity.</p>`
    : sws.length ? `<p class="hint">No Mist site yet. Create it in Join Mist above.</p>`
    : `<p class="hint" style="margin-bottom:.75rem">This sandbox has no Mist site. Creating one makes an empty site named after it in your Mist org.</p>
      <button data-act="mist" data-op="site" data-write data-mist-write data-ask="mist" data-busy-label="Creating…">Create Mist site</button>`);
  renderLog();
}

/* ---------- Join Mist ---------- */
const siteName = s => s.mist_site_name || s.name + "-site";
const planned = n => n.kind === "switch" && n.mgmt_ip && !n.adopted_at
  ? ` <span class="planned" title="Planned. The switch takes its fxp0 address from DHCP; SimRack learns it when the switch is adopted.">planned</span>` : "";
function adoptBtn(s, n, sm) {
  const why = !s.mist_site_id ? "Create the sandbox's Mist site first" : !n.running ? "Start the switch first" : "";
  const cls = sm ? "sm" : !why && !n.adopted_at ? "primary" : "";
  return `<button${cls ? ` class="${cls}"` : ""}${sm ? ` aria-label="Adopt ${esc(n.name)}${n.adopted_at ? " again" : ""}"` : ""} data-act="adopt" data-node="${esc(n.name)}" data-write data-mist-write data-ask="mist" data-busy-label="Adopting…" data-why="${esc(why)}"
    data-tip="Joins ${esc(n.name)} to the sandbox's Mist site over its serial console. Takes about a minute.">${n.adopted_at ? sm ? "Again" : "Adopt again" : sm ? "Adopt" : "Adopt into Mist"}</button>`;
}
/* what the build sends, in the words of Mist's Campus Fabric wizard; the defaults are fabric.py's */
function fabricFacts(s) {
  const r = s.recipe, routed = s.nodes.some(n => n.role === "collapsed-core") ? "core"
    : ["core", "distribution", "edge"].includes(r.routed_at) ? r.routed_at : "edge";
  return [["Topology", r.topology_kind], ["Routed at", routed], ["Overlay AS", r.overlay_as], ["Underlay AS from", r.underlay_as_base || 65001]]
    .filter(([, v]) => v != null && v !== "");
}
function fabricRows(s) {
  const rank = n => { const i = TIERS.findIndex(t => t.roles.includes(n.role)); return i < 0 ? TIERS.length : i; };
  return s.nodes.filter(n => n.kind === "switch").sort((a, b) => rank(a) - rank(b)).map(n => ({ n, ports: s.links.flatMap(l =>
    l.a_node === n.name ? [{ port: l.a_port, peer: l.b_node, peerPort: l.b_port }] : l.b_node === n.name ? [{ port: l.b_port, peer: l.a_node, peerPort: l.a_port }] : [])
    .sort((a, b) => a.port.localeCompare(b.port, undefined, { numeric: true })) }));
}
function fabricText(s) {
  const lines = [`Campus fabric for Mist site ${siteName(s)}`, ...fabricFacts(s).map(([k, v]) => `${k}: ${v}`), ""];
  for (const x of fabricRows(s)) {
    lines.push([x.n.name, x.n.role, x.n.pod].filter(Boolean).join("  "));
    for (const p of x.ports) lines.push(`  ${p.port} -> ${p.peer} ${p.peerPort}`);
  }
  return lines.join("\n");
}
function joinSteps(s, sws) {
  const site = !!s.mist_site_id, all = sws.every(n => n.adopted_at), pw = rootPw[s.name];
  const li = (done, now) => `<li class="${done ? "done" : now ? "now" : "todo"}">`;
  let h = `${li(site, !site)}<h3>Mist site</h3>${site
    ? `<p class="hint">${esc(siteName(s))} <span class="mono faint">${esc(s.mist_site_id)}</span></p>`
    : `<p class="hint">Adoption joins switches to a site, so the sandbox needs its own: an empty site named ${esc(siteName(s))} in your Mist org.</p>
      <div class="actions"><button class="primary" data-act="mist" data-op="site" data-write data-mist-write data-ask="mist" data-busy-label="Creating…">Create Mist site</button></div>`}</li>`;
  h += `${li(all, site && !all)}<h3>Adopt the switches</h3>
    <p class="hint">Adopt logs in over the serial console, sets the root password and enters the site's adoption commands. The switch then calls Mist over fxp0. About a minute each.</p>
    <div class="scroll-y"><table><thead><tr><th>Switch</th><th class="state">State</th><th>Mist</th><th></th></tr></thead><tbody>${sws.map(n => `<tr>
      <td class="nm"><b>${esc(n.name)}</b> <span class="faint role">${esc(n.role)}</span></td>
      <td class="state"><span class="chip ${n.running ? "ok" : ""}">${n.running ? "running" : "stopped"}</span></td>
      <td>${n.adopted_at ? `<span class="chip mist" title="Adopted ${esc(when(n.adopted_at))}">in Mist</span>${n.mgmt_ip ? ` <span class="mono">${esc(n.mgmt_ip)}</span>` : ""}` : `<span class="faint">not yet</span>`}</td>
      <td class="right">${adoptBtn(s, n, true)}</td></tr>`).join("")}</tbody></table></div>
    <div class="pw"><span class="faint">Root password for adopted switches</span>${pw
      ? `<code>${esc(pw)}</code><button class="sm" data-act="copy-pw">Copy</button><button class="sm ghost" data-act="hide-pw">Hide</button>`
      : `<span class="dots" aria-hidden="true">••••••••</span><button class="sm" data-act="reveal">Reveal</button>`}</div></li>`;
  const built = s.fabric_built_at, ready = site && all;
  const undo = Object.keys(s.mist_snapshots || {}).filter(l => l.startsWith("before-fabric-")).pop();
  h += `${li(!!built, ready && !built)}<h3>Build the fabric in Mist</h3>
    <p class="hint">Makes the sandbox's Mist site match the cables: its networks and VRF, every switch managed by Mist, and an EVPN topology with a fabric port at both ends of each cable. SimRack saves a Mist revert point first.</p>
    <dl class="facts">${fabricFacts(s).map(([k, v]) => `<div><dt>${k}</dt><dd${/AS/.test(k) ? ` class="mono"` : ""}>${esc(v)}</dd></div>`).join("")}</dl>
    <div class="scroll-y"><table><thead><tr><th>Switch</th><th class="role">Role</th><th class="pod">PoD</th><th>Fabric ports</th></tr></thead><tbody>${fabricRows(s).map(x => `<tr>
      <td class="nm"><b>${esc(x.n.name)}</b></td><td class="role muted">${esc(x.n.role)}</td><td class="pod muted">${esc(x.n.pod || "–")}</td>
      <td>${x.ports.length ? x.ports.map(p => `<span class="pl"><span class="mono">${esc(p.port)}</span> → ${esc(p.peer)} <span class="mono faint">${esc(p.peerPort)}</span></span>`).join("")
        : `<span class="faint">none</span>`}</td></tr>`).join("")}</tbody></table></div>
    <div class="actions"><button${ready && !built ? ` class="primary"` : ""} data-act="mist" data-op="fabric" data-write data-mist-write data-ask="mist" data-busy-label="Building…"
      data-why="${site ? "" : "Create the sandbox's Mist site first"}" data-tip="Writes to the Mist site. Every switch must already be in it.">${built ? "Build again" : "Build fabric in Mist"}</button>
      <button data-act="copy-fabric">Copy as text</button></div>
    ${built ? `<p class="note">Built ${esc(when(built))}.${undo ? ` Revert point: <code>${esc(undo)}</code>` : ""}</p>` : ""}</li>`;
  const fc = s.fabric_check, healthy = !!(fc && fc.summary && fc.summary.healthy);
  h += `${li(healthy, !!built && !healthy)}<h3>Check the cabling</h3>
    <p class="hint">Checks each cable three ways: Proxmox (both ends on the cable's bridge, link up), LLDP (what each switch port hears, as Mist reports it) and the Mist topology. ${writesOn()
      ? "Ends on the wrong bridge are plugged back in and stray ports parked." : "Read-only, so it reports problems and fixes none."} It never adds NICs and never writes to Mist.</p>
    <div class="actions"><button${built && !healthy ? ` class="primary"` : ""} data-act="check" data-read data-ask-why="Where Proxmox differs from the plan, SimRack puts the cabling back the way the plan says." data-busy-label="Checking…">Check cabling</button></div>
    ${fc ? checkReport(fc) : ""}</li>`;
  return h;
}
/* each verdict as [chip class, word]; the quiet ones show no detail */
const VERDICT = {
  proxmox: { ok: ["ok", "ok"], fixed: ["accent", "fixed"], broken: ["bad", "broken"] },
  lldp: { ok: ["ok", "ok"], wrong: ["bad", "wrong"], waiting: ["warn", "waiting"] },
  mist: { linked: ["ok", "linked"], "not linked": ["bad", "not linked"], none: ["", "no topology"], skipped: ["plain", "not fabric"] },
};
function verdict(col, state, why) {
  const [cls, word] = VERDICT[col][state] || ["", state || "unknown"];
  return `<span class="lbl">${{ proxmox: "Proxmox", lldp: "LLDP", mist: "Mist" }[col]}</span><span class="chip${cls ? " " + cls : ""}">${esc(word)}</span>${
    why && cls !== "ok" ? `<span class="why">${esc(why)}</span>` : ""}`;
}
function checkReport(fc) {
  const sm = fc.summary || {}, rows = fc.cables || [];
  const mist = rows.some(r => r.lldp !== "unknown" || r.mist !== "unknown");
  const facts = [plural(rows.length, "cable")];
  if (sm.fixed) facts.push(`${sm.fixed} fixed`);
  if (sm.lldp_waiting) facts.push(`LLDP waiting on ${sm.lldp_waiting}`);
  facts.push(`checked ${when(fc.checked_at)}`);
  if (!fc.repair) facts.push("read-only");
  const more = [
    ...(fc.parked || []).map(p => p.fixed ? `Parked ${p.node} ${p.port}: it had no cable but sat on ${p.bridge}.`
      : `${p.node} ${p.port} has no cable but sits on ${p.bridge}. Check again once SimRack may change the lab to park it.`),
    ...(fc.missing || []).map(m => `${m.node} ${m.port} has no NIC in Proxmox.`),
    ...(fc.extra || []).map(x => `Mist links ${x.a_node} and ${x.b_node}, but no cable joins them. Build again to drop the link.`),
    ...(fc.notes || []),
  ];
  return `<p class="checked"><span class="chip ${sm.healthy ? "ok" : "bad"}">${sm.healthy ? "Healthy" : "Needs attention"}</span><span class="note">${esc(facts.join(" · "))}</span></p>
    ${rows.length ? `<div class="scroll-y"><table class="checks"><thead><tr><th>Cable</th><th>Proxmox</th>${mist ? "<th>LLDP</th><th>Mist</th>" : ""}</tr></thead><tbody>${rows.map(r => `<tr>
      <td class="cab"><span class="end"><b>${esc(r.a_node)}</b> <span class="mono muted">${esc(r.a_port)}</span></span> <span class="end">↔ <b>${esc(r.b_node)}</b> <span class="mono muted">${esc(r.b_port)}</span></span><span class="br">${esc(r.bridge)}</span></td>
      <td>${verdict("proxmox", r.proxmox, r.proxmox_detail)}</td>${mist ? `<td>${verdict("lldp", r.lldp, r.lldp_detail)}</td><td>${verdict("mist", r.mist, r.mist_detail)}</td>` : ""}</tr>`).join("")}</tbody></table></div>`
      : `<p class="hint">No cables to check.</p>`}
    ${more.length ? `<ul class="notes">${more.map(t => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}`;
}
async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch { note("err", "Couldn't copy to the clipboard. Select the text and copy it instead."); return; }
  const was = btn.textContent; btn.textContent = "Copied";
  setTimeout(() => { if (btn.isConnected && btn.textContent === "Copied") btn.textContent = was; }, 1500);
}
/* the password never goes through run(), so it stays out of Activity; renderMain forgets it when the view changes */
async function revealPw(s, btn) {
  btn.setAttribute("aria-busy", "true"); btn.disabled = true;
  try {
    const r = await api(`/api/sandboxes/${encodeURIComponent(s.name)}/reveal`, {});
    if ($("#main").dataset.view === "sbx:" + s.name) rootPw[s.name] = r.root_password;
  } catch (e) { note("err", "Couldn't reveal the root password: " + e.message); }
  if (btn.isConnected) { btn.removeAttribute("aria-busy"); btn.disabled = false; }
  renderMain();
  $('[data-act="copy-pw"]')?.focus({ preventScroll: true });
}

/* ---------- fabric diagram ---------- */
const TIERS = [
  { key: "upstream", label: "Upstream", roles: ["vsrx"] },
  { key: "border", label: "Border", roles: ["border"] },
  { key: "core", label: "Core", roles: ["core", "collapsed-core"] },
  { key: "dist", label: "Distribution", roles: ["distribution"] },
  { key: "access", label: "Access", roles: ["access", "esilag-access"] },
];
const LAYOUT = {
  full: { NH: 64, dot: [16, 22], nm: [28, 26], sub: [14, 47] },
  compact: { NH: 54, dot: [14, 19], nm: [25, 23], sub: [12, 41] },
  dense: { NH: 44, dot: [12, 22], nm: [22, 26] },
};
const r1 = v => Math.round(v * 10) / 10;
let measurer = null;
function textW(text, font) {
  measurer = measurer || document.createElement("canvas").getContext("2d");
  measurer.font = font;
  return measurer.measureText(text).width;
}
function fitText(text, font, max) {
  text = String(text);
  if (textW(text, font) <= max) return text;
  let lo = 0, hi = text.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (textW(text.slice(0, mid) + "…", font) <= max) lo = mid; else hi = mid - 1; }
  return text.slice(0, lo) + "…";
}
const isSel = (t, id) => !!focusItem && focusItem.t === t && focusItem.id === id;
function linkUp(s, l) {
  const a = s.nodes.find(n => n.name === l.a_node), b = s.nodes.find(n => n.name === l.b_node);
  return !!(a && b && a.running && b.running);
}
function portRanges(ports) {
  const nums = ports.map(p => Number(p.split("/").pop())).sort((a, b) => a - b), out = [];
  for (let i = 0; i < nums.length; i++) {
    let j = i; while (j + 1 < nums.length && nums[j + 1] === nums[j] + 1) j++;
    out.push(i === j ? String(nums[i]) : `${nums[i]}–${nums[j]}`); i = j;
  }
  return out.length ? "ge-0/0/" + out.join(", ") : "";
}
const motion = () => matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
function canvasWidth() {
  const c = $("#canvas"); if (!c) return 900;
  const cs = getComputedStyle(c);
  return Math.max(280, Math.floor(c.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)));
}
function watchCanvas() {
  if (canvasRO) { canvasRO.disconnect(); canvasRO = null; }
  const c = $("#canvas"); if (!c || !window.ResizeObserver) return;
  let queued = false;
  canvasRO = new ResizeObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      if (!c.isConnected || Math.abs(canvasWidth() - lastCanvasW) < 4) return;
      const sh = view.startsWith("shape:") ? shapeOf(view.slice(6)) : null, s = sandbox();
      if (sh) swap(c, shapeDiagram(sh));
      else if (s && !creating && !view) swap(c, diagram(s));
    });
  });
  canvasRO.observe(c);
}

/* A PoD is an explicit n.pod, or else distribution and access switches cabled to each other. */
function findPods(s, members) {
  const explicit = members.some(n => n.pod != null && n.pod !== "");
  const groups = new Map();
  const add = (key, n) => {
    if (!groups.has(key)) groups.set(key, { key, dist: [], acc: [] });
    groups.get(key)[n.role === "distribution" ? "dist" : "acc"].push(n);
  };
  if (explicit) members.forEach(n => add(n.pod == null ? "" : String(n.pod), n));
  else {
    const up = new Map(members.map(n => [n.name, n.name]));
    const root = x => { while (up.get(x) !== x) x = up.get(x); return x; };
    for (const l of s.links) if (up.has(l.a_node) && up.has(l.b_node)) up.set(root(l.a_node), root(l.b_node));
    members.forEach(n => add(root(n.name), n));
  }
  const list = [...groups.values()];
  if (explicit) list.sort((a, b) => (a.key === "") - (b.key === "") || a.key.localeCompare(b.key, undefined, { numeric: true }));
  else list.sort((a, b) => (b.dist.length > 0) - (a.dist.length > 0));
  let k = 0;
  for (const g of list) g.label = explicit ? (g.key === "" ? "No PoD" : /^\d+$/.test(g.key) ? "PoD " + g.key : g.key)
    : g.dist.length && g.acc.length ? "PoD " + ++k : "";
  return { list, explicit, boxed: explicit || k > 1 };
}

function diagram(s, opt) {
  const plan = !!(opt && opt.plan);
  if (!s.nodes.length) return `<div class="empty"><div><p style="margin:0 0 .25rem;color:var(--ink);font-weight:600">Empty fabric</p><p style="margin:0">${plan ? "This topology has no switches." : "Add a guest to start drawing."}</p></div></div>`;
  const cw = canvasWidth(); lastCanvasW = cw;
  const root = getComputedStyle(document.documentElement), SANS = root.getPropertyValue("--sans").trim(), MONO = root.getPropertyValue("--mono").trim();
  const known = new Set(TIERS.flatMap(t => t.roles));
  const rows = [...TIERS.map(t => ({ ...t, nodes: s.nodes.filter(n => t.roles.includes(n.role)) })),
    { key: "hosts", label: "Hosts", nodes: s.nodes.filter(n => !known.has(n.role)) }].filter(t => t.nodes.length);
  const podRow = t => t.key === "dist" || t.key === "access";
  const pods = findPods(s, rows.filter(podRow).flatMap(t => t.nodes));
  const inPods = t => pods.boxed && podRow(t);
  const cols = g => Math.max(g.dist.length, g.acc.length);
  const U = pods.boxed ? pods.list.reduce((a, g) => a + cols(g), 0) : 0;
  const most = Math.max(U, ...rows.filter(t => !inPods(t)).map(t => t.nodes.length));

  // Width first: every column gets at least MINSTEP, then boxes shrink and drop detail as the fabric grows.
  // The smallest box still fits the longest name, so names never get cut.
  const nameNW = Math.ceil(Math.max(0, ...s.nodes.map(n => textW(n.name, `600 12px ${SANS}`))) + LAYOUT.dense.nm[0] + 10);
  const MINNW = Math.min(168, Math.max(100, nameNW));
  const narrow = cw < 600, LEFT = narrow ? 8 : 100, RIGHT = 8, MINSTEP = MINNW + 28, MAXSTEP = 400;
  const W = Math.max(narrow ? 300 : 600, cw, LEFT + RIGHT + most * MINSTEP), span = W - LEFT - RIGHT;
  const stepFor = m => Math.min(MAXSTEP, span / m);
  const stepP = U ? stepFor(U) : Infinity;
  const minStep = Math.min(stepP, ...rows.filter(t => !inPods(t)).map(t => stepFor(t.nodes.length)));
  const NW = Math.round(Math.max(MINNW, Math.min(208, minStep - 28)));
  const mode = NW >= 150 ? "full" : NW >= 118 ? "compact" : "dense", L = LAYOUT[mode], NH = L.NH;
  const GAP = mode === "full" ? (rows.length > 3 ? 164 : 180) : mode === "compact" ? 152 : 136;
  const TOP = pods.boxed && podRow(rows[0]) ? 44 : 28;
  const rowY = r => TOP + r * GAP;

  const pos = Object.create(null), order = new Map(s.nodes.map((n, i) => [n.name, i])), nbr = new Map(s.nodes.map(n => [n.name, []]));
  for (const l of s.links) if (nbr.has(l.a_node) && nbr.has(l.b_node)) { nbr.get(l.a_node).push(l.b_node); nbr.get(l.b_node).push(l.a_node); }
  const bary = n => { const xs = nbr.get(n.name).filter(m => pos[m]).map(m => pos[m].cx); return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : Infinity; };
  const sorted = list => list.map(n => [n, bary(n), order.get(n.name)]).sort((p, q) => (p[1] - q[1]) || (p[2] - q[2])).map(p => p[0]);
  const place = (list, r, x0, width) => {
    const step = width / list.length;
    list.forEach((n, i) => { const cx = r1(x0 + step * (i + .5)); pos[n.name] = { x: r1(cx - NW / 2), cx, y: rowY(r), row: r, n }; });
  };
  const podX0 = LEFT + (span - U * stepP) / 2;
  rows.forEach((t, r) => {
    if (!inPods(t)) { const tw = stepFor(t.nodes.length) * t.nodes.length; place(sorted(t.nodes), r, LEFT + (span - tw) / 2, tw); return; }
    let x = podX0;
    for (const g of pods.list) {
      const members = t.key === "dist" ? g.dist : g.acc, w = cols(g) * stepP;
      if (members.length) place(sorted(members), r, x, w);
      x += w;
    }
  });

  const ends = Object.create(null);
  const links = s.links.filter(l => pos[l.a_node] && pos[l.b_node]).map(l => {
    let A = pos[l.a_node], B = pos[l.b_node], ap = l.a_port, bp = l.b_port;
    if (A.row > B.row) [A, B, ap, bp] = [B, A, bp, ap];
    const k = { l, A, B, ap, bp, same: A.row === B.row };
    const ka = A.n.name + ":b", kb = B.n.name + (k.same ? ":b" : ":t");
    (ends[ka] = ends[ka] || []).push({ k, side: "a", other: B.cx });
    (ends[kb] = ends[kb] || []).push({ k, side: "b", other: A.cx });
    return k;
  });
  const chipW = t => t.length * 6.4 + 10;
  const busiest = Math.max(0, ...Object.values(ends).map(e => e.length));
  const crowded = mode !== "full" || busiest > 4;
  for (const key in ends) {
    const list = ends[key].sort((p, q) => p.other - q.other), m = list.length;
    const lanes = m < 2 || .72 * NW / (m - 1) >= chipW("ge-0/0/0") + 6 ? 1 : crowded ? 3 : 2;
    list.forEach((e, i) => { e.k[e.side + "x"] = r1((m === 1 ? .5 : .14 + .72 * i / (m - 1)) * NW); e.k[e.side + "lane"] = i % lanes; });
  }
  const boxes = pods.boxed ? pods.list.filter(g => g.label).map(g => {
    const ps = [...g.dist, ...g.acc].map(n => pos[n.name]), lastRow = Math.max(...ps.map(p => p.row));
    return { label: g.label, lastRow, x1: r1(Math.min(...ps.map(p => p.x)) - 10), x2: r1(Math.max(...ps.map(p => p.x)) + NW + 10),
             y1: rowY(Math.min(...ps.map(p => p.row))) - 30, y2: rowY(lastRow) + NH + 30 };
  }) : [];
  const last = rows.length - 1;
  const H = rowY(last) + NH + Math.max(links.some(k => k.same && k.A.row === last) ? 84 : 24, boxes.some(b => b.lastRow === last) ? 40 : 0);

  const selNode = !plan && focusItem && focusItem.t === "node" ? focusItem.id : null;
  const cls = [mode, crowded ? "crowded" : "", !plan && focusItem ? "focusing" : "", plan ? "plan" : "", plan && opt.intro ? "intro" : ""].filter(Boolean).join(" ");
  let svg = `<svg class="${cls}" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="group" aria-label="${plan ? "Planned fabric" : "Fabric diagram"} for ${esc(s.name)}">`;
  for (const b of boxes) svg += `<g class="pod"><rect x="${b.x1}" y="${b.y1}" width="${r1(b.x2 - b.x1)}" height="${b.y2 - b.y1}" rx="12"/><text x="${b.x1 + 12}" y="${b.y2 + 4}">${esc(b.label)}</text></g>`;
  // Narrow canvases put the tier name on the divider, clear of the port chips at both ends of a cable.
  rows.forEach((t, r) => {
    const y = rowY(r), lineY = y - (GAP - NH) / 2, line = r && !(inPods(t) && inPods(rows[r - 1]));
    if (!narrow) {
      svg += `<text class="tier" x="20" y="${y + NH / 2 + 4}">${esc(t.label)}</text>`;
      if (line) svg += `<line class="tierline" x1="16" x2="${W - 16}" y1="${lineY}" y2="${lineY}"/>`;
      return;
    }
    svg += `<text class="tier above" x="${LEFT}" y="${r ? lineY + 4 : y - 9}">${esc(t.label)}</text>`;
    if (line) svg += `<line class="tierline" x1="${r1(LEFT + textW(t.label, `600 11px ${SANS}`) + 8)}" x2="${W - 4}" y1="${lineY}" y2="${lineY}"/>`;
  });
  const chip = (x, y, text) => { const w = chipW(text); return `<g class="port"><rect x="${r1(x - w / 2)}" y="${y - 8}" width="${r1(w)}" height="16" rx="4"/><text x="${x}" y="${y + 3.5}" text-anchor="middle">${esc(text)}</text></g>`; };
  for (const k of links) {
    const x1 = r1(k.A.x + k.ax), y1 = k.A.y + NH, x2 = r1(k.B.x + k.bx), y2 = k.same ? k.B.y + NH : k.B.y, mid = (y1 + y2) / 2;
    const d = k.same ? `M${x1} ${y1} C${x1} ${y1 + 56},${x2} ${y2 + 56},${x2} ${y2}` : `M${x1} ${y1} C${x1} ${mid},${x2} ${mid},${x2} ${y2}`;
    const lyA = y1 + 15 + k.alane * 17, lyB = k.same ? y2 + 15 + k.blane * 17 : y2 - 15 - k.blane * 17;
    if (plan) {
      // pathLength lets the intro draw a seen cable from its upper end; a guessed one keeps real-length dashes
      const seen = k.l.via === "lldp", out = !!opt.kept && !opt.kept.has(k.l);
      svg += `<g class="cable plan${seen ? "" : " guess"}${out ? " out" : ""}"><title>${esc(k.l.a_node)} ${esc(k.l.a_port)} ↔ ${esc(k.l.b_node)} ${esc(k.l.b_port)} · ${seen ? "seen by LLDP" : "ports guessed"}${
        out ? " · left out: " + esc(opt.why.get(k.l) || "neither switch is built") : ""}</title>
      <path class="wire" d="${d}"${seen ? ` pathLength="1"` : ""}/>${chip(x1, lyA, k.ap)}${chip(x2, lyB, k.bp)}</g>`;
      continue;
    }
    const up = k.A.n.running && k.B.n.running, on = isSel("cable", k.l.bridge);
    const lit = !!selNode && (k.l.a_node === selNode || k.l.b_node === selNode);
    svg += `<g class="cable${up ? " up" : ""}${on ? " sel" : ""}${lit ? " lit" : ""}" data-act="focus-cable" data-id="${esc(k.l.bridge)}" tabindex="-1">
      <title>${esc(k.l.a_node)} ${esc(k.l.a_port)} ↔ ${esc(k.l.b_node)} ${esc(k.l.b_port)} · ${esc(k.l.bridge)} · mtu ${k.l.mtu}</title>
      <path class="hit" d="${d}"/><path class="wire" d="${d}"/>${chip(x1, lyA, k.ap)}${chip(x2, lyB, k.bp)}</g>`;
  }
  const nmFont = `600 ${mode === "dense" ? 12 : 13}px ${SANS}`, subFont = `11px ${MONO}`;
  for (const name in pos) {
    const p = pos[name], n = p.n;
    if (plan) {
      const sub = !n.from ? "" : mode === "full" ? "copies " + n.from : mode === "compact" ? n.from : "";
      const out = !!opt.skip && opt.skip.has(name);
      svg += `<g class="node${out ? " out" : ""}" transform="translate(${p.x},${p.y})">
      <title>${esc(name)} · ${esc(n.role)}${n.pod ? " · " + esc(n.pod) : ""}${n.from ? " · copies " + esc(n.from) : ""}${out ? " · not in this build" : ""}</title>
      <rect class="box" width="${NW}" height="${NH}" rx="8"/><circle class="dot ring" cx="${L.dot[0]}" cy="${L.dot[1]}" r="4.5"/>
      <text class="nm" x="${L.nm[0]}" y="${L.nm[1]}">${esc(fitText(name, nmFont, NW - L.nm[0] - 10))}</text>${
      sub ? `<text class="sub" x="${L.sub[0]}" y="${L.sub[1]}">${esc(fitText(sub, subFont, NW - L.sub[0] - 10))}</text>` : ""}</g>`;
      continue;
    }
    const on = isSel("node", name);
    const sub = mode === "full" ? `${n.vmid}${n.mgmt_ip ? " · " + n.mgmt_ip : ""}` : mode === "compact" ? (n.mgmt_ip || String(n.vmid)) : "";
    svg += `<g class="node${on ? " sel" : ""}" data-act="focus-node" data-id="${esc(name)}" tabindex="0" role="button" aria-expanded="${on}" aria-controls="inspector" aria-label="${esc(name)}, ${esc(n.role)}, ${n.running ? "running" : "stopped"}" transform="translate(${p.x},${p.y})">
      <title>${esc(name)} · ${esc(n.role)} · VMID ${n.vmid}${n.mgmt_ip ? " · " + esc(n.mgmt_ip) : ""}</title>
      <rect class="box" width="${NW}" height="${NH}" rx="8"/><circle class="dot${n.running ? " on" : ""}" cx="${L.dot[0]}" cy="${L.dot[1]}" r="4.5"/>
      <text class="nm" x="${L.nm[0]}" y="${L.nm[1]}">${esc(fitText(name, nmFont, NW - L.nm[0] - 10))}</text>${
      sub ? `<text class="sub" x="${L.sub[0]}" y="${L.sub[1]}">${esc(fitText(sub, subFont, NW - L.sub[0] - 10))}</text>` : ""}</g>`;
  }
  return svg + "</svg>";
}

/* ---------- selection strip ---------- */
const CLOSE = `<button class="ghost icon close" data-act="unfocus" aria-label="Close" title="Close (Esc)"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></button>`;
function inspector(s) {
  if (!focusItem) return "";
  if (focusItem.t === "node") {
    const n = s.nodes.find(x => x.name === focusItem.id);
    const cables = s.links.filter(l => l.a_node === n.name || l.b_node === n.name);
    const used = usedPorts(s, n.name), free = PORTS.filter(p => !used.has(p));
    const boot = n.image ? n.image.split("/").pop() : n.template_vmid ? "template " + n.template_vmid : "–";
    return `<div class="strip-head">
      <h2 tabindex="-1">${esc(n.name)} <span class="chip ${n.running ? "ok" : ""}">${n.running ? "running" : "stopped"}</span></h2>
      <div class="actions">
        ${n.running ? `<button data-act="power" data-node="${esc(n.name)}" data-action="shutdown" data-write data-ask="power" data-busy-label="Shutting down…">Shut down</button>
          <button data-act="power" data-node="${esc(n.name)}" data-action="stop" data-write data-ask="power" data-ask-always data-ask-why="Like pulling the plug: the guest gets no chance to shut down." data-busy-label="Powering off…">Power off</button>`
          : `<button class="primary" data-act="power" data-node="${esc(n.name)}" data-action="start" data-write data-ask="power" data-busy-label="Starting…">Start</button>`}
        ${n.kind === "switch" ? adoptBtn(s, n) : ""}
        <button data-act="cable-from" data-node="${esc(n.name)}" data-write>Cable from here</button>
        <button class="danger" data-act="del-node" data-node="${esc(n.name)}" data-write data-ask="delete" data-ask-always data-ask-why="The guest and its disk are destroyed, and its cables come out." data-busy-label="Deleting…">Delete</button>
      </div>${CLOSE}
    </div>
    <div class="strip-body">
      <dl class="facts">
        <div><dt>Role</dt><dd>${esc(n.role)} · ${esc(n.kind)}</dd></div>
        <div><dt>VMID</dt><dd class="mono">${n.vmid}</dd></div>
        <div><dt>Mgmt IP</dt><dd class="mono">${esc(n.mgmt_ip || "–")}${planned(n)}</dd></div>
        ${n.kind === "switch" ? `<div><dt>Mist</dt><dd>${n.adopted_at ? "adopted " + esc(when(n.adopted_at)) : s.mist_site_id ? "not adopted yet" : "no site yet"}</dd></div>` : ""}
        <div><dt>Boot</dt><dd class="mono">${esc(boot)}</dd></div>
        <div class="wide"><dt>Cables</dt><dd>${cables.length ? cables.map(l => { const mine = l.a_node === n.name;
          return `<span class="pl"><span class="mono">${esc(mine ? l.a_port : l.b_port)}</span> → ${esc(mine ? l.b_node : l.a_node)} <span class="mono faint">${esc(mine ? l.b_port : l.a_port)}</span></span>`; }).join("") : "none"}</dd></div>
        <div class="wide"><dt>Free ports</dt><dd class="mono">${free.length ? esc(portRanges(free)) : "none"}</dd></div>
      </dl>
      ${n.kind === "switch" ? `<div class="console"><form class="fields inline" data-form="console" data-node="${esc(n.name)}" autocomplete="off">
          <label class="f">Serial console<input name="command" class="mono" placeholder="show interfaces terse" maxlength="2000"></label>
          <button type="submit" data-write data-ask="console" data-busy-label="Sending…">Send</button></form>
        ${consoleOut[n.name] ? `<pre>${esc(consoleOut[n.name])}</pre>` : `<p class="note" style="margin-top:.5rem">Sends one command to the serial console and shows the reply here.</p>`}</div>` : ""}
    </div>`;
  }
  const l = s.links.find(x => x.bridge === focusItem.id), ok = linkUp(s, l);
  const from = moveFrom.bridge === l.bridge && (moveFrom.node === l.a_node || moveFrom.node === l.b_node) ? moveFrom.node : l.a_node;
  return `<div class="strip-head">
      <h2 tabindex="-1"><span><span class="end">${esc(l.a_node)} <span class="mono muted">${esc(l.a_port)}</span></span> <span class="end">↔ ${esc(l.b_node)} <span class="mono muted">${esc(l.b_port)}</span></span></span>
        <span class="chip ${ok ? "ok" : ""}">${ok ? "both ends up" : "an end is down"}</span></h2>
      <div class="actions"><button class="danger" data-act="unplug" data-bridge="${esc(l.bridge)}" data-write data-ask="cable">Unplug</button></div>${CLOSE}
    </div>
    <div class="strip-body">
      <dl class="facts">
        <div><dt>Bridge</dt><dd class="mono">${esc(l.bridge)}</dd></div>
        <div><dt>MTU</dt><dd class="mono">${l.mtu}</dd></div>
        <div class="wide"><dt>Link-local traffic</dt><dd>LLDP and LACP pass through</dd></div>
      </dl>
      <form class="fields line move" data-form="move" data-bridge="${esc(l.bridge)}" autocomplete="off">
        <label class="f">Move this end<select name="from_node" data-move-from><option value="${esc(l.a_node)}"${from === l.a_node ? " selected" : ""}>${esc(l.a_node)} ${esc(l.a_port)}</option><option value="${esc(l.b_node)}"${from === l.b_node ? " selected" : ""}>${esc(l.b_node)} ${esc(l.b_port)}</option></select></label>
        <label class="f">To guest<select name="to_node" data-port-target="to_port">${moveTargets(s, l, from)}</select></label>
        <label class="f">Port<select name="to_port" class="mono">${portOptions(s, "", null)}</select></label>
        <button type="submit" data-write data-ask="cable" data-busy-label="Moving…">Move cable</button>
      </form>
    </div>`;
}

/* ---------- selection, focus and the rail ---------- */
function reselect(n) {
  if (!n || !n.tagName) return null;
  const tag = n.tagName.toLowerCase();
  if (n.name && /^(input|select|textarea)$/.test(tag)) return `${tag}[name="${CSS.escape(n.name)}"]`;
  if (tag === "h2") return "h2";
  let q = tag;
  for (const k of ["act", "id", "node", "action", "bridge", "op", "tick"]) if (n.dataset && n.dataset[k] != null) q += `[data-${k}="${CSS.escape(n.dataset[k])}"]`;
  return q === tag ? null : q;
}
function reveal(fromStage) {
  const strip = $("#inspector"); if (!strip || strip.hidden) return;
  if (fromStage) { if (strip.getBoundingClientRect().top > innerHeight - 120) strip.scrollIntoView({ behavior: motion(), block: "nearest" }); return; }
  strip.scrollIntoView({ behavior: motion(), block: "start" });
  const h = $("h2", strip); if (h) h.focus({ preventScroll: true });
}
function closeStrip() {
  const back = returnTo; returnTo = null;
  if (!focusItem) return;
  focusItem = null; renderAll();
  const el = back && document.querySelector(back);
  if (el) el.focus();
}
function applyRail() {
  const on = localStorage.getItem("lf_rail") !== "off";
  $(".app").classList.toggle("rail-off", !on);
  $("#railbtn").setAttribute("aria-expanded", String(on));
}

/* ---------- events ---------- */
document.addEventListener("click", e => {
  const el = e.target.closest("[data-act]"); if (!el) return;
  const act = el.dataset.act, s = sandbox();
  if (el.tagName === "BUTTON" && el.disabled) return;
  switch (act) {
    case "refresh": refresh(true); break;
    case "new": creating = true; setView(""); renderAll(); break;
    case "cancel-new": creating = false; renderAll(); break;
    case "pick": creating = false; setView(""); sel = el.dataset.name; localStorage.setItem("lf_sel", sel); renderAll(); break;
    case "import":
      if (view !== "import") prevView = view;
      creating = false; setView("import"); renderAll();
      if (!el.isConnected) $(".intake h1")?.focus({ preventScroll: true });
      break;
    case "setup":
      if (view === "setup") { loadSetup(); break; }
      prevView = view; creating = false; setupPage = null; setupErr = ""; setView("setup");
      if (S) renderAll();
      if (!el.isConnected) $(".intake h1")?.focus({ preventScroll: true });
      break;
    case "setup-retry": loadSetup(); renderMain(); break;
    case "setup-close":
      setView(prevView === "import" || (prevView.startsWith("shape:") && shapeOf(prevView.slice(6))) ? prevView : "");
      renderAll(); $("#setupbtn").focus({ preventScroll: true });
      break;
    case "copy-cmds": copyText(((setupPage && setupPage.proxmox_token_commands) || []).join("\n"), el); break;
    case "setup-export": exportProfile(el); break;
    case "setup-import": $("[data-setup-file]")?.click(); break;
    case "cancel-import": setView(prevView.startsWith("shape:") && shapeOf(prevView.slice(6)) ? prevView : ""); renderAll(); break;
    case "pick-shape": creating = false; setView("shape:" + el.dataset.name); renderAll(); break;
    case "del-shape": {
      const name = el.dataset.name, box = $("#shapemsg");
      showErr(box, "");
      run(el, `Delete shape ${name}`, async () => {
        try { return await api(`/api/shapes/${encodeURIComponent(name)}/delete`, {}); } catch (err) { showErr(box, err.message, err.detail); throw err; }
      }, () => { setView(""); });
      break; }
    case "focus-node": case "focus-cable": {
      if (e.target.closest("button") && e.target.closest("button") !== el) return;
      const t = act === "focus-node" ? "node" : "cable", target = el.matches("tr, li") ? $("button.link", el) || el : el;
      returnTo = reselect(target);
      if (isSel(t, el.dataset.id)) { closeStrip(); break; }
      const fromStage = !!el.closest("#canvas");
      focusItem = { t, id: el.dataset.id }; renderAll(); reveal(fromStage); break; }
    case "unfocus": closeStrip(); break;
    case "rail": localStorage.setItem("lf_rail", localStorage.getItem("lf_rail") === "off" ? "on" : "off"); applyRail(); break;
    case "power":
      run(el, `${el.dataset.action === "start" ? "Start" : el.dataset.action === "stop" ? "Power off" : "Shut down"} ${el.dataset.node}`,
        () => api(`/api/sandboxes/${encodeURIComponent(s.name)}/nodes/${encodeURIComponent(el.dataset.node)}/power`, { action: el.dataset.action })); break;
    case "del-node":
      run(el, `Delete ${el.dataset.node}`, () => api(`/api/sandboxes/${encodeURIComponent(s.name)}/nodes/${encodeURIComponent(el.dataset.node)}/delete`, {}), () => { focusItem = null; }); break;
    case "unplug":
      run(el, `Unplug ${el.dataset.bridge}`, () => api(`/api/sandboxes/${encodeURIComponent(s.name)}/cables/${encodeURIComponent(el.dataset.bridge)}/remove`, {}), () => { focusItem = null; }); break;
    case "cable-from": {
      const f = $('[data-form="cable"]'); f.a_node.value = el.dataset.node;
      fillSelect(f.a_port, portOptions(s, el.dataset.node, null));
      const firstFree = $$("option", f.a_port).find(o => !o.disabled); if (firstFree) f.a_port.value = firstFree.value;
      f.scrollIntoView({ behavior: motion(), block: "center" }); f.b_node.focus({ preventScroll: true }); break; }
    case "adopt":
      run(el, `Adopt ${el.dataset.node} into Mist`, () => api(`/api/sandboxes/${encodeURIComponent(s.name)}/nodes/${encodeURIComponent(el.dataset.node)}/adopt`, {})); break;
    case "reveal": revealPw(s, el); break;
    case "hide-pw": delete rootPw[s.name]; renderMain(); $('[data-act="reveal"]')?.focus({ preventScroll: true }); break;
    case "copy-pw": copyText(rootPw[s.name] || "", el); break;
    case "copy-fabric": copyText(fabricText(s), el); break;
    case "mist":
      run(el, el.dataset.op === "site" ? "Create Mist site" : "Build fabric in Mist",
        () => api(`/api/sandboxes/${encodeURIComponent(s.name)}/mist/${el.dataset.op}`, {})); break;
    case "pause": togglePause(el); break;
    case "check":
      el.dataset.ask = S.writes_enabled ? "repair" : "";
      run(el, "Check cabling", () => api(`/api/sandboxes/${encodeURIComponent(s.name)}/fabric/check`, {})); break;
    case "mist-get":
      run(el, el.dataset.op === "health" ? "Mist health" : "Adoption command",
        () => api(`/api/sandboxes/${encodeURIComponent(s.name)}/mist/${el.dataset.op}`)); break;
  }
});
document.addEventListener("keydown", e => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches("g[data-act]")) { e.preventDefault(); e.target.dispatchEvent(new MouseEvent("click", { bubbles: true })); }
  if (e.key === "Escape" && focusItem) {
    const t = e.target;
    if (t.matches("select") || (t.matches("input, textarea") && t.value)) return;
    e.preventDefault(); closeStrip();
  }
});
document.addEventListener("change", e => {
  const t = e.target;
  if (t.matches("[data-recipes]")) updateRecipeNote(t.form);
  if (t.matches("[data-setup-file]") && t.files.length) importProfile(t);
  if (t.matches('[data-form="create"] [data-boot], [data-form="build"] [data-boot]')) updateBootNote(t.form);
  if (t.matches("[data-tick]") && view.startsWith("shape:") && shapeOf(view.slice(6))) {
    const sh = shapeOf(view.slice(6)), set = new Set(picksFor(sh));
    if (t.dataset.tick === "*") { set.clear(); if (t.checked) for (const n of sh.nodes) set.add(n.name); }
    else if (t.checked) set.add(t.dataset.tick); else set.delete(t.dataset.tick);
    picks[sh.name] = { set, touched: true };
    showErr($("[data-build-msg]"), "");
    updateShape(sh); setWriteDisabled();
  }
  if (t.matches('[data-form="add-node"] [name="type"]')) {
    const kind = t.value.split(":")[0], want = kind === "vsrx" ? /vsrx/i : kind === "switch" ? SWITCH_IMAGE : null;
    const o = want && $$("option", t.form.boot).find(o => !o.disabled && want.test(o.textContent));
    if (o) t.form.boot.value = o.value;
  }
  if (t.matches("[data-move-from]") && sandbox()) {
    const s = sandbox(), f = t.form, l = s.links.find(x => x.bridge === f.dataset.bridge);
    if (l) {
      moveFrom = { bridge: l.bridge, node: t.value };
      const to = f.elements.to_node, was = to.value;
      to._html = null; fillSelect(to, moveTargets(s, l, t.value));
      if (to.value !== was) { const port = f.elements.to_port; port._html = null; fillSelect(port, portOptions(s, to.value, null)); }
    }
  }
  if (t.dataset.portTarget && sandbox()) {
    const port = t.form.elements[t.dataset.portTarget];
    port._html = null; fillSelect(port, portOptions(sandbox(), t.value, null));
    const firstFree = $$("option", port).find(o => !o.disabled); if (firstFree && port.selectedOptions[0]?.disabled) port.value = firstFree.value;
  }
});
/* setup: a lab field the user changed keeps its value when the page is read again, and any change clears "Saved" */
const setupEdit = e => {
  const t = e.target, region = t.closest && t.closest("#setup-lab, #setup-connect");
  if (!region || !t.name || t.type === "file") return;
  if (region.id === "setup-lab") { t.setAttribute("data-touched", ""); t.removeAttribute("aria-invalid"); }
  const ok = $("[data-setup-ok], [data-setup-tokens-ok]", region); if (ok) ok.textContent = "";
};
document.addEventListener("input", setupEdit);
document.addEventListener("change", setupEdit);
/* saved Mist JSON can be dropped on the import panel; elsewhere on that page a drop must not open the file */
const fileDrag = e => view === "import" && !!e.dataTransfer && [...e.dataTransfer.types].includes("Files");
const dropOver = z => $$("[data-drop]").forEach(d => d.classList.toggle("over", d === z));
document.addEventListener("dragover", e => {
  if (!fileDrag(e)) return;
  e.preventDefault();
  const z = e.target.closest ? e.target.closest("[data-drop]") : null;
  e.dataTransfer.dropEffect = z ? "copy" : "none";
  dropOver(z);
});
document.addEventListener("dragleave", e => { if (!e.relatedTarget) dropOver(null); });
document.addEventListener("drop", e => {
  if (!fileDrag(e)) return;
  e.preventDefault(); dropOver(null);
  const z = e.target.closest ? e.target.closest("[data-drop]") : null, input = z && $('input[type="file"]', z);
  if (input && e.dataTransfer.files.length) { input.files = e.dataTransfer.files; showErr($("[data-import-msg]", z), ""); }
});
document.addEventListener("submit", e => {
  const f = e.target, kind = f.dataset.form; if (!kind) return;
  e.preventDefault();
  const btn = $('button[type="submit"]', f), s = sandbox(), base = s ? `/api/sandboxes/${encodeURIComponent(s.name)}` : "";
  if (btn && btn.disabled) return;
  const v = name => (f.elements[name] ? f.elements[name].value.trim() : "");
  switch (kind) {
    case "token": token = v("token"); localStorage.setItem("simrack_token", token); authNeeded = false; refresh(true); break;
    case "import": importShape(f, btn); break;
    case "setup-tokens": saveTokens(f, btn); break;
    case "setup": saveProfile(f, btn); break;
    case "create": {
      const body = { name: v("name"), recipe: v("recipe"), start: f.start.checked, with_mist_site: f.mist.checked, ...bootBody(v("boot")) };
      run(btn, `Build ${body.name}`, () => api("/api/sandboxes", body), r => { creating = false; sel = r.name; localStorage.setItem("lf_sel", sel); f.reset(); });
      break; }
    case "build": {
      const sh = shapeOf(f.dataset.shape), box = $("[data-build-msg]", f); if (!sh) return;
      const chosen = picksFor(sh);
      const body = { name: v("name"), switches: sh.nodes.map(n => n.name).filter(x => chosen.has(x)), start: f.start.checked,
        with_mist_site: f.mist.checked && !f.mist.disabled, ...bootBody(v("boot")) };
      showErr(box, "");
      run(btn, `Build ${body.name} from ${sh.name}`, async () => {
        try { return await api(`/api/shapes/${encodeURIComponent(sh.name)}/build`, body); } catch (err) { showErr(box, err.message, err.detail); throw err; }
      }, r => { delete picks[sh.name]; setView(""); creating = false; sel = r.sandbox.name; localStorage.setItem("lf_sel", sel); });
      break; }
    case "add-node": {
      const [kindv, role] = v("type").split(":");
      run(btn, `Add ${v("node")}`, () => api(`${base}/nodes`, { node: v("node"), role, kind: kindv, ...bootBody(v("boot")) }), () => { f.node.value = ""; });
      break; }
    case "cable":
      if (!v("a_node") || !v("b_node")) { note("err", "Pick both ends of the cable."); return; }
      run(btn, `Cable ${v("a_node")} ${v("a_port")} ↔ ${v("b_node")} ${v("b_port")}`,
        () => api(`${base}/cables`, { a_node: v("a_node"), a_port: v("a_port"), b_node: v("b_node"), b_port: v("b_port") }), () => { f.reset(); });
      break;
    case "move":
      if (!v("to_node")) { note("err", "Pick where the cable goes."); return; }
      run(btn, `Move ${f.dataset.bridge}`, () => api(`${base}/cables/${encodeURIComponent(f.dataset.bridge)}/move`, { from_node: v("from_node"), to_node: v("to_node"), to_port: v("to_port") }),
        r => { if (r && r.link) focusItem = { t: "cable", id: r.link.bridge }; });
      break;
    case "console": {
      const node = f.dataset.node;
      if (!v("command")) return;
      run(btn, `Console ${node}: ${v("command")}`, () => api(`${base}/nodes/${encodeURIComponent(node)}/console`, { command: v("command") }),
        r => { consoleOut[node] = (r && r.output) || "(no output)"; f.command.value = ""; });
      break; }
    case "pve-snap": run(btn, `Save Proxmox point ${v("label")}`, () => api(`${base}/snapshot`, { label: v("label") }), () => { f.label.value = ""; }); break;
    case "pve-revert":
      if (!v("label")) { note("err", "No Proxmox revert point saved yet."); return; }
      run(btn, `Revert guests to ${v("label")}`, () => api(`${base}/revert`, { label: v("label") })); break;
    case "mist-snap":
      if (!s.mist_site_id) { note("err", "This sandbox has no Mist site."); return; }
      run(btn, `Save Mist point ${v("label")}`, () => api(`${base}/mist/snapshot`, { label: v("label") }), () => { f.label.value = ""; }); break;
    case "mist-revert":
      if (!v("label")) { note("err", "No Mist revert point saved yet."); return; }
      run(btn, `Revert Mist to ${v("label")}`, () => api(`${base}/mist/revert`, { label: v("label") })); break;
    case "teardown": {
      const name = s.name, keep = f.keep_mist.checked, plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
      btn.dataset.askWhy = `${plural(s.nodes.length, "guest")} and ${plural(s.links.length, "cable")} are destroyed`
        + (s.mist_site_id ? keep ? "; the Mist site stays." : ", and so is its Mist site." : ".") + " This can't be undone.";
      run(btn, `Tear down ${name}`, () => api(`${base}/teardown`, { keep_mist: keep }), r => {
        if (r && r.complete === false) note("err", `${name} was only partly removed. It is still listed; fix the cause and tear down again.`, r);
        else { sel = ""; localStorage.removeItem("lf_sel"); }
      });
      break; }
  }
});

/* ---------- refresh ---------- */
function renderAll() { renderTop(); renderBanner(); renderRail(); renderMain(); }
async function refresh(force) {
  if (!force && (busy || document.hidden || $("#ask").open)) return;
  try { S = await api("/api/state"); authNeeded = false; }
  catch (e) { $("#conn").className = "conn off"; if (!authNeeded) renderBanner(e.message); return; }
  if (sel && !S.sandboxes.some(s => s.name === sel)) { sel = ""; localStorage.removeItem("lf_sel"); }
  if (!sel && S.sandboxes.length && !creating) sel = S.sandboxes[0].name;
  if (view.startsWith("shape:") && !shapeOf(view.slice(6))) setView("");
  if (S.profile && !S.profile.loaded && !autoSetup) {
    autoSetup = true;
    if (view !== "setup") { prevView = view; creating = false; setView("setup"); }
  }
  renderAll();
}
applyRail();
/* the rail pins below the top bar and any banner, so keep their height in CSS */
const chromeRO = new ResizeObserver(() => {
  const th = $(".top").getBoundingClientRect().height, b = $("#banner"), bh = b.hidden ? 0 : b.getBoundingClientRect().height;
  document.documentElement.style.setProperty("--top-h", th + "px");
  document.documentElement.style.setProperty("--chrome-h", th + bh + "px");
});
chromeRO.observe($(".top")); chromeRO.observe($("#banner"));
refresh(true);
setInterval(refresh, 10000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
