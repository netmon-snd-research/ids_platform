"""
Kontrol unggah dataset BERTAHAP di halaman Tambah Pipeline & Dataset.

Pasangan sisi peramban dari ``orchestrator/chunked_upload.py``. Peramban
memotong berkas per ``CHUNK_BYTES``, mengirim tiap potongan ke rute
``/ids-upload`` di server yang sama, mencoba ulang potongan yang gagal dengan
jeda yang makin panjang, dan menyelaraskan diri dari jawaban server bila
sebuah jawaban hilang di jalan. Memilih berkas yang sama lagi — setelah
koneksi putus total, halaman dimuat ulang, atau server dijalankan ulang —
melanjutkan dari byte terakhir yang sudah diterima, bukan dari nol.

DUA bagian JavaScript:

* **Pengelola** (``_MANAGER_JS``) hidup di jendela UTAMA aplikasi, bukan di
  iframe kontrol. Ia yang benar-benar mengirim potongan. Iframe kontrol dibuang
  Streamlit begitu pengguna membuka halaman lain; bila pengirimnya ada di
  sana, unggahan ikut mati. Di jendela utama, unggahan tetap berjalan selama
  pengguna berpindah halaman di dalam aplikasi, dan indikator kecil di pojok
  halaman menunjukkan kemajuannya. Menutup tab atau memuat ulang peramban
  tetap menghentikannya — peramban meminta konfirmasi dulu, dan memilih
  berkas yang sama melanjutkannya.
* **Tampilan** (``_WIDGET_HTML``) di dalam iframe: kotak unggah bergaya
  uploader bawaan Streamlit, lalu berkas sebagai BARIS dengan bilah progres,
  tombol jeda/lanjutkan, dan tombol batal. Ia hanya menempel pada pengelola
  dan menggambar keadaannya, jadi kembali ke halaman ini menampilkan unggahan
  yang sedang berjalan.

Halaman memantau keadaan unggahan dari sisi server: begitu berkasnya lengkap,
:func:`render_chunked_uploader` mengembalikan :class:`ChunkedFile` yang
dipakai alur pemeriksaan dan penyimpanan yang sudah ada.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import streamlit as st

from orchestrator import chunked_upload as cu

#: Kunci sesi token unggah. SENGAJA tidak berawalan `_contrib`: awalan itu
#: dibuang saat pengguna berpindah halaman (page_flags.VIEW_STATE_PREFIXES),
#: dan token baru membuat halaman ini tidak lagi mengenali unggahan yang masih
#: berjalan di jendela utama ketika pengguna kembali.
_TOKEN_KEY = "_ids_upload_token"


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

    Hanya fragmen kecil ini yang berjalan tiap dua detik; kontrol unggah dan
    isi halaman lain tidak tersentuh.
    """
    state = cu.upload_state(token)
    if bool(state and state.get("done")) != was_done:
        st.rerun(scope="app")


def _labels() -> dict:
    from ui.i18n import t
    keys = ("btn", "hint", "connecting", "resuming", "left", "retry", "done",
            "received", "too_big", "expired", "stopped", "cancel", "pause",
            "resume", "paused_at", "pill", "pill_paused", "pill_retry")
    # Placeholder {..} dibiarkan utuh untuk diisi JavaScript.
    return {k: t(f"up.{k}") for k in keys}


def render_chunked_uploader(username: str) -> ChunkedFile | None:
    """Gambar kontrol unggah; kembalikan berkas bila sudah lengkap diterima.

    Kontrolnya SELALU digambar, juga setelah berkas lengkap: baris berkas
    dengan tombol batalnya tetap di bawah kotak unggah.

    Isi HTML-nya sengaja hanya memuat hal yang TETAP selama unggahan berjalan
    (token, batas, teks, kode pengelola, dan berkas yang sudah selesai). Bila
    isinya berubah, Streamlit memuat ulang iframe-nya; kemajuan karena itu
    dibaca tampilan dari pengelola, bukan ditulis ke HTML.
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
        "manager": _MANAGER_JS,
        "version": _MANAGER_VERSION,
        "done": ({"name": state["filename"], "size": state["size"]}
                 if done else None),
    }
    # `</` di dalam JSON ditulis ulang supaya teks apa pun tidak dapat
    # menutup tag <script> sebelum waktunya.
    payload = json.dumps(config).replace("</", "<\\/")
    components.html(_WIDGET_HTML.replace("__CONFIG__", payload), height=96)
    _watch(token, done)
    if done:
        return ChunkedFile(state["path"], state["filename"], state["size"], token)
    return None


# ── Pengelola unggah: hidup di jendela UTAMA aplikasi ─────────────────────
#
# Disisipkan tampilan sebagai <script> ke dokumen induk (satu origin), jadi
# fungsi, fetch, dan XHR-nya milik jendela utama dan tidak ikut mati saat
# iframe tampilan dibuang. Satu pekerjaan per token sesi.
_MANAGER_JS = r"""
(function () {
  const VERSION = "__VERSION__";
  const prev = window.__idsUploads;
  if (prev && prev.version === VERSION) return;
  if (prev && prev.busy && prev.busy()) return;   // jangan ganti pengelola yang sedang bekerja

  const ACTIVE = ["connecting", "up", "warn", "hold"];
  const jobs = {};
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const busy = () => Object.values(jobs).some((j) => ACTIVE.includes(j.state));

  async function api(j, path, opts) {
    const res = await fetch(j.prefix + path, opts);
    let body = {};
    try { body = await res.json(); } catch (_) {}
    return { status: res.status, body };
  }
  function snap(j) {
    return { name: j.name, size: j.size, received: j.received, sent: j.received + j.inflight,
             state: j.state, rate: j.rate, wait: j.wait, error: j.error, resumed: j.resumed };
  }
  function emit(j) {
    j.views = j.views.filter((v) => v.frame && v.frame.isConnected);
    for (const v of j.views) { try { v.fn(snap(j)); } catch (_) {} }
    pill();
  }
  function measure(j) {
    const secs = (Date.now() - j.t0) / 1000;
    j.rate = secs > 1 ? (j.received + j.inflight - j.base) / secs : 0;
  }
  function putChunk(j, url, blob) {
    return new Promise((resolve, reject) => {
      const x = (j.xhr = new XMLHttpRequest());
      x.open("PUT", url);
      x.timeout = 180000;
      x.upload.onprogress = (ev) => { if (ev.lengthComputable) { j.inflight = ev.loaded; measure(j); emit(j); } };
      x.onload = () => { let body = {}; try { body = JSON.parse(x.responseText); } catch (_) {}
                         resolve({ status: x.status, body }); };
      x.onerror = () => reject(new Error("network"));
      x.ontimeout = () => reject(new Error("timeout"));
      x.onabort = () => reject(new Error("aborted"));
      x.send(blob);
    });
  }
  async function sync(j) {
    try { const s = await api(j, "/" + j.token, {}); if (s.status === 200) j.received = s.body.received; } catch (_) {}
  }

  async function run(j) {
    const f = j.file;
    try {
      let r;
      for (let i = 0; ; i++) {
        if (j.cancelled) return;
        try { r = await api(j, "/begin", { method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ token: j.token, filename: f.name, size: f.size }) });
              break; }
        catch (_) { await sleep(Math.min(30000, 1000 * 2 ** i)); }
      }
      if (r.status !== 200) { j.state = "err"; j.error = { text: r.body.error || ("HTTP " + r.status) }; emit(j); return; }
      j.received = j.resumed = j.base = r.body.received;
      j.t0 = Date.now();
      j.state = "up"; emit(j);
      let fails = 0;
      while (j.received < f.size) {
        if (j.cancelled) return;
        if (j.paused) {
          // Jeda: tidak ada yang dikirim sampai dilanjutkan. Potongan yang
          // sedang terkirim sudah dibatalkan; server hanya menyimpan potongan utuh.
          j.state = "hold"; j.inflight = 0; emit(j);
          await new Promise((res) => (j.wake = res));
          if (j.cancelled) return;
          await sync(j);
          j.base = j.received; j.t0 = Date.now(); j.rate = 0; fails = 0;
          j.state = "up"; emit(j);
          continue;
        }
        const at = j.received, end = Math.min(at + j.chunk, f.size);
        try {
          j.inflight = 0;
          const res = await putChunk(j, j.prefix + "/" + j.token + "/chunk?offset=" + at, f.slice(at, end));
          j.inflight = 0;
          if (res.status === 200 || (res.status === 409 && typeof res.body.received === "number")) {
            j.received = res.body.received;
            if (res.status === 200) { fails = 0; j.state = "up"; }
          } else if (res.status === 403) {
            j.state = "err"; j.error = { kind: "expired" }; emit(j); return;
          } else if (res.status >= 400 && res.status < 500) {
            j.state = "err"; j.error = { text: res.body.error || ("HTTP " + res.status) }; emit(j); return;
          } else { throw new Error("HTTP " + res.status); }
        } catch (e) {
          j.inflight = 0;
          if (j.cancelled) return;
          if (j.paused) continue;                       // dibatalkan oleh tombol jeda
          // Jaringan putus / server sibuk: tunggu, tanyakan posisi terakhir, ulangi.
          fails++;
          const wait = Math.min(30000, 1000 * 2 ** Math.min(fails, 5));
          j.state = "warn"; j.wait = Math.round(wait / 1000); emit(j);
          await sleep(wait);
          await sync(j);
          continue;
        }
        measure(j); emit(j);
      }
      j.state = "ok"; j.inflight = 0; emit(j);
    } catch (e) {
      if (!j.cancelled) { j.state = "err"; j.error = { kind: "stopped", text: e.message }; emit(j); }
    } finally { j.xhr = null; }
  }

  // Indikator di pojok halaman: tampil hanya saat unggahan berjalan dan tidak
  // ada tampilan unggah yang terbuka (pengguna sedang di halaman lain).
  let pillEl = null;
  function themeOf() {
    const app = document.querySelector(".stApp") || document.body;
    const cs = getComputedStyle(app);
    const m = (cs.backgroundColor.match(/[\d.]+/g) || [255, 255, 255]).slice(0, 3).map(Number);
    return { dark: (0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2]) < 128, fg: cs.color, bg: cs.backgroundColor, font: cs.fontFamily };
  }
  function pill() {
    const shown = Object.values(jobs).filter((j) => ACTIVE.includes(j.state)
                    && !j.views.some((v) => v.frame && v.frame.isConnected));
    if (!shown.length) { if (pillEl) pillEl.hidden = true; return; }
    const j = shown[0], th = themeOf();
    if (!pillEl) {
      pillEl = document.createElement("div");
      pillEl.setAttribute("role", "status");
      pillEl.innerHTML = '<div class="t"></div><div class="b"><div></div></div>';
      document.body.appendChild(pillEl);
    }
    const tone = j.state === "hold" ? (th.dark ? "148,163,184" : "71,85,105")
               : j.state === "warn" ? (th.dark ? "251,191,36" : "180,83,9") : "255,75,75";
    Object.assign(pillEl.style, { position: "fixed", right: "20px", bottom: "20px", zIndex: 999990,
      maxWidth: "min(360px, calc(100vw - 40px))", padding: "12px 16px 14px", borderRadius: "10px",
      background: th.bg, color: th.fg, fontFamily: th.font, fontSize: "13px", lineHeight: "1.4",
      border: `1px solid rgba(${tone},.45)`, boxShadow: "0 6px 24px rgba(0,0,0,.18)" });
    pillEl.hidden = false;
    const pct = (j.size ? (j.received + j.inflight) / j.size * 100 : 0).toFixed(1) + "%";
    const key = j.state === "hold" ? "pill_paused" : j.state === "warn" ? "pill_retry" : "pill";
    const t = pillEl.querySelector(".t");
    t.textContent = j.labels[key].replace("{name}", j.name).replace("{pct}", pct);
    Object.assign(t.style, { whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", marginBottom: "8px" });
    const b = pillEl.querySelector(".b");
    Object.assign(b.style, { height: "4px", borderRadius: "2px", background: `rgba(${tone},.18)`, overflow: "hidden" });
    Object.assign(b.firstChild.style, { height: "100%", width: pct, background: `rgb(${tone})` });
  }
  setInterval(pill, 1500);   // menangkap iframe yang dibuang tanpa kejadian apa pun

  // Menutup tab / memuat ulang selama unggahan berjalan: minta konfirmasi.
  window.addEventListener("beforeunload", (e) => { if (busy()) { e.preventDefault(); e.returnValue = ""; } });

  window.__idsUploads = {
    version: VERSION, busy,
    get(token) { const j = jobs[token]; return j ? snap(j) : null; },
    start(token, opts, file) {
      const old = jobs[token];
      if (old && ACTIVE.includes(old.state)) return snap(old);
      const j = jobs[token] = { token, file, name: file.name, size: file.size, prefix: opts.prefix,
        chunk: opts.chunk, labels: opts.labels, received: 0, inflight: 0, rate: 0, wait: 0,
        resumed: 0, base: 0, t0: Date.now(), state: "connecting", error: null,
        views: old ? old.views : [], paused: false, cancelled: false, xhr: null, wake: null };
      emit(j);
      run(j);
      return snap(j);
    },
    subscribe(token, frame, fn) {
      const j = jobs[token];
      if (!j) return false;
      j.views = j.views.filter((v) => v.frame !== frame && v.frame && v.frame.isConnected);
      j.views.push({ frame, fn });
      fn(snap(j));
      pill();
      return true;
    },
    pause(token) {
      const j = jobs[token];
      if (!j || !["connecting", "up", "warn"].includes(j.state)) return;
      j.paused = true;
      if (j.xhr) try { j.xhr.abort(); } catch (_) {}
      if (j.state !== "connecting") { j.state = "hold"; j.inflight = 0; emit(j); }
    },
    resume(token) {
      const j = jobs[token];
      if (!j || !j.paused) return;
      j.paused = false;
      if (j.wake) { const w = j.wake; j.wake = null; w(); }
    },
    async cancel(token, prefix) {
      const j = jobs[token];
      if (j) {
        j.cancelled = true; j.state = "gone";
        if (j.xhr) try { j.xhr.abort(); } catch (_) {}
        if (j.wake) { const w = j.wake; j.wake = null; w(); }
        delete jobs[token];
        pill();
      }
      try { await fetch((j ? j.prefix : prefix) + "/" + token, { method: "DELETE" }); } catch (_) {}
    },
  };
})();
"""
_MANAGER_VERSION = hashlib.sha1(_MANAGER_JS.encode()).hexdigest()[:10]
_MANAGER_JS = _MANAGER_JS.replace("__VERSION__", _MANAGER_VERSION)


# ── Tampilan: iframe kontrol unggah ───────────────────────────────────────
#
# Warna dibaca dari halaman induk (satu origin), jadi kontrolnya mengikuti
# tema terang maupun gelap yang sedang dipakai aplikasi.
_WIDGET_HTML = r"""
<style>
  /* Warna BERMAKNA, masing-masing sebagai triplet rgb supaya latar bernada
     tipisnya dapat diturunkan: aksen = sedang mengunggah, ok = diterima utuh,
     warn = koneksi terganggu dan sedang dicoba lagi, err = gagal/ditolak,
     hold = dijeda pengguna (netral-tenang: bukan masalah, hanya berhenti);
     csv/json = jenis berkas. Nilai terang di sini, nilai gelap disetel
     applyTheme() saat halaman induk bertema gelap. */
  :root { --bg:#ffffff; --panel:#f0f2f6; --fg:#31333f; --muted:rgba(49,51,63,.6);
          --line:rgba(49,51,63,.2);
          --acc:255,75,75; --ok:21,128,61; --warn:180,83,9; --err:220,38,38; --hold:71,85,105;
          --csv:13,148,136; --json:37,99,235; }
  * { box-sizing: border-box; }
  /* `hidden` HARUS menang atas `display` milik kelas mana pun: tanpa ini
     baris berkas yang kosong ikut tergambar sebelum berkas dipilih. */
  [hidden] { display: none !important; }
  html, body { margin: 0; background: transparent; color: var(--fg);
               font-family: var(--font, "Source Sans Pro", "Source Sans 3", system-ui, sans-serif); }
  /* Ruang 3px di sekeliling supaya cincin fokus tidak terpotong tepi iframe. */
  body { padding: 3px; }
  .zone { background: var(--panel); border-radius: 10px; padding: 18px 20px;
          display: flex; align-items: center; gap: 12px 18px; flex-wrap: wrap;
          border: 1px dashed transparent; transition: border-color .15s, background .15s; }
  .zone:hover:not(.off), .zone.over { border-color: rgb(var(--acc)); background: rgba(var(--acc),.06); }
  .zone.off { opacity: .55; }
  button.up { display: inline-flex; align-items: center; gap: 8px; cursor: pointer;
              background: var(--bg); color: var(--fg); border: 1px solid var(--line);
              border-radius: 8px; padding: 8px 16px; font: inherit; font-size: 15px; line-height: 1.2; }
  button.up:hover:not(:disabled) { border-color: rgb(var(--acc)); color: rgb(var(--acc)); }
  button.up:focus-visible { outline: 2px solid rgb(var(--acc)); outline-offset: 2px; }
  button.up:disabled { cursor: not-allowed; }
  .hint { font-size: 14px; line-height: 1.4; color: var(--muted); }
  input[type=file] { display: none; }
  /* Baris berkas. Sedang mengunggah = NETRAL (hanya bilahnya beraksen), sebab
     merah juga berarti gagal: baris yang normal tidak boleh terlihat seperti
     error. Latar bernada hanya untuk keadaan yang perlu diperhatikan. */
  .file { --st: var(--acc); display: grid; grid-template-columns: auto 1fr auto;
          column-gap: 16px; align-items: center; margin-top: 14px;
          padding: 16px 14px 16px 16px; border-radius: 10px;
          background: transparent; border: 1px solid var(--line);
          transition: background .3s, border-color .3s; }
  .file.ok, .file.warn, .file.err, .file.hold { background: rgba(var(--st),.08); border-color: rgba(var(--st),.28); }
  .file.ok { --st: var(--ok); }
  .file.warn { --st: var(--warn); }
  .file.err { --st: var(--err); }
  .file.hold { --st: var(--hold); }
  /* Lencana jenis berkas: CSV dan keluarga JSON dibedakan warna dan teksnya. */
  .badge { --t: var(--json); width: 46px; height: 40px; border-radius: 8px; display: grid;
           place-items: center; font-size: 10.5px; font-weight: 700; letter-spacing: .05em;
           color: rgb(var(--t)); background: rgba(var(--t),.14); border: 1px solid rgba(var(--t),.28); }
  .badge.csv { --t: var(--csv); }
  .text { min-width: 0; display: grid; gap: 4px; }
  .name { font-size: 14.5px; font-weight: 600; line-height: 1.35;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { font-size: 13px; line-height: 1.45; color: var(--muted); overflow-wrap: anywhere;
          font-variant-numeric: tabular-nums; }
  .file.ok .meta, .file.warn .meta, .file.err .meta, .file.hold .meta { color: rgb(var(--st)); font-weight: 600; }
  .acts { display: flex; gap: 6px; }
  .x { background: none; border: none; cursor: pointer; color: var(--muted);
       width: 34px; height: 34px; border-radius: 8px; display: grid; place-items: center; }
  .x:hover { color: rgb(var(--err)); background: rgba(var(--err),.12); }
  .x:focus-visible { outline: 2px solid rgb(var(--acc)); }
  /* Jeda/lanjutkan BUKAN tindakan merusak, jadi sorotnya aksen, bukan merah. */
  .x.pz:hover { color: rgb(var(--acc)); background: rgba(var(--acc),.12); }
  .bar { grid-column: 1 / -1; height: 6px; border-radius: 3px; background: rgba(var(--st),.15);
         overflow: hidden; margin-top: 14px; }
  .bar > div { height: 100%; width: 0; background: rgb(var(--st)); transition: width .3s, background .3s; }
  /* Mencoba lagi: bilahnya berdenyut pelan — sedang menunggu, bukan macet. */
  .file.warn .bar > div { animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .45; } }
  @media (prefers-reduced-motion: reduce) {
    .bar > div, .zone, .file { transition: none; }
    .file.warn .bar > div { animation: none; } }
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
  <div class="badge" id="badge" aria-hidden="true"></div>
  <div class="text"><div class="name" id="name"></div><div class="meta" id="meta"></div></div>
  <div class="acts">
    <button class="x pz" id="pause" type="button" hidden>
      <svg id="icoPause" width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>
      <svg id="icoPlay" width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" hidden>
        <path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5z"/></svg>
    </button>
    <button class="x" id="cancel" type="button">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
           stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
    </button>
  </div>
  <div class="bar" id="bar"><div id="fill"></div></div>
</div>
<script>
const C = __CONFIG__;
const L = C.labels;
const P = window.parent;
const $ = (id) => document.getElementById(id);
const fmt = (s, v) => s.replace(/\{(\w+)\}/g, (m, k) => (k in v ? v[k] : m));
const size = (n) => n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(2) + " GB"
                  : n >= 1024 ** 2 ? (n / 1024 ** 2).toFixed(1) + " MB"
                  : Math.max(1, Math.round(n / 1024)) + " KB";
const LIMIT = Math.round(C.max / 1024 ** 3) + "GB";

// ── Tema: warna dan huruf diambil dari halaman induk (satu origin). ──
function rgb(c) { const m = (c || "").match(/[\d.]+/g); return m ? m.slice(0, 3).map(Number) : null; }
function applyTheme() {
  try {
    const doc = P.document;
    const app = doc.querySelector(".stApp") || doc.body;
    const cs = P.getComputedStyle(app);
    const bg = rgb(cs.backgroundColor), fg = rgb(cs.color);
    if (!bg || !fg) return;
    const dark = (0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]) < 128;
    const side = doc.querySelector('[data-testid="stSidebar"]');
    const panel = side ? rgb(P.getComputedStyle(side).backgroundColor) : null;
    const r = document.documentElement.style;
    r.setProperty("--bg", `rgb(${bg})`);
    r.setProperty("--fg", `rgb(${fg})`);
    r.setProperty("--muted", `rgba(${fg},.62)`);
    r.setProperty("--line", `rgba(${fg},.2)`);
    r.setProperty("--panel", panel && panel.join() !== bg.join() ? `rgb(${panel})`
                               : (dark ? "#262730" : "#f0f2f6"));
    r.setProperty("--font", cs.fontFamily);
    // Versi gelap warna bermakna: lebih terang supaya tetap kontras.
    const tones = dark
      ? { "--ok": "74,222,128", "--warn": "251,191,36", "--err": "248,113,113", "--hold": "148,163,184",
          "--csv": "45,212,191", "--json": "96,165,250" }
      : { "--ok": "21,128,61", "--warn": "180,83,9", "--err": "220,38,38", "--hold": "71,85,105",
          "--csv": "13,148,136", "--json": "37,99,235" };
    for (const [k, v] of Object.entries(tones)) r.setProperty(k, v);
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
  } catch (_) { /* tetap memakai warna bawaan */ }
}
applyTheme();
setInterval(applyTheme, 1500);   // tema dapat diganti saat halaman terbuka

// Tinggi iframe mengikuti isinya (satu origin, jadi bingkainya dapat diatur).
// Wadah Streamlit di sekelilingnya ikut diatur: tingginya dipatok dari
// `height=` saat dibuat, dan tanpa ini baris berkas meluber keluar wadah,
// menimpa elemen di bawahnya (garis pemisah dan judul tembus ke kartu).
function fit() {
  try {
    const h = Math.ceil(document.body.scrollHeight) + 2 + "px";
    const frame = window.frameElement;
    frame.style.height = h;
    const box = frame.closest('[data-testid="stElementContainer"]');
    if (box) box.style.height = h;
  } catch (_) {}
}
if (window.ResizeObserver) new ResizeObserver(fit).observe(document.body);

// ── Pengelola di jendela utama ──
function manager() {
  try {
    const cur = P.__idsUploads;
    if (!cur || cur.version !== C.version) {
      const s = P.document.createElement("script");
      s.textContent = C.manager;
      P.document.head.appendChild(s);
    }
    return P.__idsUploads || null;
  } catch (_) { return null; }
}
const M = manager();

$("pickLabel").textContent = L.btn;
$("hint").textContent = fmt(L.hint, { limit: LIMIT,
  formats: C.accept.split(",").map((s) => s.slice(1).toUpperCase()).join(", ") });
$("f").accept = C.accept;
$("cancel").title = L.cancel;
$("cancel").setAttribute("aria-label", L.cancel);

function row(name, meta, opts = {}) {
  const state = opts.state || "up";
  const ext = (name.split(".").pop() || "").toUpperCase().slice(0, 6);
  $("row").hidden = false;
  $("row").className = "file " + state;
  $("badge").textContent = ext;
  $("badge").className = "badge" + (ext === "CSV" ? " csv" : "");
  $("name").textContent = name;
  $("name").title = name;
  $("meta").textContent = state === "ok" ? "✓ " + meta : meta;
  if (opts.pct !== undefined) $("fill").style.width = opts.pct.toFixed(1) + "%";
  $("bar").hidden = opts.pct === undefined;
  fit();
}
function lockZone(on) {
  $("pick").disabled = on;
  $("zone").classList.toggle("off", on);
}
function pauseButton(visible, paused) {
  $("pause").hidden = !visible;
  // Atribut, bukan properti `.hidden`: elemen SVG tidak punya properti itu.
  $("icoPause").toggleAttribute("hidden", paused);
  $("icoPlay").toggleAttribute("hidden", !paused);
  const label = paused ? L.resume : L.pause;
  $("pause").title = label;
  $("pause").setAttribute("aria-label", label);
}

// Menggambar keadaan pekerjaan dari pengelola.
function render(s) {
  const pct = s.size ? s.sent / s.size * 100 : 0;
  if (s.state === "connecting") {
    row(s.name, size(s.size) + " · " + L.connecting, { pct: 0 });
    pauseButton(true, false); lockZone(true);
  } else if (s.state === "up") {
    const parts = [size(s.sent) + " / " + size(s.size), pct.toFixed(1) + "%"];
    if (s.rate > 0) {
      parts.push((s.rate / 1024 ** 2).toFixed(1) + " MB/s");
      parts.push(fmt(L.left, { min: Math.max(1, Math.round((s.size - s.sent) / s.rate / 60)) }));
    } else if (s.resumed > 0) {
      parts.splice(0, 2, fmt(L.resuming, { done: size(s.resumed) }));
    }
    row(s.name, parts.join(" · "), { pct });
    pauseButton(true, false); lockZone(true);
  } else if (s.state === "warn") {
    row(s.name, fmt(L.retry, { sec: s.wait, safe: size(s.received) }), { pct: s.received / s.size * 100, state: "warn" });
    pauseButton(true, false); lockZone(true);
  } else if (s.state === "hold") {
    const p = s.received / s.size * 100;
    row(s.name, fmt(L.paused_at, { done: size(s.received), pct: p.toFixed(1) + "%" }), { pct: p, state: "hold" });
    pauseButton(true, true); lockZone(true);
  } else if (s.state === "ok") {
    row(s.name, size(s.size) + " · " + (C.done ? L.received : L.done), { pct: 100, state: "ok" });
    pauseButton(false, false); lockZone(true);
  } else if (s.state === "err") {
    const e = s.error || {};
    const msg = e.kind === "expired" ? L.expired
              : e.kind === "stopped" ? fmt(L.stopped, { err: e.text })
              : (size(s.size) + " · " + (e.text || ""));
    row(s.name, msg, { state: "err", pct: e.kind === "expired" ? s.received / s.size * 100 : undefined });
    pauseButton(false, false); lockZone(false);
  }
}

function upload(file) {
  if (file.size > C.max) {
    row(file.name, size(file.size) + " · " + fmt(L.too_big, { limit: LIMIT }), { state: "err" });
    return;
  }
  if (!M) { row(file.name, "JS: upload manager unavailable", { state: "err" }); return; }
  // Berkasnya dibungkus ulang sebagai File milik jendela utama (tanpa menyalin
  // isinya), supaya tetap dapat dibaca setelah iframe ini dibuang.
  const owned = new P.File([file], file.name, { type: file.type, lastModified: file.lastModified });
  M.start(C.token, { prefix: C.prefix, chunk: C.chunk, labels: L }, owned);
  M.subscribe(C.token, window.frameElement, render);
}

$("pause").addEventListener("click", () => {
  const s = M && M.get(C.token);
  if (!s) return;
  if (s.state === "hold") M.resume(C.token); else M.pause(C.token);
});
$("cancel").addEventListener("click", async () => {
  if (M) await M.cancel(C.token, C.prefix);
  else try { await fetch(C.prefix + "/" + C.token, { method: "DELETE" }); } catch (_) {}
  $("row").hidden = true;
  $("f").value = "";
  pauseButton(false, false);
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
window.addEventListener("error", (e) => row($("name").textContent || "-", "JS: " + e.message, { state: "err" }));

// Keadaan awal: tempel pada unggahan yang masih berjalan di jendela utama
// (pengguna kembali ke halaman ini), atau tampilkan berkas yang sudah lengkap.
if (!(M && M.subscribe(C.token, window.frameElement, render)) && C.done) {
  row(C.done.name, size(C.done.size) + " · " + L.received, { pct: 100, state: "ok" });
  lockZone(true);
}
fit();
</script>
"""
