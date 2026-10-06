"""
Kontrol unggah dataset BERTAHAP di halaman Tambah Pipeline & Dataset.

Pasangan sisi peramban dari ``orchestrator/chunked_upload.py``. Peramban
memotong berkas per ``CHUNK_BYTES``, mengirim tiap potongan ke rute
``/ids-upload`` di server yang sama, mencoba ulang potongan yang gagal dengan
jeda yang makin panjang, dan menyelaraskan diri dari jawaban server bila
sebuah jawaban hilang di jalan. Memilih berkas yang sama lagi — setelah
koneksi putus total, halaman dimuat ulang, atau server dijalankan ulang —
melanjutkan dari byte terakhir yang sudah diterima, bukan dari nol.

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
def _watch(token: str) -> None:
    """Muat ulang halaman begitu server menerima berkas secara lengkap.

    Hanya fragmen kecil ini yang berjalan tiap dua detik; kontrol unggah
    (iframe) dan isi halaman lain tidak tersentuh, jadi unggahan yang sedang
    berjalan tidak terganggu.
    """
    state = cu.upload_state(token)
    if state and state.get("done"):
        st.rerun(scope="app")


def render_chunked_uploader(username: str, *, help_text: str = "") -> ChunkedFile | None:
    """Gambar kontrol unggah; kembalikan berkas bila sudah lengkap diterima."""
    token = _token(username)
    state = cu.upload_state(token)
    if state and state.get("done"):
        return ChunkedFile(state["path"], state["filename"], state["size"], token)

    import streamlit.components.v1 as components

    config = {
        "token": token,
        "prefix": cu.ROUTE_PREFIX,
        "max": cu.MAX_DATASET_UPLOAD_BYTES,
        "chunk": cu.CHUNK_BYTES,
        "accept": ",".join(cu.DATASET_EXTENSIONS),
    }
    components.html(_WIDGET_HTML.replace("__CONFIG__", json.dumps(config)),
                    height=150)
    if help_text:
        st.caption(help_text)
    _watch(token)
    return None


# Gaya mengikuti tema Streamlit lewat `color-scheme` dan warna netral
# transparan, sehingga terbaca pada tema terang maupun gelap.
_WIDGET_HTML = r"""
<style>
  :root { color-scheme: light dark; font-family: "Source Sans Pro", system-ui, sans-serif; }
  body { margin: 0; color: CanvasText; background: transparent; }
  .box { border: 1px dashed rgba(127,127,127,.45); border-radius: 10px;
         padding: 14px 16px; display: grid; gap: 8px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  label.btn { border: 1px solid rgba(127,127,127,.5); border-radius: 8px;
              padding: 6px 14px; cursor: pointer; font-size: 14px; }
  label.btn:focus-within { outline: 2px solid #ff4b4b; }
  input[type=file] { position: absolute; opacity: 0; width: 1px; height: 1px; }
  .hint { font-size: 13px; opacity: .7; }
  .bar { height: 8px; border-radius: 4px; background: rgba(127,127,127,.2); overflow: hidden; }
  .bar > div { height: 100%; width: 0; background: #ff4b4b; transition: width .3s; }
  .msg { font-size: 13px; min-height: 18px; overflow-wrap: anywhere; }
  .err { color: #e5484d; }
</style>
<div class="box">
  <div class="row">
    <label class="btn" tabindex="0">Pilih berkas<input id="f" type="file"></label>
    <span class="hint" id="hint"></span>
  </div>
  <div class="bar" id="bar" hidden><div id="fill"></div></div>
  <div class="msg" id="msg"></div>
</div>
<script>
const C = __CONFIG__;
const $ = (id) => document.getElementById(id);
const GB = (n) => (n / 1024 ** 3).toFixed(2) + " GB";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
$("f").accept = C.accept;
document.querySelector("label.btn").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("f").click(); }
});
$("hint").textContent = "Maks. " + Math.round(C.max / 1024 ** 3) + " GB per berkas · "
  + C.accept.replaceAll(",", ", ") + ". Unggahan yang terputus dapat dilanjutkan.";

function say(text, isError) {
  $("msg").textContent = text;
  $("msg").className = "msg" + (isError ? " err" : "");
}

async function api(path, opts) {
  const res = await fetch(C.prefix + path, opts);
  let body = {};
  try { body = await res.json(); } catch (_) {}
  return { status: res.status, body };
}

// Galat apa pun TAMPIL, bukan diam: kontrol yang tidak bereaksi tanpa pesan
// tidak dapat dibedakan dari unggahan yang sedang berjalan.
window.addEventListener("error", (e) => say("Kesalahan di peramban: " + e.message, true));
window.addEventListener("unhandledrejection", (e) => say("Unggahan berhenti: " + (e.reason && e.reason.message || e.reason), true));

// PUT satu potongan lewat XHR, bukan fetch: hanya XHR yang melaporkan
// kemajuan unggah di DALAM satu potongan, sehingga bilah progres bergerak
// sejak byte pertama alih-alih diam sampai 8 MB pertama selesai.
function putChunk(url, blob, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("PUT", url);
    x.timeout = 180000;
    x.upload.onprogress = (ev) => { if (ev.lengthComputable) onProgress(ev.loaded); };
    x.onload = () => {
      let body = {};
      try { body = JSON.parse(x.responseText); } catch (_) {}
      resolve({ status: x.status, body });
    };
    x.onerror = () => reject(new Error("jaringan"));
    x.ontimeout = () => reject(new Error("batas waktu"));
    x.send(blob);
  });
}

let running = false;
$("f").addEventListener("change", async () => {
  const file = $("f").files[0];
  if (!file || running) return;
  if (file.size > C.max) { say(file.name + " (" + GB(file.size) + ") melebihi batas " + GB(C.max) + ".", true); return; }
  running = true;
  $("bar").hidden = false;
  $("fill").style.width = "0%";
  say(file.name + " · " + GB(file.size) + " — menghubungi server…");
  let t0 = Date.now(), base = 0;
  const show = (sent) => {
    const pct = sent / file.size * 100;
    const secs = (Date.now() - t0) / 1000;
    const rate = secs > 1 ? (sent - base) / secs : 0;
    const eta = rate > 0 ? Math.round((file.size - sent) / rate / 60) : null;
    $("fill").style.width = pct.toFixed(1) + "%";
    say(file.name + " · " + GB(sent) + " / " + GB(file.size) + " (" + pct.toFixed(1) + "%)"
        + (rate > 0 ? " · " + (rate / 1024 ** 2).toFixed(1) + " MB/dtk" : "")
        + (eta !== null ? " · sisa ±" + eta + " menit" : ""));
  };
  try {
    // Mulai — atau lanjutkan berkas parsial yang sudah ada di server.
    let r;
    for (let i = 0; ; i++) {
      try { r = await api("/begin", { method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ token: C.token, filename: file.name, size: file.size }) });
            break; }
      catch (e) { say("Menghubungi server… (percobaan " + (i + 1) + ")"); await sleep(Math.min(30000, 1000 * 2 ** i)); }
    }
    if (r.status !== 200) { say(r.body.error || ("Ditolak server (" + r.status + ")."), true); return; }
    let received = r.body.received, fails = 0;
    base = received; t0 = Date.now();
    if (received > 0) say("Melanjutkan dari " + GB(received) + "…");
    show(received);
    while (received < file.size) {
      const end = Math.min(received + C.chunk, file.size);
      try {
        const at = received;
        const res = await putChunk(C.prefix + "/" + C.token + "/chunk?offset=" + at,
                                   file.slice(at, end), (loaded) => show(at + loaded));
        if (res.status === 200 || (res.status === 409 && typeof res.body.received === "number")) {
          received = res.body.received;
          if (res.status === 200) fails = 0;
        } else if (res.status === 403) {
          say((res.body.error || "Sesi unggah berakhir.") + " Muat ulang halaman, lalu pilih berkas yang sama untuk melanjutkan.", true);
          return;
        } else if (res.status >= 400 && res.status < 500) {
          say(res.body.error || ("Ditolak server (" + res.status + ")."), true);
          return;
        } else { throw new Error("HTTP " + res.status); }
      } catch (e) {
        // Jaringan putus / server sibuk: tunggu, tanyakan posisi terakhir, ulangi.
        fails++;
        const wait = Math.min(30000, 1000 * 2 ** Math.min(fails, 5));
        say("Koneksi terganggu (" + e.message + "), mencoba lagi dalam " + Math.round(wait / 1000)
            + " detik… " + GB(received) + " sudah aman di server.");
        await sleep(wait);
        try { const s = await api("/" + C.token, {}); if (s.status === 200) received = s.body.received; } catch (_) {}
        continue;
      }
      show(received);
    }
    $("fill").style.width = "100%";
    say(file.name + " selesai diunggah. Memeriksa berkas…");
  } catch (e) {
    say("Unggahan berhenti: " + e.message + ". Pilih berkas yang sama untuk melanjutkan.", true);
  } finally { running = false; }
});
</script>
"""
