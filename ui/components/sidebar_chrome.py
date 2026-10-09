"""
Gaya bersama untuk sidebar — jejak lokasi, gaya item navigasi, dan baris teks
yang semuanya berbaris pada satu garis kiri.

Pola acuannya navigasi Jenkins: tenang, banyak ruang kosong, sedikit garis, satu
warna aksen hanya untuk penanda halaman aktif.

**Kenapa warnanya aman di tema terang maupun gelap.** Menu tiga halaman dirender
oleh ``streamlit-option-menu``, sebuah komponen kustom di dalam *iframe* —
CSS halaman tidak dapat menjangkaunya, jadi satu-satunya kendali gaya adalah
dict ``styles`` miliknya. Untungnya Streamlit menyuntikkan variabel tema ke
dalam iframe itu (``--primary-color``, ``--background-color``,
``--secondary-background-color``, ``--text-color``), dan CSS bawaan komponennya
memang sudah memakai ``var(--text-color)``. Karena itu seluruh warna di sini
ditulis sebagai ``var(--…)``, bukan nilai heksa — warnanya ikut berganti sendiri
saat pengguna berpindah tema. Untuk elemen di luar iframe (jejak lokasi, baris
blok) dipakai ``color: inherit`` + ``opacity``, yang aman tanpa perlu tahu
temanya sama sekali.

**Perataan kiri.** Item navigasi menyisip ``INSET_PX`` dari tepi (3px di
antaranya berupa garis aksen di sisi kiri item aktif; item tidak aktif memakai
garis transparan selebar sama supaya teksnya tetap sebaris). Seluruh baris teks
milik blok lain dirender lewat ``sidebar_line`` yang memakai sisipan yang sama,
sehingga jejak lokasi, judul blok, isi progres, dan identitas berbaris rapi.
"""
from __future__ import annotations

from html import escape

import streamlit as st

# Sisipan kiri seluruh isi sidebar. Item navigasi mencapainya lewat
# border-left 3px + padding; baris teks lewat padding-left.
INSET_PX = 10
_NAV_ACCENT_PX = 3

# Jejak lokasi: informatif saja, tidak dapat diklik. Menu navigasi memakai
# komponen iframe yang tidak menyediakan perpindahan halaman dari luar secara
# andal, jadi tautan di sini justru akan menjadi tautan yang rusak.
BREADCRUMB_ROOT = "Menu"
BREADCRUMB_SEP = "›"

# Dua ukuran teks saja di seluruh sidebar (item navigasi memakai yang besar).
FONT_MAIN = "0.875rem"
FONT_SMALL = "0.78rem"


# ── Merek di puncak sidebar ───────────────────────────────────────────────

#: Nama aplikasi di puncak sidebar dan di judul tab browser (ui/app.py).
APP_NAME = "ReproIDS"


@st.cache_resource(show_spinner=False)
def _logo_data_uri() -> str:
    """Logo awan (ui/assets/cloud.png, sama dengan favicon) sebagai data URI.

    Disisipkan langsung ke HTML, jadi logo tampil bersama teksnya tanpa
    menunggu satu permintaan gambar lagi di jaringan yang lambat.
    """
    import base64
    from pathlib import Path

    berkas = Path(__file__).resolve().parent.parent / "assets" / "cloud.png"
    try:
        isi = base64.b64encode(berkas.read_bytes()).decode("ascii")
    except OSError:                         # pragma: no cover - defensif
        return ""
    return f"data:image/png;base64,{isi}"


def brand_html() -> str:
    """Logo + nama aplikasi untuk puncak sidebar."""
    uri = _logo_data_uri()
    logo = (f'<img src="{uri}" width="24" height="24" alt="" '
            f'style="flex:0 0 auto;">' if uri else "")
    return (f'<div style="display:flex;align-items:center;gap:.5rem;'
            f'padding:2px 0 2px {INSET_PX}px;">{logo}'
            f'<span style="font-size:1.05rem;font-weight:700;'
            f'letter-spacing:.01em;">{escape(APP_NAME)}</span></div>')


def render_brand() -> None:
    """Logo awan + "ReproIDS" di paling atas sidebar.

    Menggantikan jejak lokasi "Menu › …": menu tepat di bawahnya sudah
    menandai halaman aktif, jadi jejak itu mengulang hal yang sama.
    """
    st.markdown(brand_html(), unsafe_allow_html=True)


# ── Baris teks dengan perataan kiri yang sama ─────────────────────────────

def sidebar_line(text: str, *, muted: bool = False, strong: bool = False,
                 small: bool = False) -> str:
    """Satu baris teks sidebar sebagai HTML. Murni; isinya selalu di-escape.

    Bobot & ukuran ditentukan di sini (bukan di pemanggil) supaya jumlah gaya
    di seluruh sidebar tetap sedikit. Baris tidak pernah membungkus: teks yang
    kepanjangan dipotong dengan elipsis, jadi jarak antar-baris tetap seragam
    berapa pun lebar sidebar yang dipilih pengguna.
    """
    # `overflow:hidden` memberi elipsis ke SAMPING; tanpa padding & line-height
    # yang cukup ia ikut memangkas tinggi huruf (jejak lokasi pernah terpotong
    # di tepi atas sidebar karena ini).
    style = (
        f"padding:2px 0 2px {INSET_PX}px;"
        f"font-size:{FONT_SMALL if small else FONT_MAIN};"
        "line-height:1.7;"
        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"
    )
    if muted:
        style += "opacity:.6;"
    if strong:
        style += "font-weight:600;"
    return f'<div style="{style}">{escape(str(text))}</div>'


def render_line(text: str, **kwargs) -> None:
    st.markdown(sidebar_line(text, **kwargs), unsafe_allow_html=True)


def progress_bar_html(percent) -> str:
    """Bar progres tipis, sejajar dengan baris teks di sebelahnya.

    Dibuat sendiri (bukan ``st.progress``) supaya sisipan kirinya persis sama
    dengan baris teks lain. Warna isian memakai aksen tema dengan cadangan
    ``currentColor``; jalurnya abu transparan yang terbaca di kedua tema.
    """
    pct = max(0, min(100, int(percent)))
    return (
        f'<div style="padding-left:{INSET_PX}px;margin:.15rem 0 .25rem;">'
        f'<div style="height:4px;border-radius:2px;'
        f'background:rgba(128,128,128,.25);overflow:hidden;">'
        f'<div style="width:{pct}%;height:100%;'
        f'background:var(--primary-color,currentColor);opacity:.8;"></div>'
        f"</div></div>"
    )


def render_progress_bar(percent) -> None:
    st.markdown(progress_bar_html(percent), unsafe_allow_html=True)


# ── Gaya item navigasi ────────────────────────────────────────────────────

def menu_styles() -> dict:
    """Dict ``styles`` untuk ``option_menu``. Murni — isinya diperiksa test.

    Item aktif: latar netral lembut (``--secondary-background-color``), sudut
    membulat, teks lebih tegas, ditambah satu garis aksen tipis di kiri. Item
    lain polos — hanya garis transparan selebar sama supaya teks kedua keadaan
    tetap berbaris. Jarak vertikal seragam untuk semua item.
    """
    pad_x = INSET_PX - _NAV_ACCENT_PX
    return {
        "container": {
            "padding": "0",
            "background-color": "transparent",
        },
        "icon": {
            # Ukuran ikon seragam; warnanya ikut teks, bukan warna sendiri.
            "font-size": "0.95rem",
            "color": "inherit",
        },
        "nav-link": {
            "font-size": FONT_MAIN,
            "font-weight": "400",
            "text-align": "left",
            "color": "var(--text-color)",
            "margin": "1px 0",
            "padding": f"7px {pad_x}px",
            "border-radius": "6px",
            "border-left": f"{_NAV_ACCENT_PX}px solid transparent",
            "--hover-color": "var(--secondary-background-color)",
        },
        "nav-link-selected": {
            # Latar LEMBUT & netral — bukan blok warna mencolok. Satu-satunya
            # aksen adalah garis kiri tipis.
            "background-color": "var(--secondary-background-color)",
            "color": "var(--text-color)",
            "font-weight": "600",
            "border-left": f"{_NAV_ACCENT_PX}px solid var(--primary-color)",
        },
    }
