"use strict";

const $ = (id) => document.getElementById(id);
const el = {
  path: $("path"), count: $("count"), badges: $("badges"), filter: $("filter"),
  list: $("list"), status: $("status"), fname: $("fname"), fdims: $("fdims"),
  fage: $("fage"), fzoom: $("fzoom"), fmode: $("fmode"), stage: $("stage"),
  img: $("img"), empty: $("empty"), help: $("help"),
};

const S = {
  dir: null, recursive: false, sort: "time", filter: "",
  listing: null, pinned: [], source: null,
  all: [], items: [], sel: 0, nodes: [],
  seq: -1, sig: null, reqId: 0, offline: false,
  cur: null, nat: { w: 0, h: 0 },
  v: { s: 1, tx: 0, ty: 0, mode: "contain", fit: true },
};

// ---------------------------------------------------------------- small helpers

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function fmtAge(m) {
  if (!m) return "";
  const d = Date.now() / 1000 - m;
  if (d < 60) return Math.max(0, Math.round(d)) + "s";
  if (d < 3600) return Math.round(d / 60) + "m";
  if (d < 86400) return Math.round(d / 3600) + "h";
  if (d < 86400 * 14) return Math.round(d / 86400) + "d";
  return new Date(m * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function fmtWhen(m) {
  return m ? new Date(m * 1000).toLocaleString() : "";
}

function imgUrl(it) {
  const v = it.m ? "&v=" + Math.round(it.m * 1000) + "-" + (it.s || 0) : "";
  return "/img?p=" + encodeURIComponent(it.path) + v;
}

async function api(path, params) {
  const u = new URL(path, location.origin);
  for (const [k, val] of Object.entries(params || {})) {
    if (val !== null && val !== undefined) u.searchParams.set(k, val);
  }
  const r = await fetch(u, { headers: { Accept: "application/json" } });
  if (!r.ok && r.status !== 404) throw new Error("http " + r.status);
  return r.json();
}

function setOffline(off) {
  if (S.offline === off) return;
  S.offline = off;
  el.status.classList.toggle("bad", off);
  updateFoot();
}

function flash(msg) {
  el.status.textContent = msg;
  clearTimeout(flash.t);
  flash.t = setTimeout(updateFoot, 1500);
}

// The page reports its own failures to the server, so a browser on another machine can
// say what went wrong without anyone opening dev tools. Errors land in the server log.
function report(msg, level) {
  try {
    fetch("/api/clientlog", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Pview": "1" },
      body: JSON.stringify({ level: level || "error", msg: String(msg).slice(0, 2000) }),
    }).catch(function () {});
  } catch (e) { /* reporting must never break the page */ }
}

addEventListener("error", function (e) {
  if (e.target && e.target !== window) return;  // resource errors are reported where they happen
  report("js error: " + (e.message || e.error) + " at " + (e.filename || "") + ":" + (e.lineno || 0));
});
addEventListener("unhandledrejection", function (e) {
  report("unhandled rejection: " + ((e.reason && e.reason.message) || e.reason));
});

// ---------------------------------------------------------------- data loading

async function loadList(selectPath, keepPosition) {
  if (!S.dir) return;
  const req = ++S.reqId;
  let data;
  try {
    data = await api("/api/list", { d: S.dir, r: S.recursive ? 1 : 0 });
  } catch (e) {
    setOffline(true);
    return;
  }
  if (req !== S.reqId) return;
  setOffline(false);
  S.listing = data.error ? { dir: S.dir, parent: null, images: [], dirs: [], error: data.error } : data;
  S.sig = data.sig || null;

  const prev = S.items[S.sel];
  const wasTop = keepPosition && prev && prev.kind === "img" && !prev.pin &&
    S.items.findIndex((i) => i.kind === "img" && !i.pin) === S.sel;
  const keep = selectPath || (prev && prev.path);

  buildItems();
  applyFilter();
  if (wasTop) {
    const top = S.items.findIndex((i) => i.kind === "img" && !i.pin);
    select(top >= 0 ? top : 0, true);
  } else {
    selectByPath(keep);
  }
  render();
}

async function setDir(dir, recursive, selectPath) {
  S.dir = dir;
  S.recursive = !!recursive;
  S.filter = "";
  el.filter.value = "";
  el.filter.hidden = true;
  await loadList(selectPath);
  syncUrl();
}

async function loadView(apply) {
  const d = await api("/api/view");
  S.seq = d.seq;
  if (!apply) return d;
  if (d.view) {
    S.pinned = d.view.pinned || [];
    S.source = d.view.source || null;
    await setDir(d.view.dir, d.view.recursive, d.view.select);
  } else if (!S.dir) {
    S.pinned = [];
    await setDir(d.default_dir, false, null);
  }
  return d;
}

async function poll() {
  try {
    const r = await api("/api/poll", { d: S.dir, r: S.recursive ? 1 : 0 });
    setOffline(false);
    if (r.seq > S.seq) {
      await loadView(true);
    } else if (r.sig && r.sig !== S.sig) {
      await loadList(null, true);
    }
  } catch (e) {
    setOffline(true);
  }
  setTimeout(poll, 1800);
}

// ---------------------------------------------------------------- list model

function buildItems() {
  const L = S.listing || { images: [], dirs: [] };
  const pinned = new Set(S.pinned.map((e) => e.p));
  const items = [];
  if (L.parent) items.push({ kind: "up", path: L.parent, label: "../" });
  for (const e of S.pinned) items.push({ kind: "img", path: e.p, label: e.rel, m: e.m, s: e.s, pin: true });
  for (const d of L.dirs || []) items.push({ kind: "dir", path: d.p, label: d.name + "/" });
  let imgs = (L.images || []).filter((e) => !pinned.has(e.p));
  if (S.sort === "name") {
    imgs = imgs.slice().sort((a, b) => a.rel.localeCompare(b.rel, undefined, { numeric: true }));
  }
  for (const e of imgs) items.push({ kind: "img", path: e.p, label: e.rel, m: e.m, s: e.s });
  S.all = items;
}

function applyFilter() {
  if (!S.filter.trim()) {
    S.items = S.all;
    return;
  }
  const terms = S.filter.toLowerCase().split(/\s+/).filter(Boolean);
  S.items = S.all.filter((it) => it.kind === "up" || terms.every((t) => it.label.toLowerCase().includes(t)));
}

function selectByPath(path) {
  let i = path ? S.items.findIndex((it) => it.path === path) : -1;
  if (i < 0) i = S.items.findIndex((it) => it.kind === "img");
  S.sel = Math.max(0, i);
}

// ---------------------------------------------------------------- rendering

function render() {
  const rows = S.items.map((it, i) => {
    const cls = "item " + it.kind + (it.pin ? " pin" : "") + (i === S.sel ? " sel" : "");
    const age = it.kind === "img" ? '<span class="ag" title="' + esc(fmtWhen(it.m)) + '">' + esc(fmtAge(it.m)) + "</span>" : "";
    return '<li class="' + cls + '" data-i="' + i + '" title="' + esc(it.path) + '">' +
      '<span class="nm">' + esc(it.label) + "</span>" + age + "</li>";
  });
  el.list.innerHTML = rows.join("");
  S.nodes = Array.from(el.list.children);
  updateHead();
  updateFoot();
  showSelection();
  scrollToSel();
}

function updateHead() {
  const d = S.dir || "";
  el.path.textContent = d ? "‎" + d : "";
  el.path.title = d;
  const nimg = S.items.filter((i) => i.kind === "img").length;
  el.count.textContent = nimg + (nimg === 1 ? " image" : " images") + (S.filter ? " (filtered)" : "");
  const b = [];
  b.push('<span class="badge' + (S.recursive ? " on" : "") + '" title="r toggles this">subfolders ' +
    (S.recursive ? "on" : "off") + "</span>");
  b.push('<span class="badge">sort: ' + S.sort + "</span>");
  if (S.pinned.length) b.push('<span class="badge on" title="' + esc(S.source || "") + '">' + S.pinned.length + " from screen</span>");
  el.badges.innerHTML = b.join(" ");
}

function updateFoot() {
  if (S.offline) {
    el.status.textContent = "server offline";
    return;
  }
  const L = S.listing || {};
  el.status.textContent = L.error ? L.error : (L.truncated ? "listing truncated" : "");
}

function scrollToSel() {
  const n = S.nodes[S.sel];
  if (n) n.scrollIntoView({ block: "nearest" });
}

function select(i, force) {
  if (!S.items.length) return;
  i = Math.max(0, Math.min(S.items.length - 1, i));
  if (i === S.sel && !force) {
    showSelection();
    return;
  }
  if (S.nodes[S.sel]) S.nodes[S.sel].classList.remove("sel");
  S.sel = i;
  if (S.nodes[S.sel]) S.nodes[S.sel].classList.add("sel");
  scrollToSel();
  showSelection();
  syncUrl();
}

function showSelection() {
  const it = S.items[S.sel];
  if (!it) return;
  if (it.kind === "img") {
    showImage(it);
  } else {
    S.cur = null;
    el.img.hidden = true;
    el.empty.textContent = it.kind === "up" ? "parent folder" : it.label;
    el.fname.textContent = it.label;
    el.fdims.textContent = el.fage.textContent = el.fzoom.textContent = el.fmode.textContent = "";
  }
  preload();
}

function preload() {
  for (const d of [1, 2, -1]) {
    const it = S.items[S.sel + d];
    if (it && it.kind === "img") new Image().src = imgUrl(it);
  }
}

// ---------------------------------------------------------------- image + zoom

function showImage(it) {
  if (S.cur && S.cur.path === it.path) return;
  S.cur = it;
  el.img.hidden = false;
  el.img.style.visibility = "hidden";
  el.empty.textContent = "";
  el.img.src = imgUrl(it);
  updateBar();
}

el.img.addEventListener("load", () => {
  S.nat = { w: el.img.naturalWidth || 800, h: el.img.naturalHeight || 600 };
  doFit();
  el.img.style.visibility = "visible";
  updateBar();
});

el.img.addEventListener("error", () => {
  const it = S.cur;
  el.img.hidden = true;
  el.empty.textContent = "could not load " + (it ? it.label : "");
  if (!it) return;
  // ask the server about the same file, so the message says whose fault it is
  fetch(imgUrl(it), { method: "HEAD" }).then((r) => {
    el.empty.textContent = "could not load " + it.label + " — server answered " + r.status +
      " (" + (r.headers.get("content-type") || "no type") + ", " + (r.headers.get("content-length") || "?") + " bytes)";
    report("image failed to decode: " + it.path + " HEAD=" + r.status + " type=" + r.headers.get("content-type"));
  }).catch((e) => {
    el.empty.textContent = "could not load " + it.label + " — no answer from the server";
    report("image request failed: " + it.path + " " + e);
  });
});

function stageSize() {
  return { W: el.stage.clientWidth, H: el.stage.clientHeight };
}

function fitScale() {
  const { W, H } = stageSize();
  const { w, h } = S.nat;
  if (!w || !h) return 1;
  return S.v.mode === "width" ? W / w : Math.min(W / w, H / h);
}

function doFit() {
  const { W, H } = stageSize();
  const s = fitScale();
  S.v.s = s;
  S.v.fit = true;
  S.v.tx = Math.max(0, (W - S.nat.w * s) / 2);
  S.v.ty = S.nat.h * s <= H ? (H - S.nat.h * s) / 2 : 0;
  apply();
}

function clampT() {
  const { W, H } = stageSize();
  const iw = S.nat.w * S.v.s, ih = S.nat.h * S.v.s;
  S.v.tx = iw <= W ? (W - iw) / 2 : Math.min(0, Math.max(W - iw, S.v.tx));
  S.v.ty = ih <= H ? (H - ih) / 2 : Math.min(0, Math.max(H - ih, S.v.ty));
}

function apply() {
  clampT();
  el.img.style.width = S.nat.w * S.v.s + "px";
  el.img.style.height = S.nat.h * S.v.s + "px";
  el.img.style.transform = "translate(" + S.v.tx + "px," + S.v.ty + "px)";
  updateBar();
}

function zoomAt(px, py, f) {
  if (!S.cur || !S.nat.w) return;
  const fit = fitScale();
  const s0 = S.v.s;
  const s1 = Math.max(fit * 0.5, Math.min(Math.max(fit, 1) * 24, s0 * f));
  if (s1 === s0) return;
  S.v.tx = px - (px - S.v.tx) * (s1 / s0);
  S.v.ty = py - (py - S.v.ty) * (s1 / s0);
  S.v.s = s1;
  S.v.fit = Math.abs(s1 - fit) < 1e-6;
  apply();
}

function zoomCenter(f) {
  const { W, H } = stageSize();
  zoomAt(W / 2, H / 2, f);
}

function pan(dx, dy) {
  if (!S.cur) return;
  S.v.tx += dx;
  S.v.ty += dy;
  S.v.fit = false;
  apply();
}

function updateBar() {
  const it = S.cur;
  if (!it) return;
  el.fname.textContent = it.label;
  el.fname.title = it.path;
  el.fdims.textContent = S.nat.w ? S.nat.w + "×" + S.nat.h : "";
  el.fage.textContent = fmtAge(it.m);
  el.fage.title = fmtWhen(it.m);
  el.fzoom.textContent = Math.round(S.v.s * 100) + "%";
  el.fmode.textContent = S.v.fit ? (S.v.mode === "width" ? "fit width" : "fit") : "free";
}

// ---------------------------------------------------------------- navigation

async function openSel() {
  const it = S.items[S.sel];
  if (!it) return;
  if (it.kind === "dir" || it.kind === "up") {
    S.pinned = [];
    await setDir(it.path, S.recursive, null);
  } else if (it.kind === "img") {
    // on a file, go to the folder it lives in - the useful move when a recursive
    // listing shows something several folders down
    const dir = it.path.slice(0, it.path.lastIndexOf("/"));
    if (dir && dir !== S.dir) {
      S.pinned = [];
      await setDir(dir, S.recursive, it.path);
    }
  }
}

async function goParent() {
  const L = S.listing || {};
  if (!L.parent) return;
  const from = S.dir;
  S.pinned = [];
  await setDir(L.parent, S.recursive, from);
}

async function toggleRecursive() {
  const keep = S.items[S.sel] && S.items[S.sel].path;
  S.recursive = !S.recursive;
  await loadList(keep);
  syncUrl();
}

function toggleSort() {
  const keep = S.items[S.sel] && S.items[S.sel].path;
  S.sort = S.sort === "time" ? "name" : "time";
  buildItems();
  applyFilter();
  selectByPath(keep);
  render();
}

function syncUrl() {
  if (!S.dir) return;
  const q = new URLSearchParams();
  q.set("d", S.dir);
  if (S.recursive) q.set("r", "1");
  const it = S.items[S.sel];
  if (it && it.kind === "img") q.set("f", it.path);
  history.replaceState(null, "", "?" + q.toString());
  const base = S.dir.split("/").filter(Boolean).pop() || "/";
  document.title = (it && it.kind === "img" ? it.label + " — " : "") + base + " — pview";
}

// ---------------------------------------------------------------- filter box

function showFilter() {
  el.filter.hidden = false;
  el.filter.focus();
  el.filter.select();
}

function clearFilter() {
  S.filter = "";
  el.filter.value = "";
  el.filter.hidden = true;
  el.filter.blur();
  const keep = S.items[S.sel] && S.items[S.sel].path;
  applyFilter();
  selectByPath(keep);
  render();
}

el.filter.addEventListener("input", () => {
  const keep = S.items[S.sel] && S.items[S.sel].path;
  S.filter = el.filter.value;
  applyFilter();
  selectByPath(keep);
  render();
});

// ---------------------------------------------------------------- mouse

el.list.addEventListener("mousedown", (e) => {
  const li = e.target.closest("li.item");
  if (!li) return;
  select(Number(li.dataset.i));
});

el.list.addEventListener("dblclick", (e) => {
  const li = e.target.closest("li.item");
  if (li) openSel();
});

el.stage.addEventListener("wheel", (e) => {
  if (!S.cur) return;
  e.preventDefault();
  let d = e.deltaY;
  if (e.deltaMode === 1) d *= 16;
  else if (e.deltaMode === 2) d *= stageSize().H;
  const k = e.ctrlKey ? 0.01 : 0.0022;
  const r = el.stage.getBoundingClientRect();
  zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-d * k));
}, { passive: false });

let gscale = 1;
el.stage.addEventListener("gesturestart", (e) => { e.preventDefault(); gscale = e.scale; });
el.stage.addEventListener("gesturechange", (e) => {
  e.preventDefault();
  const r = el.stage.getBoundingClientRect();
  const f = e.scale / (gscale || 1);
  gscale = e.scale;
  zoomAt(e.clientX - r.left, e.clientY - r.top, f);
});
el.stage.addEventListener("gestureend", (e) => e.preventDefault());

let drag = null;
el.stage.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || !S.cur) return;
  el.stage.setPointerCapture(e.pointerId);
  drag = { x: e.clientX, y: e.clientY };
  el.stage.classList.add("drag");
});
el.stage.addEventListener("pointermove", (e) => {
  if (!drag) return;
  pan(e.clientX - drag.x, e.clientY - drag.y);
  drag.x = e.clientX;
  drag.y = e.clientY;
});
for (const t of ["pointerup", "pointercancel", "pointerleave"]) {
  el.stage.addEventListener(t, () => { drag = null; el.stage.classList.remove("drag"); });
}

el.stage.addEventListener("dblclick", (e) => {
  if (!S.cur) return;
  const r = el.stage.getBoundingClientRect();
  const fit = fitScale();
  if (Math.abs(S.v.s - fit) < 1e-6) {
    zoomAt(e.clientX - r.left, e.clientY - r.top, Math.max(3, 1 / Math.max(fit, 0.01)));
  } else {
    doFit();
  }
});

new ResizeObserver(() => {
  if (!S.cur || !S.nat.w) return;
  if (S.v.fit) doFit();
  else apply();
}).observe(el.stage);

// ---------------------------------------------------------------- keyboard

const PAN = 80;

addEventListener("keydown", async (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.activeElement === el.filter) {
    if (e.key === "Escape") { clearFilter(); e.preventDefault(); }
    else if (e.key === "Enter") { el.filter.blur(); e.preventDefault(); }
    else if (e.key === "ArrowDown") { select(S.sel + 1); e.preventDefault(); }
    else if (e.key === "ArrowUp") { select(S.sel - 1); e.preventDefault(); }
    return;
  }
  if (!el.help.hidden && e.key !== "?") { el.help.hidden = true; }
  let handled = true;
  switch (e.key) {
    case "j": case "ArrowDown": select(S.sel + 1); break;
    case "k": case "ArrowUp": select(S.sel - 1); break;
    case "PageDown": select(S.sel + 10); break;
    case "PageUp": select(S.sel - 10); break;
    case "g": case "Home": select(0); break;
    case "G": case "End": select(S.items.length - 1); break;
    case "l": case "ArrowRight": case "Enter": await openSel(); break;
    case "h": case "ArrowLeft": case "Backspace": await goParent(); break;
    case "/": showFilter(); break;
    case "r": await toggleRecursive(); break;
    case "s": toggleSort(); break;
    case "+": case "=": zoomCenter(1.25); break;
    case "-": case "_": zoomCenter(1 / 1.25); break;
    case "0": doFit(); break;
    case "1": {
      const { W, H } = stageSize();
      zoomAt(W / 2, H / 2, 1 / S.v.s);
      break;
    }
    case "w":
      S.v.mode = S.v.mode === "width" ? "contain" : "width";
      doFit();
      break;
    case "H": pan(PAN, 0); break;
    case "L": pan(-PAN, 0); break;
    case "K": pan(0, PAN); break;
    case "J": pan(0, -PAN); break;
    case "o":
      if (S.cur) window.open(imgUrl(S.cur), "_blank", "noopener");
      break;
    case "y":
      if (S.cur && navigator.clipboard) navigator.clipboard.writeText(S.cur.path).then(() => flash("path copied"), () => flash("copy failed"));
      break;
    case "?": el.help.hidden = !el.help.hidden; break;
    case "Escape":
      if (S.filter) clearFilter();
      else doFit();
      break;
    default: handled = false;
  }
  if (handled) e.preventDefault();
});

el.help.addEventListener("click", () => { el.help.hidden = true; });

// ---------------------------------------------------------------- start

(async function init() {
  const q = new URLSearchParams(location.search);
  try {
    if (q.get("d")) {
      await setDir(q.get("d"), q.get("r") === "1", q.get("f"));
      await loadView(false);
    } else {
      await loadView(true);
    }
  } catch (e) {
    setOffline(true);
    report("init failed: " + e);
  }
  poll();
  setTimeout(() => {
    const it = S.items[S.sel];
    report("loaded: " + navigator.userAgent +
      " window=" + innerWidth + "x" + innerHeight + " dpr=" + devicePixelRatio +
      " stage=" + el.stage.clientWidth + "x" + el.stage.clientHeight +
      " items=" + S.items.length + " sel=" + (it ? it.kind + ":" + it.label : "none") +
      " img=" + (S.cur ? (el.img.complete ? "complete " + el.img.naturalWidth + "x" + el.img.naturalHeight +
        " shown " + Math.round(el.img.getBoundingClientRect().width) + "x" +
        Math.round(el.img.getBoundingClientRect().height) : "still loading") : "none"), "info");
  }, 2500);
})();
