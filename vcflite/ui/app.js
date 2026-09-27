"use strict";

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;
const IS_MAC = /Mac/.test(navigator.platform || navigator.userAgent);
const MOD = IS_MAC ? "⌘" : "Ctrl+";
const PAGE = 150;
const MAX_ROWS = 1500;

const S = {
  showAll: false,   // show INFO, FORMAT and full sample columns
  focus: null,      // index of the sample picked in the sidebar (null = all samples)
  short: [],        // display names for the samples (shared prefix/suffix removed)
  rowData: new Map(),
  file: null,
  top: null,        // offset of first row in the DOM
  next: null,       // offset right after the last row in the DOM
  eof: false,
  atStart: true,
  hideRef: false,
  busyAfter: false,
  busyBefore: false,
  selOffset: null,
  status: {},
  poll: null,
  search: null,     // {text, lastMatch}
  pendingGoto: null,
  msgTimer: null,
};

/* ------------------------------------------------------------------ utils */

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function fmtSize(n) {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i];
}
const fmtPos = (p) => Number(p).toLocaleString("en-US");

function gtClass(gt) {
  const a = gt.split(/[\/|]/);
  if (a.every((x) => x === "." || x === "")) return "miss";
  if (a.every((x) => x === "0" || x === ".")) return "ref";
  const called = a.filter((x) => x !== ".");
  return called.every((x) => x === called[0]) ? "hom" : "het";
}

async function copyText(t) {
  try { await navigator.clipboard.writeText(t); return; } catch (e) { /* fall through */ }
  const ta = document.createElement("textarea");
  ta.value = t;
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  ta.remove();
}

// Sample names often share a long prefix/suffix ("Sample_Diag-excap51-HG002-
// EEogPU"); drop the shared part, cutting only at separators, so they read as
// "HG002".  Falls back to the full names if that would make them ambiguous.
function shortNames(names) {
  if (names.length < 2) return names.slice();
  const sep = /[_\-.\s:|]/;
  let pre = names.reduce((a, b) => { let i = 0; while (i < a.length && a[i] === b[i]) i++; return a.slice(0, i); });
  let cut = pre.length;
  while (cut > 0 && !sep.test(pre[cut - 1])) cut--;          // back up to a separator
  let suf = names.reduce((a, b) => { let i = 0; while (i < a.length && i < b.length && a[a.length - 1 - i] === b[b.length - 1 - i]) i++; return a.slice(a.length - i); });
  let scut = 0;
  while (scut < suf.length && !sep.test(suf[scut])) scut++;  // forward to a separator
  const sufLen = suf.length - scut;
  const out = names.map((n) => n.slice(cut, n.length - sufLen) || n);
  return new Set(out).size === names.length ? out : names.slice();
}

// The sample index to ask the backend for: none in "show all columns" mode.
const pick = () => (S.showAll ? null : S.focus);
const multi = () => S.file && S.file.samples.length > 1;

// Rendered width of header text (same font as the table's sample headers).
function textWidth(text, size) {
  const ctx = (textWidth.c ||= document.createElement("canvas").getContext("2d"));
  ctx.font = `${size} ${getComputedStyle(document.body).fontFamily}`;
  return ctx.measureText(text).width;
}

/* --------------------------------------------------------------- messages */

function setMsg(html, { error = false, sticky = false } = {}) {
  clearTimeout(S.msgTimer);
  $("st-msg").innerHTML = error ? `<span class="err">${html}</span>` : html;
  if (!sticky && html) S.msgTimer = setTimeout(() => ($("st-msg").innerHTML = ""), error ? 6000 : 4000);
}

function progressBar(x) {
  return `<span class="bar-prog"><i style="width:${Math.round(x * 100)}%"></i></span>`;
}

/* ---------------------------------------------------------------- welcome */

async function showWelcome(error) {
  $("viewer").hidden = true;
  $("welcome").hidden = false;
  const err = $("welcome-error");
  err.hidden = !error;
  err.textContent = error || "";
  const rec = await api().recent();
  const ul = $("recent");
  ul.innerHTML = "";
  for (const p of rec.slice(0, 5)) {
    const li = document.createElement("li");
    const name = p.split(/[\\/]/).pop();
    const dir = p.slice(0, p.length - name.length);
    li.innerHTML = `<span class="n">${esc(name)}</span><span class="p">${esc(dir)}</span>`;
    li.title = p;
    li.onclick = () => openPath(p);
    ul.appendChild(li);
  }
}

/* ------------------------------------------------------------------ open */

async function chooseFile() {
  const p = await api().choose_file();
  if (p) openPath(p);
}

let readyResolve;
const ready = new Promise((r) => (readyResolve = r));

async function openPath(path) {
  await ready;
  if (S.file) setMsg(`Opening ${esc(path.split(/[\\/]/).pop())}…`, { sticky: true });
  document.body.style.cursor = "progress";
  let res;
  try {
    res = await api().open(path);
  } finally {
    document.body.style.cursor = "";
  }
  if (res.error) {
    if (S.file) setMsg(esc(res.error), { error: true });
    else showWelcome(res.error);
    return;
  }
  setupFile(res);
}

function setupFile(f) {
  S.file = f;
  S.status = {};
  S.search = null;
  S.pendingGoto = null;
  S.selOffset = null;
  S.hideRef = false;
  S.focus = null;
  S.hasRef = false;
  S.short = shortNames(f.samples);
  S.rowData = new Map();
  fillSamplePicker(f);
  $("hide-ref").checked = false;
  $("hide-ref-wrap").hidden = true;
  $("welcome").hidden = true;
  $("viewer").hidden = false;
  $("detail").hidden = true;
  showHeader(false);
  document.title = "VCF Lite - " + f.name;

  // Let long names wrap after "_", "." and "-" rather than mid-word.
  $("i-name").innerHTML = esc(f.name).replace(/([_.\-])/g, "$1<wbr>");
  $("i-name").title = f.path + "\nClick to show in " + (IS_MAC ? "Finder" : "file browser");
  const n = f.samples.length;
  $("i-samples-label").textContent = n === 1 ? "Sample" : "Samples";
  $("i-samples").textContent = n === 0 ? "none" : n === 1 ? f.samples[0] : n.toLocaleString();
  $("i-samples").title = f.samples.slice(0, 50).join(", ") + (n > 50 ? ", …" : "");
  $("i-size").textContent = fmtSize(f.size);
  for (const id of ["pos", "rsid"]) { $(id).value = ""; $(id).classList.remove("bad"); }
  $("rsid-msg").hidden = true;
  setMsg("");

  setupColumns(f);
  fillContigs(f.contigs);
  renderHeaderText("");
  renderInfo();
  renderFileInfo();
  renderPage(f.page);
  if (f.indexNote) setMsg(esc(f.indexNote), { error: true });
  if (f.compression === "gzip") {
    setMsg("This file is gzip- rather than bgzip-compressed, so jumping around is slow. " +
           "Re-compress it with <code>bgzip</code> for instant search.", { sticky: true });
  }
  startPolling();
  $("table-wrap").focus();
}

/* ---------------------------------------------------------------- columns */

const FIXED_W = { CHROM: 68, POS: 84, ID: 100, REF: 56, ALT: 56, QUAL: 82, FILTER: 62, INFO: 340, FORMAT: 110 };

// Which columns the table shows.  Compact (default): the fixed columns up to
// FILTER plus each sample's genotype.  Full: everything, as in the file.
function viewColumns(f) {
  // Fixed columns are looked up by name, so the table's order never depends
  // on the file's.
  const byName = (name) => {
    const i = f.columns.indexOf(name);
    return { src: i, label: name, kind: name };
  };
  if (S.showAll) {
    return [
      ...f.columns.slice(0, 9).map((_, i) => ({ src: i, label: f.columns[i], kind: f.columns[i] })),
      ...f.samples.slice(0, f.tableSamples).map((name, s) => ({ src: 9 + s, label: name, kind: "sample", sample: name })),
    ];
  }
  const cols = ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER"].map(byName);
  if (multi() && S.focus == null) {
    // All samples: one genotype column per sample, headed by its name.
    f.samples.slice(0, f.tableSamples).forEach((name, s) =>
      cols.push({ src: 9 + s, label: S.short[s], kind: "gt", sample: name }));
    return cols;
  }
  // One sample (the only one, or the one picked; the backend then sends it as
  // column 9): GT, Ref reads, Alt reads.  Kept, blank, when there are none.
  const name = S.focus != null ? f.samples[S.focus] : f.samples[0] || null;
  cols.push({ src: 9, label: "GT", kind: "gt", sample: name });
  cols.push({ src: 9, label: "Ref reads", kind: "refreads", sample: name });
  cols.push({ src: 9, label: "Alt reads", kind: "altreads", sample: name });
  return cols;
}

// Reads supporting REF and ALT for sample column ``src``: FORMAT/AD, else
// INFO/DP4 (bcftools: ref-fwd, ref-rev, alt-fwd, alt-rev).  "" if unknown.
function readCounts(c, src) {
  const keys = (c[8] || "").split(":");
  const k = keys.indexOf("AD");
  if (k >= 0 && c[src] !== undefined) {
    const ad = c[src].split(":")[k];
    if (ad && ad !== ".") {
      const parts = ad.split(",");
      return { ref: parts[0] === "." ? "" : parts[0], alt: parts.slice(1).join(","), from: "FORMAT/AD" };
    }
  }
  const m = (c[7] || "").match(/(?:^|;)DP4=(\d+),(\d+),(\d+),(\d+)/);
  if (m) return { ref: String(+m[1] + +m[2]), alt: String(+m[3] + +m[4]), from: "INFO/DP4" };
  return { ref: "", alt: "", from: null };
}

function setupColumns(f) {
  const cols = viewColumns(f);
  S.view = cols;
  // Size CHROM for the main chromosomes; long decoy/alt contig names ellipsize.
  const main = f.contigs.filter((c) => /^(chr)?([0-9]+|X|Y|MT?)$/i.test(c));
  const longest = Math.max(4, ...(main.length ? main : f.contigs.slice(0, 30)).map((c) => c.length));
  const widths = cols.map((c) => {
    if (c.src === 0) return Math.min(110, Math.max(68, 18 + longest * 7.6));
    if (c.kind === "gt" || c.kind === "refreads" || c.kind === "altreads") {
      // GT under a sample name (all-samples view) needs room for the name.
      if (c.kind === "gt" && c.label !== "GT") return Math.max(52, Math.min(160, Math.ceil(textWidth(c.label, "12px")) + 20));
      return { gt: 52, refreads: 78, altreads: 78 }[c.kind];
    }
    if (c.kind === "sample") return f.samples.length === 1 ? 200 : Math.max(130, Math.min(220, 40 + c.label.length * 7.5));
    if (c.kind === "INFO" && cols.length <= 8) return 600;
    return FIXED_W[c.kind] || 100;
  });
  S.widths = widths;
  // The column that soaks up spare width: INFO when shown, else the last one.
  const info = cols.findIndex((c) => c.kind === "INFO");
  S.flexCol = info >= 0 ? info : cols.length - 1;
  const cg = $("cols");
  cg.innerHTML = widths.map((w) => `<col style="width:${w}px">`).join("");
  const th = (c, i, extra = "") => {
    const cls = c.src >= 9 ? "sample" : "";
    const who = c.sample ? ` of ${c.sample}` : "";
    const title = { gt: `Genotype${who}`, refreads: `Reads supporting REF${who}: FORMAT/AD, or INFO/DP4 if there's no AD`,
                    altreads: `Reads supporting ALT${who}: FORMAT/AD, or INFO/DP4 if there's no AD` }[c.kind] || c.label;
    return `<th class="${cls}" ${extra} title="${esc(title)}">${esc(c.label)}<span class="rs" data-i="${i}"></span></th>`;
  };
  // "+N more" column when not every sample fits (never in single-sample view).
  const more = pick() == null && (S.showAll || multi()) ? f.samples.length - f.tableSamples : 0;
  S.moreCol = more > 0;
  const moreTh = more > 0 ? `<th class="sample" title="Click a row to see all samples">+${more.toLocaleString()} more</th>` : "";
  $("thead").innerHTML = `<tr>${cols.map((c, i) => th(c, i)).join("")}${moreTh}</tr>`;
  if (more > 0) cg.insertAdjacentHTML("beforeend", `<col style="width:120px">`);
  $("cols-btn").textContent = S.showAll ? "Fewer columns" : "Show all columns";
  updateTableWidth();
}

// INFO absorbs any spare horizontal space so the table fills the window.
function updateTableWidth() {
  const cols = [...$("cols").children];
  if (!cols.length) return;
  const w = cols.map((c, i) => (i === S.flexCol ? S.widths[i] : parseFloat(c.style.width)));
  let total = w.reduce((a, b) => a + b, 0);
  const avail = $("table-wrap").clientWidth || total;
  if (S.flexCol >= 0 && total < avail) {
    w[S.flexCol] += avail - total;
    total = avail;
  } else if (S.flexCol >= 0 && total > avail && S.view[S.flexCol].kind === "INFO") {
    // Tight on space: INFO gives up width first (it's truncated anyway).
    const give = Math.min(total - avail, w[S.flexCol] - 130);
    if (give > 0) { w[S.flexCol] -= give; total -= give; }
  }
  if (S.flexCol >= 0) cols[S.flexCol].style.width = w[S.flexCol] + "px";
  $("grid").style.width = total + "px";
}

// Column resizing
document.addEventListener("mousedown", (e) => {
  const h = e.target.closest(".rs");
  if (!h) return;
  e.preventDefault();
  const col = $("cols").children[+h.dataset.i];
  const x0 = e.clientX;
  const w0 = parseFloat(col.style.width);
  const i = +h.dataset.i;
  const move = (ev) => {
    const nw = Math.max(40, w0 + ev.clientX - x0);
    col.style.width = nw + "px";
    if (i === S.flexCol) S.widths[i] = nw;
    updateTableWidth();
  };
  const up = () => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
});

function fillContigs(contigs) {
  const sel = $("chrom");
  const cur = sel.value;
  sel.innerHTML = contigs.map((c) => `<option>${esc(c)}</option>`).join("");
  if (contigs.includes(cur)) sel.value = cur;
}

/* ------------------------------------------------------------------- rows */

function gtCell(v, hasGT, full) {
  if (!hasGT) return `<td>${esc(full ? v : v.split(":")[0])}</td>`;
  const k = v.indexOf(":");
  const gt = k < 0 ? v : v.slice(0, k);
  const rest = full && k >= 0 ? v.slice(k + 1) : "";
  return `<td><span class="gt ${gtClass(gt)}">${esc(gt)}</span>` +
         (rest ? `<span class="fx">:</span>${esc(rest).replace(/:/g, '<span class="fx">:</span>')}` : "") + "</td>";
}

function rowHTML(r) {
  const c = r.c;
  const hasGT = !!(c[8] && c[8].startsWith("GT"));
  let h = "";
  let reads = null;
  for (const col of S.view) {
    const v = c[col.src] ?? "";
    if (col.kind === "refreads" || col.kind === "altreads") {
      if (!reads || reads.src !== col.src) reads = { src: col.src, ...readCounts(c, col.src) };
      h += `<td class="c-reads">${esc(col.kind === "refreads" ? reads.ref : reads.alt)}</td>`;
      continue;
    }
    if (col.kind === "gt") { h += hasGT && col.src < c.length ? gtCell(v, true, false) : "<td></td>"; continue; }
    if (col.src >= 9) { h += gtCell(v, hasGT, col.kind === "sample"); continue; }
    if (col.src < 0) { h += "<td></td>"; continue; }
    switch (col.src) {
      case 0: h += `<td class="c-chrom">${esc(v)}</td>`; break;
      case 1: h += `<td class="c-pos">${esc(v)}</td>`; break;
      case 2: h += `<td class="c-id" title="${esc(v)}">${esc(v)}</td>`; break;
      case 3: case 4: h += `<td title="${v.length > 8 ? esc(v) : ""}">${esc(v)}</td>`; break;
      case 6: h += `<td class="c-filter ${v === "PASS" || v === "." ? "pass" : "fail"}" title="${v.length > 7 ? esc(v) : ""}">${esc(v)}</td>`; break;
      case 7: h += `<td class="c-info">${esc(v)}</td>`; break;
      case 8: h += `<td class="c-format">${esc(v)}</td>`; break;
      default: h += `<td>${esc(v)}</td>`;
    }
  }
  if (S.moreCol) h += "<td></td>";
  return h;
}

// Reload the table from the first visible row (after the sample, column set
// or hom-ref filter changes), keeping the selection if it's still there.
async function reloadHere() {
  const [first] = visibleRows();
  const off = first ? +first.dataset.o : S.top;
  const page = await api().page_at(off, PAGE, S.hideRef, false, pick());
  renderPage(page);
}

// Re-render the rows already loaded (e.g. after changing the column set).
function rerenderRows() {
  const byOffset = S.rowData;
  for (const tr of dataRows()) {
    const r = byOffset.get(+tr.dataset.o);
    if (r) tr.innerHTML = rowHTML(r);
  }
}

function makeRows(rows) {
  const frag = document.createDocumentFragment();
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.dataset.o = r.o;
    tr.className = r.r ? "ref" : "var";
    S.rowData.set(r.o, r);
    if (r.o === S.selOffset) tr.classList.add("sel");
    tr.innerHTML = rowHTML(r);
    frag.appendChild(tr);
  }
  return frag;
}

const body = () => $("body");
const dataRows = () => body().querySelectorAll("tr[data-o]");

function renderPage(page, { flashFirst = false } = {}) {
  const b = body();
  b.innerHTML = "";
  S.rowData.clear();
  b.appendChild(makeRows(page.rows));
  S.top = page.rows.length ? page.rows[0].o : page.next;
  S.next = page.next;
  S.eof = page.eof;
  S.atStart = !!page.atStart;
  updateEdges();
  $("table-wrap").scrollTop = 0;
  if (!page.rows.length) {
    b.innerHTML = `<tr class="divider"><td colspan="99">${S.hideRef ? "No variant records here." : "No records."}</td></tr>`;
  }
  if (flashFirst && b.firstElementChild) flash(b.firstElementChild);
  updateRange();
  // Give the user something to scroll up into.
  if (!S.atStart) loadBefore({ keepTop: true });
  else maybeLoadMore();
}

function updateEdges() {
  const b = body();
  b.querySelectorAll("tr.divider").forEach((d) => d.remove());
  if (S.atStart && b.firstElementChild) {
    b.insertAdjacentHTML("afterbegin", `<tr class="divider"><td colspan="99">Start of file</td></tr>`);
  }
  if (S.eof && b.lastElementChild) {
    b.insertAdjacentHTML("beforeend", `<tr class="divider"><td colspan="99">End of file</td></tr>`);
  }
}

function flash(tr) {
  tr.classList.remove("flash");
  void tr.offsetWidth;
  tr.classList.add("flash");
}

async function loadAfter() {
  if (S.busyAfter || S.eof || S.next == null) return;
  S.busyAfter = true;
  const file = S.file;
  try {
    let r = await api().rows(S.next, PAGE, S.hideRef, pick());
    if (file !== S.file) return;
    let spins = 0;
    // When hiding hom-ref rows we may scan a long way; keep the user informed.
    while (!r.rows.length && !r.eof && r.next != null && spins < 200) {
      spins++;
      setMsg("Skipping hom-ref records…", { sticky: true });
      r = await api().rows(r.next, PAGE, S.hideRef, pick());
      if (file !== S.file) return;
    }
    if (spins) setMsg("");
    body().querySelectorAll("tr.divider").forEach((d) => d.remove());
    body().appendChild(makeRows(r.rows));
    S.next = r.next;
    S.eof = r.eof;
    trimTop();
    updateEdges();
    updateRange();
  } finally {
    S.busyAfter = false;
  }
  maybeLoadMore();
}

async function loadBefore({ keepTop = false } = {}) {
  if (S.busyBefore || S.atStart || S.top == null) return;
  if (!S.status.indexReady) return; // needs an index; retried once it's ready
  S.busyBefore = true;
  const file = S.file;
  try {
    const r = await api().before(S.top, PAGE, S.hideRef, pick());
    if (file !== S.file || r.pending) return;
    const wrap = $("table-wrap");
    const b = body();
    const firstData = b.querySelector("tr[data-o]");
    const anchorTop = firstData ? firstData.offsetTop : 0;
    const prevScroll = wrap.scrollTop;
    b.insertBefore(makeRows(r.rows), b.firstChild);
    if (r.rows.length) S.top = r.rows[0].o;
    S.atStart = r.atStart;
    trimBottom();
    updateEdges();
    const shift = firstData ? firstData.offsetTop - anchorTop : 0;
    if (keepTop) {
      // Keep the jump target near the top, with a few rows of context above.
      wrap.scrollTop = Math.max(0, shift - 3 * rowHeight());
    } else {
      wrap.scrollTop = prevScroll + shift;
    }
    updateRange();
  } finally {
    S.busyBefore = false;
  }
}

function rowHeight() {
  const r = body().querySelector("tr[data-o]");
  return r ? r.offsetHeight : 25;
}

function trimTop() {
  const rows = dataRows();
  const extra = rows.length - MAX_ROWS;
  if (extra <= 0) return;
  const wrap = $("table-wrap");
  const keep = rows[extra];
  const before = keep.offsetTop;
  for (let i = 0; i < extra; i++) { S.rowData.delete(+rows[i].dataset.o); rows[i].remove(); }
  body().querySelectorAll("tr.divider").forEach((d) => d.remove());
  wrap.scrollTop -= before - keep.offsetTop;
  S.top = +keep.dataset.o;
  S.atStart = false;
}

function trimBottom() {
  const rows = dataRows();
  const extra = rows.length - MAX_ROWS;
  if (extra <= 0) return;
  const firstGone = rows[rows.length - extra];
  S.next = +firstGone.dataset.o;
  S.eof = false;
  for (let i = rows.length - extra; i < rows.length; i++) { S.rowData.delete(+rows[i].dataset.o); rows[i].remove(); }
}

function maybeLoadMore() {
  const w = $("table-wrap");
  const nearBottom = w.scrollHeight - w.scrollTop - w.clientHeight < 1200;
  if (nearBottom && !S.eof) loadAfter();
}

let rangeRAF = 0;
function onScroll() {
  const w = $("table-wrap");
  if (w.scrollHeight - w.scrollTop - w.clientHeight < 1200) loadAfter();
  if (w.scrollTop < 600) loadBefore();
  if (!rangeRAF) rangeRAF = requestAnimationFrame(() => { rangeRAF = 0; updateRange(); });
}

function visibleRows() {
  const w = $("table-wrap");
  const rows = dataRows();
  if (!rows.length) return [];
  const headH = $("thead").offsetHeight;
  const top = w.scrollTop + headH;
  const bottom = w.scrollTop + w.clientHeight;
  // binary search for first visible row
  let lo = 0, hi = rows.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].offsetTop + rows[mid].offsetHeight <= top) lo = mid + 1; else hi = mid;
  }
  let last = lo;
  while (last + 1 < rows.length && rows[last + 1].offsetTop < bottom) last++;
  return [rows[lo], rows[last]];
}

function updateRange() {
  const [a, b] = visibleRows();
  if (!a) { $("st-range").textContent = ""; return; }
  const p = (tr) => `${tr.children[0].textContent}:${fmtPos(tr.children[1].textContent)}`;
  $("st-range").textContent = p(a) + "  –  " + p(b);
  // Keep the chromosome picker on what you're looking at: the selected row if
  // it's on screen, else the middle visible row (not the context rows at the
  // top edge, which can belong to the previous chromosome).
  const sel = $("chrom");
  if (document.activeElement === sel) return;
  const rows = [...dataRows()];
  const i0 = rows.indexOf(a), i1 = rows.indexOf(b);
  const picked = rows.slice(i0, i1 + 1).find((r) => r.classList.contains("sel")) || rows[(i0 + i1) >> 1];
  const c = picked && picked.children[0].textContent;
  if (c && sel.value !== c && [...sel.options].some((o) => o.value === c)) sel.value = c;
}

/* ------------------------------------------------------------- navigation */

async function goPos() {
  const chrom = $("chrom").value;
  const pos = $("pos").value.trim();
  if (!chrom) return;
  $("pos").classList.remove("bad");
  const r = await api().goto(chrom, pos, S.hideRef, pick());
  if (r.error) {
    if (r.indexing) {
      S.pendingGoto = true;
      setMsg(`Indexing the file first… ${progressBar(S.status.indexProgress || 0)}`, { sticky: true });
      return;
    }
    if (r.field === "pos") $("pos").classList.add("bad");
    setMsg(esc(r.error), { error: true });
    return;
  }
  renderPage(r.page);
  selectOffset(r.page.rows.length ? r.page.rows[0].o : null);
  setMsg(r.note ? esc(r.note) : "");
}

// Select (lime highlight + info box) the row at ``offset``, if it's loaded.
function selectOffset(offset) {
  if (offset == null) return;
  const tr = body().querySelector(`tr[data-o="${offset}"]`);
  if (tr) selectRow(tr);
}

async function findId() {
  const text = $("rsid").value.trim();
  if (!text) return;
  $("rsid").classList.remove("bad");
  $("rsid-msg").hidden = true;
  // Same ID again = next match.
  if (S.search && S.search.input === text && S.search.lastMatch != null) return findNext();
  const r = await api().find_id(text);
  if (r.error) { setMsg(esc(r.error), { error: true }); return; }
  S.search = { text: r.text, input: text, lastMatch: null };
  showSearchProgress(0);
  startPolling();
}

async function findNext() {
  if (!S.search || S.search.lastMatch == null) return;
  const r = await api().find_id(S.search.text, S.search.lastMatch);
  if (r.error) { setMsg(esc(r.error)); return; }
  showSearchProgress(0);
  startPolling();
}

function showSearchProgress(x) {
  setMsg(`Searching for ${esc(S.search.text)} ${progressBar(x)} <button class="link" id="cancel-search">Cancel</button>`, { sticky: true });
  $("cancel-search").onclick = () => api().cancel();
}

async function jumpTo(offset, { select = false } = {}) {
  const page = await api().page_at(offset, PAGE, S.hideRef, true, pick());
  renderPage(page, { flashFirst: !select });
  if (select && page.rows.length) selectOffset(page.rows[0].o);
}

async function goStart() {
  const page = await api().page_at(null, PAGE, S.hideRef, false, pick());
  renderPage(page);
}

/* ---------------------------------------------------------------- polling */

function startPolling() {
  if (S.poll) return;
  const tick = async () => {
    const file = S.file;
    let st;
    try { st = await api().status(); } catch (e) { st = null; }
    if (!st || file !== S.file) { S.poll = null; return; }
    handleStatus(st);
    const busy = !st.indexReady || !st.build || !st.kind || !st.coverage || !st.phased || (st.task && !st.task.done);
    S.poll = busy ? setTimeout(tick, 200) : null;
  };
  S.poll = setTimeout(tick, 50);
}

function handleStatus(st) {
  const prev = S.status;
  S.status = st;
  if (st.contigs) fillContigsOnce(st.contigs);
  const sig = (x) => JSON.stringify([x.build, x.kind, x.coverage, x.phased]);
  if (sig(prev) !== sig(st)) {
    renderInfo();
  }
  renderFileInfo();
  if (st.indexError) setMsg("Indexing failed: " + esc(st.indexError), { error: true });
  if (!st.indexReady && S.pendingGoto) {
    setMsg(`Indexing the file first… ${progressBar(st.indexProgress || 0)}`, { sticky: true });
  }
  if (st.indexReady && !prev.indexReady) {
    if (S.pendingGoto) {
      S.pendingGoto = null;
      setMsg("");
      goPos();
    } else if (!S.atStart) {
      loadBefore();
    }
  }
  const t = st.task;
  if (t && t.kind === "search" && S.search) {
    if (!t.done) {
      showSearchProgress(t.progress);
    } else if (!S.search.handled || S.search.handled !== t) {
      S.search.handled = t;
      if (t.error === "cancelled") {
        setMsg("Search cancelled");
      } else if (t.error) {
        setMsg("Search failed: " + esc(t.error), { error: true });
      } else if (t.result && t.result.offset != null) {
        const first = S.search.lastMatch == null;
        S.search.lastMatch = t.result.offset;
        jumpTo(t.result.offset, { select: true });
        setMsg(`Found ${esc(S.search.text)}. Press Enter again for the next match.`, { sticky: true });
      } else {
        const none = S.search.lastMatch == null;
        if (none) {
          setMsg("");
          $("rsid-msg").hidden = false;  // "Not found." under the field
          $("rsid").classList.add("bad");
          S.search = null;
        } else {
          setMsg(`No more matches for ${esc(S.search.text)}`);
        }
      }
    }
  }
}

let contigsFilled = null;
function fillContigsOnce(c) {
  if (contigsFilled === S.file) return;
  contigsFilled = S.file;
  S.file.contigs = c;
  fillContigs(c);
}

function renderInfo() {
  const st = S.status;
  const b = $("i-build");
  if (!st.build) b.innerHTML = `<span class="pending">detecting…</span>`;
  else if (!st.build.build) b.innerHTML = `<span class="unknown">unknown</span>`;
  else b.innerHTML = `${esc(st.build.build)}<span class="why">from ${esc(st.build.why)}</span>`;
  b.title = st.build ? st.build.why : "";

  const ph = $("i-phased");
  if (!st.phased) ph.innerHTML = `<span class="pending">checking…</span>`;
  else {
    const p = st.phased;
    // "true"/"false" are self-explanatory; explain n/a and mixed.
    const why = p.label === "mixed" || p.label === "n/a" ? `<span class="why">${esc(p.why)}</span>` : "";
    ph.innerHTML = `<span class="${p.label === "n/a" ? "unknown" : ""}">${esc(p.label)}</span>${why}`;
    ph.title = p.why;
  }

  const cv = $("i-cov");
  if (!st.coverage) cv.innerHTML = `<span class="pending">estimating…</span>`;
  else {
    const c = st.coverage;
    const plain = c.label === "unknown" || c.label === "n/a";
    cv.innerHTML = `<span class="${plain ? "unknown" : ""}">${esc(c.label)}</span><span class="why">${esc(c.why)}</span>`;
    cv.title = c.tip || "";
  }

  const h = $("i-homref");
  if (!st.kind) {
    h.innerHTML = `<span class="pending">checking…</span>`;
  } else {
    // "Variant-only": false when the file also has hom-ref / reference records.
    const hasRef = st.kind.kind === "homref" || st.kind.kind === "gvcf";
    if (st.kind.kind === "empty") h.innerHTML = `<span class="unknown">n/a</span><span class="why">no records</span>`;
    else h.innerHTML = hasRef ? `false${st.kind.kind === "gvcf" ? `<span class="why">gVCF with reference blocks</span>` : ""}` : "true";
    S.hasRef = hasRef;
    updateHideRefToggle();
  }
}

function updateHideRefToggle() {
  const perSample = multi() && pick() != null;
  const show = !!S.hasRef || perSample;
  $("hide-ref-wrap").hidden = !show;
  $("hide-ref-wrap").title = perSample
    ? `Only show records where ${S.short[S.focus]} carries an ALT allele`
    : "Only show records where a sample carries an ALT allele";
  if (!show && S.hideRef) { S.hideRef = false; $("hide-ref").checked = false; }
}

function fillSamplePicker(f) {
  $("sample-form").hidden = f.samples.length < 2;
  $("sample-pick").innerHTML = `<option value="">All samples</option>` +
    f.samples.map((n, i) => `<option value="${i}" title="${esc(n)}">${esc(S.short[i])}</option>`).join("");
}

function renderFileInfo() {
  const f = S.file;
  const st = S.status;
  const comp = { bgzf: "bgzip", gzip: "gzip", text: "plain text" }[f.compression] || f.compression;
  let idx;
  const kind = f.index || (st.indexReady ? "scan" : null);
  if (kind === "tbi") idx = "tabix index";
  else if (kind === "csi") idx = "CSI index";
  else if (kind === "scan" || st.indexReady) idx = "indexed";
  else idx = `indexing ${Math.round((st.indexProgress || 0) * 100)}%`;
  $("st-file").textContent = `${comp} · ${idx}`;
}

/* ----------------------------------------------------------------- detail */

function selectRow(tr) {
  if (!tr || !tr.dataset.o) return;
  body().querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel"));
  tr.classList.add("sel");
  S.selOffset = +tr.dataset.o;
  showDetail(S.selOffset);
}

async function showDetail(offset) {
  const r = await api().row(offset);
  if (!r || S.selOffset !== offset) return;
  const c = r.c;
  const f = S.file;
  S.detailRow = r;
  if ($("detail").hidden) { $("detail").hidden = false; updateTableWidth(); }
  const alt = c[4] || ".";
  $("d-title").innerHTML = `${esc(c[0])}:${fmtPos(c[1])} <span>${esc(trunc(c[3], 30))}</span><span class="arrow">→</span><span>${esc(trunc(alt, 30))}</span>`;

  // Summary: the same fields as the table's main columns, one per line.
  const row = (k, v, extra = "") => `<dt>${k}</dt><dd>${v}${extra}</dd>`;
  const filt = c[6];
  const fdesc = f.filter[filt] && f.filter[filt].Description;
  let h = `<div class="summary">`;
  h += `<dl>${row("Chrom", esc(c[0]))}${row("Pos", esc(c[1]))}${row("ID", esc(c[2]))}</dl>`;
  h += `<dl>${row("Ref", esc(c[3]))}${row("Alt", esc(alt))}</dl>`;
  h += `<dl>${row("Qual", esc(c[5]))}${row("Filter", esc(filt), fdesc && filt !== "PASS" ? `<span class="desc">${esc(fdesc)}</span>` : "")}</dl>`;
  const hasGT = (c[8] || "").startsWith("GT");
  const gtOf = (col) => (hasGT && c[col] ? c[col].split(":")[0] : "");
  const gtHTML = (gt) => (gt ? `<span class="gt ${gtClass(gt)}">${esc(gt)}</span>` : "");
  // GT / Ref reads / Alt reads: for the only sample, or the one picked.
  if (!multi() || S.focus != null) {
    const k = multi() ? S.focus : 0;
    const rc = readCounts(c, 9 + k);
    const src = rc.from ? `<span class="desc">from ${rc.from}</span>` : "";
    h += `<dl>${multi() ? row("Sample", `<span title="${esc(f.samples[k])}">${esc(S.short[k])}</span>`) : ""}` +
         `${row("GT", gtHTML(gtOf(9 + k)))}` +
         `${row("Ref reads", esc(rc.ref))}${row("Alt reads", esc(rc.alt), rc.ref || rc.alt ? src : "")}</dl>`;
  }
  h += `</div>`;
  // Every sample's genotype at a glance, right after the summary.
  if (multi()) {
    const n = f.samples.length;
    const limit = Math.min(n, 1000);
    h += `<h3>Samples (${n.toLocaleString()})</h3><div class="samples-wrap"><table class="samples">` +
         `<tr><th>Sample</th><th>GT</th><th>Ref reads</th><th>Alt reads</th></tr>`;
    for (let s = 0; s < limit; s++) {
      const rc = readCounts(c, 9 + s);
      h += `<tr class="${s === S.focus ? "focus" : ""}"><td title="${esc(f.samples[s])}">${esc(S.short[s])}</td>` +
           `<td>${gtHTML(gtOf(9 + s))}</td><td>${esc(rc.ref)}</td><td>${esc(rc.alt)}</td></tr>`;
    }
    h += `</table></div>`;
    if (n > limit) h += `<p class="muted">Showing the first ${limit.toLocaleString()} samples.</p>`;
  }
  // INFO
  if (c[7] && c[7] !== ".") {
    h += `<h3>Info</h3><table class="kv">`;
    for (const kv of c[7].split(";")) {
      const i = kv.indexOf("=");
      const k = i < 0 ? kv : kv.slice(0, i);
      const v = i < 0 ? "✓" : kv.slice(i + 1).replace(/,/g, ", ");
      const d = f.info[k] && f.info[k].Description;
      h += `<tr><td class="k">${esc(k)}</td><td class="v">${esc(v)}${d ? `<span class="desc">${esc(d)}</span>` : ""}</td></tr>`;
    }
    h += `</table>`;
  }
  // Samples
  if (c.length > 9) {
    const keys = (c[8] || "").split(":");
    const names = f.samples;
    h += `<h3>${names.length === 1 ? "Sample" : "Sample fields"}</h3>`;
    if (names.length === 1) {
      const vals = c[9].split(":");
      h += `<table class="kv">`;
      keys.forEach((k, i) => {
        const d = f.format[k] && f.format[k].Description;
        let v = esc(vals[i] ?? "");
        if (k === "GT") v = `<span class="gt ${gtClass(vals[i] || ".")}">${v}</span>`;
        h += `<tr><td class="k">${esc(k)}</td><td class="v">${v}${d ? `<span class="desc">${esc(d)}</span>` : ""}</td></tr>`;
      });
      h += `</table>`;
    } else {
      h += `<div class="samples-wrap"><table class="samples"><tr><th>Sample</th>` +
        keys.map((k) => `<th title="${esc((f.format[k] && f.format[k].Description) || "")}">${esc(k)}</th>`).join("") + `</tr>`;
      const limit = Math.min(names.length, 5000);
      for (let s = 0; s < limit; s++) {
        const vals = (c[9 + s] || "").split(":");
        h += `<tr class="${s === S.focus ? "focus" : ""}"><td title="${esc(names[s])}">${esc(S.short[s])}</td>` + keys.map((k, i) => {
          const v = esc(vals[i] ?? "");
          return k === "GT" ? `<td><span class="gt ${gtClass(vals[i] || ".")}">${v}</span></td>` : `<td>${v}</td>`;
        }).join("") + `</tr>`;
      }
      h += `</table></div>`;
      if (names.length > limit) h += `<p class="muted">Showing the first ${limit.toLocaleString()} samples.</p>`;
    }
  }
  $("d-body").innerHTML = h;
}

const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

function closeDetail() {
  $("detail").hidden = true;
  updateTableWidth();
  S.selOffset = null;
  body().querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel"));
}

function moveSelection(delta) {
  const rows = [...dataRows()];
  if (!rows.length) return;
  let i = rows.findIndex((r) => +r.dataset.o === S.selOffset);
  i = i < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, i + delta));
  const tr = rows[i];
  selectRow(tr);
  const w = $("table-wrap");
  const headH = $("thead").offsetHeight;
  if (tr.offsetTop < w.scrollTop + headH) w.scrollTop = tr.offsetTop - headH;
  else if (tr.offsetTop + tr.offsetHeight > w.scrollTop + w.clientHeight) w.scrollTop = tr.offsetTop + tr.offsetHeight - w.clientHeight;
}

/* ----------------------------------------------------------------- header */

function showHeader(on) {
  $("header-view").hidden = !on;
  $("table-wrap").hidden = on;
  $("header-btn").classList.toggle("on", on);
  if (on) {
    $("detail").hidden = true;
    $("hv-filter").focus();
  } else if (S.selOffset != null) {
    $("detail").hidden = false;
  }
}

function hlMeta(line) {
  const m = line.match(/^(##[^=]+=)(<.*>)$/);
  if (m) {
    let body = esc(m[2])
      .replace(/(ID=)([^,&]+)/, '$1<span class="id">$2</span>')
      .replace(/(Description=)(&quot;.*?&quot;)/, '$1<span class="q">$2</span>');
    return `<span class="k">${esc(m[1])}</span>${body}`;
  }
  const k = line.match(/^(##[^=]+=)(.*)$/);
  if (k) return `<span class="k">${esc(k[1])}</span>${esc(k[2])}`;
  return esc(line);
}

function renderHeaderText(filter) {
  const f = S.file;
  const q = filter.trim().toLowerCase();
  const lines = f.meta.filter((l) => !q || l.toLowerCase().includes(q));
  const colLine = "#" + f.columns.join("\t");
  let html = lines.map(hlMeta).join("\n");
  if (!q || colLine.toLowerCase().includes(q)) html += (html ? "\n" : "") + `<span class="cols">${esc(colLine)}</span>`;
  $("hv-text").innerHTML = html;
  $("hv-count").textContent = q ? `${lines.length} of ${f.meta.length} lines` : `${f.meta.length} lines`;
}

/* ---------------------------------------------------------------- events */

function bind() {
  $("open-big").onclick = chooseFile;
  $("open-btn").onclick = chooseFile;
  $("i-name").onclick = () => api().reveal();
  $("header-btn").onclick = () => showHeader($("header-view").hidden);
  $("cols-btn").onclick = () => {
    S.showAll = !S.showAll;
    if (!$("header-view").hidden) showHeader(false);
    setupColumns(S.file);
    updateHideRefToggle();
    reloadHere();
  };
  $("sample-pick").onchange = (e) => {
    S.focus = e.target.value === "" ? null : +e.target.value;
    setupColumns(S.file);
    updateHideRefToggle();
    reloadHere();
    if (S.selOffset != null) showDetail(S.selOffset);
  };
  $("hv-filter").oninput = (e) => renderHeaderText(e.target.value);
  $("d-close").onclick = closeDetail;
  $("d-copy").onclick = async () => {
    if (!S.detailRow) return;
    await copyText(S.detailRow.c.join("\t"));
    setMsg("Record copied");
  };
  $("table-wrap").addEventListener("scroll", onScroll, { passive: true });
  body().addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-o]");
    if (!tr) return;
    // Clicking the selected row again closes its details.
    if (+tr.dataset.o === S.selOffset && !$("detail").hidden) closeDetail();
    else selectRow(tr);
  });
  $("goto-form").onsubmit = (e) => { e.preventDefault(); goPos(); };
  $("rsid-form").onsubmit = (e) => { e.preventDefault(); findId(); };
  $("chrom").onchange = () => goPos();
  for (const id of ["pos", "rsid"]) {
    $(id).addEventListener("input", () => $(id).classList.remove("bad"));
    $(id).addEventListener("keydown", (e) => {
      if (e.key === "Escape") { $(id).blur(); $("table-wrap").focus(); }
    });
  }
  $("rsid").addEventListener("input", () => {
    $("rsid-msg").hidden = true;
    if (S.search && S.search.input !== $("rsid").value.trim()) S.search = null;
  });
  $("hide-ref").onchange = (e) => {
    S.hideRef = e.target.checked;
    reloadHere();
  };
  document.querySelector("#welcome .hint kbd").textContent = MOD + "O";

  document.addEventListener("keydown", (e) => {
    const mod = IS_MAC ? e.metaKey : e.ctrlKey;
    const inInput = e.target.matches("input, select, textarea");
    if (mod && e.key.toLowerCase() === "o") { e.preventDefault(); chooseFile(); return; }
    if (!S.file || $("viewer").hidden) return;
    if (mod && e.key.toLowerCase() === "f") {
      e.preventDefault();
      const el = !$("header-view").hidden ? $("hv-filter") : $("rsid");
      el.focus(); el.select();
      return;
    }
    if (mod && e.key.toLowerCase() === "l") { e.preventDefault(); $("pos").focus(); $("pos").select(); return; }
    if (mod && e.key.toLowerCase() === "g") { e.preventDefault(); findNext(); return; }
    if (inInput) {
      if (e.key === "Escape" && e.target.id === "hv-filter") showHeader(false);
      return;
    }
    if (e.key === "/") { e.preventDefault(); $("pos").focus(); $("pos").select(); }
    else if (e.key === "Escape") {
      if (!$("header-view").hidden) showHeader(false);
      else if (!$("detail").hidden) closeDetail();
    } else if (e.key === "h" || e.key === "H") showHeader($("header-view").hidden);
    else if (e.key === "ArrowDown" && !$("detail").hidden) { e.preventDefault(); moveSelection(1); }
    else if (e.key === "ArrowUp" && !$("detail").hidden) { e.preventDefault(); moveSelection(-1); }
    else if (mod && e.key === "ArrowUp") { e.preventDefault(); goStart(); }
  });

  // Drag & drop: the Python side receives the real path; here we only show a hint.
  let depth = 0;
  document.addEventListener("dragenter", (e) => { e.preventDefault(); depth++; $("drop-hint").hidden = false; });
  document.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; $("drop-hint").hidden = true; } });
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => { e.preventDefault(); depth = 0; $("drop-hint").hidden = true; });

  window.addEventListener("resize", () => { if (S.file) { updateTableWidth(); updateRange(); maybeLoadMore(); } });
}

window.app = { openPath };

window.addEventListener("pywebviewready", () => {
  bind();
  readyResolve();
  if (!S.file) showWelcome();
});
