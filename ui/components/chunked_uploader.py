"""
Kontrol unggah dataset BERTAHAP di halaman Tambah Pipeline & Dataset.

Pasangan sisi peramban dari ``orchestrator/chunked_upload.py``. Peramban
memotong berkas per ``CHUNK_BYTES``, mengirim tiap potongan ke rute
``/ids-upload`` di server yang sama, mencoba ulang potongan yang gagal dengan
jeda yang makin panjang, dan menyelaraskan diri dari jawaban server bila
sebuah jawaban hilang di jalan. Memilih berkas yang sama lagi — setelah
koneksi putus total, halaman dimuat ulang, atau server dijalankan ulang —
melanjutkan dari byte terakhir yang sudah diterima, bukan dari nol.

Tampilannya meniru uploader bawaan Streamlit: kotak unggah dengan tombol
"Upload" dan keterangan batas, lalu berkas sebagai BARIS di bawahnya dengan
bilah progres dan tombol batal.

Halaman memantau keadaan unggahan dari sisi server (bukan dari peramban):
begitu berkasnya lengkap, :func:`render_chunked_uploader` mengembalikan
:class:`ChunkedFile` yang dipakai alur pemeriksaan dan penyimpanan yang sudah
ada, persis seperti objek dari ``st.file_uploader`` dahulu.
"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from orchestrator import chunked_upload as cu

_TOKEN_KEY = "_contrib_chunk_token"


class ChunkedFile:
    """Berkas hasil unggahan bertahap, berperilaku seperti objek unggahan.

    ``name``/``size``/``read``/``seek``/``tell`` cukup untuk alur pemeriksaan
    (``copy_stream``) dan ``upload_size``. ``local_path`` membuat
    ``save_dataset_upload`` MEMINDAHKAN berkasnya alih-alih menyalin 20 GB.
    """

    def __init__(self, path: str, name: str, size: int, token: str):
        self.local_path = Path(path)
        self.name = name
        self.size = int(size)
        self.token = token
        self._fh = None

    def _file(self):
        if self._fh is None or self._fh.closed:
            self._fh = open(self.local_path, "rb")
        return self._fh

    def read(self, n: int = -1) -> bytes:
        return self._file().read(n)

    def seek(self, pos: int, whence: int = 0) -> int:
        return self._file().seek(pos, whence)

    def tell(self) -> int:
        return self._file().tell()

    def close(self) -> None:
        if self._fh is not None and not self._fh.closed:
            self._fh.close()


def _token(username: str) -> str:
    token = st.session_state.get(_TOKEN_KEY)
    if not cu.token_valid(token):
        token = cu.mint_token(username)
        st.session_state[_TOKEN_KEY] = token
    return token


def finish(token: str | None, *, delete_part: bool = False) -> None:
    """Tutup unggahan ini dan siapkan kontrol untuk berkas berikutnya."""
    cu.discard(token, delete_part=delete_part)
    st.session_state.pop(_TOKEN_KEY, None)


@st.fragment(run_every="2s")
def _watch(token: str, was_done: bool) -> None:
    """Muat ulang halaman saat berkas SELESAI diterima, atau DIBATALKAN.

    Hanya fragmen kecil ini yang berjalan tiap dua detik; kontrol unggah
    (iframe) dan isi halaman lain tidak tersentuh, jadi unggahan yang sedang
    berjalan tidak terganggu.
    """
    state = cu.upload_state(token)
    if bool(state and state.get("done")) != was_done:
        st.rerun(scope="app")


def _labels() -> dict:
    from ui.i18n import t
    keys = ("btn", "hint", "drop", "connecting", "resuming", "keep_open",
            "left", "retry", "done", "received", "paused", "too_big",
            "expired", "stopped", "cancel")
    # Placeholder {..} dibiarkan utuh untuk diisi JavaScript.
    return {k: t(f"up.{k}") for k in keys}


def render_chunked_uploader(username: str) -> ChunkedFile | None:
    """Gambar kontrol unggah; kembalikan berkas bila sudah lengkap diterima.

    Kontrolnya SELALU digambar, juga setelah berkas lengkap: baris berkas
    dengan tombol batalnya tetap di bawah kotak unggah.

    Isi HTML-nya sengaja hanya memuat hal yang TETAP selama unggahan berjalan
    (token, batas, teks, dan berkas yang sudah selesai). Bila isinya berubah,
    Streamlit memuat ulang iframe-nya dan unggahan yang sedang berjalan ikut
    terputus; kemajuan karena itu dibaca kontrolnya sendiri dari server.
    """
    token = _token(username)
    state = cu.upload_state(token)
    done = bool(state and state.get("done"))

    import streamlit.components.v1 as components

    config = {
        "token": token,
        "prefix": cu.ROUTE_PREFIX,
        "max": cu.MAX_DATASET_UPLOAD_BYTES,
        "chunk": cu.CHUNK_BYTES,
        "accept": ",".join(cu.DATASET_EXTENSIONS),
        "labels": _labels(),
        "done": ({"name": state["filename"], "size": state["size"]}
                 if done else None),
    }
    # `</` di dalam JSON ditulis ulang supaya teks apa pun tidak dapat
    # menutup tag <script> sebelum waktunya.
    payload = json.dumps(config).replace("</", "<\\/")
    components.html(_WIDGET_HTML.replace("__CONFIG__", payload), height=86)
    _watch(token, done)
    if done:
        return ChunkedFile(state["path"], state["filename"], state["size"], token)
    return None


# Warna dibaca dari halaman induk (iframe ini satu origin dengannya), jadi
# kontrolnya mengikuti tema terang maupun gelap yang sedang dipakai aplikasi.
_WIDGET_HTML = r"""
<style>
  :root { --bg:#ffffff; --panel:#f0f2f6; --fg:#31333f; --muted:rgba(49,51,63,.6);
          --line:rgba(49,51,63,.2); --accent:#ff4b4b; --err:#e5484d; }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: transparent; color: var(--fg);
               font-family: var(--font, "Source Sans Pro", "Source Sans 3", system-ui, sans-serif); }
  .zone { background: var(--panel); border-radius: 8px; padding: 16px;
          display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
          border: 1px dashed transparent; transition: border-color .15s; }
  .zone.over { border-color: var(--accent); }
  .zone.off { opacity: .55; }
  button.up { display: inline-flex; align-items: center; gap: 8px; cursor: pointer;
              background: var(--bg); color: var(--fg); border: 1px solid var(--line);
              border-radius: 8px; padding: 7px 14px; font: inherit; font-size: 15px; }
  button.up:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  button.up:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  button.up:disabled { cursor: not-allowed; }
  .hint { font-size: 14px; color: var(--muted); }
  input[type=file] { display: none; }
  .file { display: grid; grid-template-columns: auto 1fr auto; gap: 4px 12px;
          align-items: center; padding: 12px 4px 4px; }
  .icon { width: 36px; height: 36px; border-radius: 6px; display: grid; place-items: center;
          background: var(--panel); color: var(--muted); }
  .file.err .icon { color: var(--err); }
  .name { font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { font-size: 12.5px; color: var(--muted); overflow-wrap: anywhere; }
  .file.err .meta { color: var(--err); }
  .x { background: none; border: none; cursor: pointer; color: var(--muted);
       width: 32px; height: 32px; border-radius: 6px; display: grid; place-items: center; }
  .x:hover { color: var(--err); background: var(--panel); }
  .x:focus-visible { outline: 2px solid var(--accent); }
  .bar { grid-column: 1 / -1; height: 6px; border-radius: 3px; background: var(--panel);
         overflow: hidden; margin-top: 6px; }
  .bar > div { height: 100%; width: 0; background: var(--accent); transition: width .3s; }
  @media (prefers-reduced-motion: reduce) { .bar > div, .zone { transition: none; } }
</style>
<div class="zone" id="zone">
  <button class="up" id="pick" type="button">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M12 16V4M6 10l6-6 6 6M4 20h16"/></svg>
    <span id="pickLabel"></span>
  </button>
  <span class="hint" id="hint"></span>
  <input id="f" type="file">
</div>
<div class="file" id="row" hidden>
  <div class="icon" aria-hidden="true">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/></svg>
  </div>
  <div style="min-width:0"><div class="name" id="name"></div><div class="meta" id="meta"></div></div>
  <button class="x" id="cancel" type="button">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
         stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
  </button>
  <div class="bar" id="bar"><div id="fill"></div></div>
</div>
<script>
const C = __CONFIG__;
const L = C.labels;
const $ = (id) => document.getElementById(id);
const fmt = (s, v) => s.replace(/\{(\w+)\}/g, (m, k) => (k in v ? v[k] : m));
const size = (n) => n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(2) + " GB"
                  : n >= 1024 ** 2 ? (n / 1024 ** 2).toFixed(1) + " MB"
                  : Math.max(1, Math.round(n / 1024)) + " KB";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const LIMIT = Math.round(C.max / 1024 ** 3) + "GB";

// ── Tema: warna dan huruf diambil dari halaman induk (satu origin). ──
function rgb(c) { const m = (c || "").match(/[\d.]+/g); return m ? m.slice(0, 3).map(Number) : null; }
function applyTheme() {
  try {
    const doc = window.parent.document;
    const app = doc.querySelector(".stApp") || doc.body;
    const cs = window.parent.getComputedStyle(app);
    const bg = rgb(cs.backgroundColor), fg = rgb(cs.color);
    if (!bg || !fg) return;
    const dark = (0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]) < 128;
    const side = doc.querySelector('[data-testid="stSidebar"]');
    const panel = side ? rgb(window.parent.getComputedStyle(side).backgroundColor) : null;
    const r = document.documentElement.style;
    r.setProperty("--bg", `rgb(${bg})`);
    r.setProperty("--fg", `rgb(${fg})`);
    r.setProperty("--muted", `rgba(${fg},.6)`);
    r.setProperty("--line", `rgba(${fg},.2)`);
    r.setProperty("--panel", panel && panel.join() !== bg.join() ? `rgb(${panel})`
                               : (dark ? "#262730" : "#f0f2f6"));
    r.setProperty("--font", cs.fontFamily);
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
  } catch (_) { /* tetap memakai warna bawaan */ }
}
applyTheme();
setInterval(applyTheme, 1500);   // tema dapat diganti saat halaman terbuka

// Tinggi iframe mengikuti isinya (satu origin, jadi bingkainya dapat diatur).
function fit() {
  try { window.frameElement.style.height = Math.ceil(document.body.scrollHeight) + 2 + "px"; } catch (_) {}
}
if (window.ResizeObserver) new ResizeObserver(fit).observe(document.body);

$("pickLabel").textContent = L.btn;
$("hint").textContent = fmt(L.hint, { limit: LIMIT,
  formats: C.accept.split(",").map((s) => s.slice(1).toUpperCase()).join(", ") });
$("f").accept = C.accept;
$("cancel").title = L.cancel;
$("cancel").setAttribute("aria-label", L.cancel);

function row(name, meta, opts = {}) {
  $("row").hidden = false;
  $("row").className = "file" + (opts.error ? " err" : "");
  $("name").textContent = name;
  $("name").title = name;
  $("meta").textContent = meta;
  if (opts.pct !== undefined) $("fill").style.width = opts.pct.toFixed(1) + "%";
  $("bar").hidden = opts.pct === undefined;
  fit();
}
function lockZone(on) {
  $("pick").disabled = on;
  $("zone").classList.toggle("off", on);
}

async function api(path, opts) {
  const res = await fetch(C.prefix + path, opts);
  let body = {};
  try { body = await res.json(); } catch (_) {}
  return { status: res.status, body };
}

// PUT satu potongan lewat XHR, bukan fetch: hanya XHR yang melaporkan
// kemajuan di DALAM satu potongan, jadi bilah bergerak sejak byte pertama.
let xhr = null;
function putChunk(url, blob, onProgress) {
  return new Promise((resolve, reject) => {
    const x = (xhr = new XMLHttpRequest());
    x.open("PUT", url);
    x.timeout = 180000;
    x.upload.onprogress = (ev) => { if (ev.lengthComputable) onProgress(ev.loaded); };
    x.onload = () => { let body = {}; try { body = JSON.parse(x.responseText); } catch (_) {}
                       resolve({ status: x.status, body }); };
    x.onerror = () => reject(new Error("network"));
    x.ontimeout = () => reject(new Error("timeout"));
    x.onabort = () => reject(new Error("cancelled"));
    x.send(blob);
  });
}

let running = false, cancelled = false;

async function upload(file) {
  if (running) return;
  if (file.size > C.max) {
    row(file.name, size(file.size) + " · " + fmt(L.too_big, { limit: LIMIT }), { error: true });
    return;
  }
  running = true; cancelled = false;
  lockZone(true);
  row(file.name, size(file.size) + " · " + L.connecting, { pct: 0 });
  let t0 = Date.now(), base = 0;
  const show = (sent) => {
    const pct = sent / file.size * 100;
    const secs = (Date.now() - t0) / 1000;
    const rate = secs > 1 ? (sent - base) / secs : 0;
    const parts = [size(sent) + " / " + size(file.size), pct.toFixed(1) + "%"];
    if (rate > 0) {
      parts.push((rate / 1024 ** 2).toFixed(1) + " MB/s");
      parts.push(fmt(L.left, { min: Math.max(1, Math.round((file.size - sent) / rate / 60)) }));
    }
    row(file.name, parts.join(" · "), { pct });
  };
  try {
    let r;
    for (let i = 0; ; i++) {
      if (cancelled) return;
      try { r = await api("/begin", { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ token: C.token, filename: file.name, size: file.size }) });
            break; }
      catch (e) { await sleep(Math.min(30000, 1000 * 2 ** i)); }
    }
    if (r.status !== 200) {
      row(file.name, size(file.size) + " · " + (r.body.error || ("HTTP " + r.status)), { error: true });
      lockZone(false);
      return;
    }
    let received = r.body.received, fails = 0;
    base = received; t0 = Date.now();
    if (received > 0) row(file.name, fmt(L.resuming, { done: size(received) }), { pct: received / file.size * 100 });
    while (received < file.size) {
      if (cancelled) return;
      const at = received, end = Math.min(at + C.chunk, file.size);
      try {
        const res = await putChunk(C.prefix + "/" + C.token + "/chunk?offset=" + at,
                                   file.slice(at, end), (loaded) => show(at + loaded));
        if (res.status === 200 || (res.status === 409 && typeof res.body.received === "number")) {
          received = res.body.received;
          if (res.status === 200) fails = 0;
        } else if (res.status === 403) {
          row(file.name, L.expired, { error: true, pct: received / file.size * 100 });
          lockZone(false);
          return;
        } else if (res.status >= 400 && res.status < 500) {
          row(file.name, res.body.error || ("HTTP " + res.status), { error: true });
          lockZone(false);
          return;
        } else { throw new Error("HTTP " + res.status); }
      } catch (e) {
        if (cancelled) return;
        // Jaringan putus / server sibuk: tunggu, tanyakan posisi terakhir, ulangi.
        fails++;
        const wait = Math.min(30000, 1000 * 2 ** Math.min(fails, 5));
        row(file.name, fmt(L.retry, { sec: Math.round(wait / 1000), safe: size(received) }),
            { pct: received / file.size * 100 });
        await sleep(wait);
        try { const s = await api("/" + C.token, {}); if (s.status === 200) received = s.body.received; } catch (_) {}
        continue;
      }
      show(received);
    }
    row(file.name, size(file.size) + " · " + L.done, { pct: 100 });
    // Halaman memuat ulang sendiri begitu server melihat berkasnya lengkap.
  } catch (e) {
    if (!cancelled) {
      row(file.name, fmt(L.stopped, { err: e.message }), { error: true });
      lockZone(false);
    }
  } finally { running = false; }
}

$("cancel").addEventListener("click", async () => {
  cancelled = true;
  if (xhr) try { xhr.abort(); } catch (_) {}
  try { await api("/" + C.token, { method: "DELETE" }); } catch (_) {}
  $("row").hidden = true;
  $("f").value = "";
  lockZone(false);
  fit();
});

$("pick").addEventListener("click", () => $("f").click());
$("f").addEventListener("change", () => { const f = $("f").files[0]; if (f) upload(f); });
// Seret-lepas, seperti uploader bawaan.
const zone = $("zone");
["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => {
  e.preventDefault(); if (!$("pick").disabled) zone.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => zone.addEventListener(t, (e) => {
  e.preventDefault(); zone.classList.remove("over"); }));
zone.addEventListener("drop", (e) => {
  const f = e.dataTransfer && e.dataTransfer.files[0];
  if (f && !$("pick").disabled) upload(f);
});
window.addEventListener("error", (e) => row($("name").textContent || "-", "JS: " + e.message, { error: true }));

// Keadaan awal: berkas yang sudah lengkap tetap tampil sebagai baris.
if (C.done) {
  row(C.done.name, size(C.done.size) + " · " + L.received, { pct: 100 });
  lockZone(true);
}
fit();
</script>
"""
