"""
Identitas DEVICE (browser) penonton, dan penyaring tampilan yang memakainya.

Kenapa perlu: pengunjung tanpa akun semuanya tercatat ber-``owner`` NULL, jadi
platform tidak dapat membedakan satu pengunjung dari yang lain. Akibatnya
setiap orang yang membuka aplikasi melihat pipeline apa yang sedang dijalankan
orang lain. Run yang masih berjalan kini hanya tampil di device yang
memulainya (lihat :func:`ui.components.dashboard.visible_runs`).

Pengenalnya angka acak 128-bit di cookie ``ids_device``:

* **Dibaca** lewat ``st.context.cookies`` — cookie yang dikirim browser saat
  sesi dibuka.
* **Ditulis** oleh komponen HTML kecil, sebab Streamlit tidak dapat menyetel
  cookie dari Python. Iframe komponen berjalan dengan ``allow-same-origin``
  (diperiksa pada bundel frontend yang terpasang), jadi ``document.cookie`` di
  dalamnya menulis cookie untuk domain aplikasi ini.

Ini BUKAN autentikasi. Cookie ini tidak memberi hak apa pun; ia hanya
menentukan run berjalan mana yang ditampilkan. Menebaknya berarti menebak 128
bit acak. Browser lain, mode penyamaran, atau cookie yang dihapus berarti
device baru — run yang masih berjalan dari device lama tidak lagi tampil,
tetapi hasilnya tetap masuk riwayat bersama begitu selesai.
"""
from __future__ import annotations

import re
import uuid

import streamlit as st

COOKIE_NAME = "ids_device"

#: Batas umur cookie yang dihormati peramban modern (400 hari).
COOKIE_MAX_AGE_S = 400 * 24 * 3600

_SESSION_KEY = "_device_id"
#: True bila sesi ini memulai dengan pengenal BARU, sehingga cookie-nya harus
#: ditulis. Bertahan selama sesi (bukan sekali pakai): komponen yang hanya
#: tergambar pada satu run dapat terbuang oleh rerun berikutnya sebelum
#: iframe-nya sempat menjalankan skrip.
_WRITE_KEY = "_device_cookie_write"

_VALID = re.compile(r"^[0-9a-f]{32}$")


def device_id() -> str:
    """Pengenal device penonton ini. Selalu mengembalikan nilai."""
    current = st.session_state.get(_SESSION_KEY)
    if current:
        return current
    try:
        cookie = st.context.cookies.get(COOKIE_NAME)
    except Exception:                       # pragma: no cover - defensif
        cookie = None
    if cookie and _VALID.match(cookie):
        current = cookie
    else:
        current = uuid.uuid4().hex
        st.session_state[_WRITE_KEY] = True
    st.session_state[_SESSION_KEY] = current
    return current


def render_device_cookie() -> None:
    """Tulis cookie bila sesi ini memakai pengenal baru. Dipanggil ui/app.py.

    Komponennya setinggi 0 dan isinya sama di setiap run, jadi Streamlit
    memakai ulang iframe yang sama alih-alih memuatnya lagi.
    """
    did = device_id()
    if not st.session_state.get(_WRITE_KEY):
        return
    import streamlit.components.v1 as components
    components.html(
        "<script>document.cookie = "
        f"'{COOKIE_NAME}={did}; path=/; max-age={COOKIE_MAX_AGE_S}; SameSite=Lax';"
        "</script>",
        height=0,
    )


def viewer_identity() -> dict:
    """``owner`` + ``device_id`` untuk run yang dimulai penonton sesi ini."""
    from ui.views.login import current_user

    return {"owner": (current_user() or {}).get("username"),
            "device_id": device_id()}


def visible_to_viewer(experiments) -> list:
    """:func:`visible_runs` untuk penonton sesi ini (device + akun + peran)."""
    from orchestrator.auth_service import is_research_admin
    from ui.components.dashboard import visible_runs
    from ui.views.login import current_user

    user = current_user()
    return visible_runs(
        experiments,
        device_id=device_id(),
        username=(user or {}).get("username"),
        sees_all=is_research_admin(user),
    )
