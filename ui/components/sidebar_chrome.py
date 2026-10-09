"""
Gaya bersama untuk sidebar: merek di puncaknya, bilah progres, dan baris teks
yang semuanya berbaris pada satu garis kiri.

Pola acuannya navigasi Jenkins: tenang, banyak ruang kosong, sedikit garis, satu
warna aksen hanya untuk penanda halaman aktif. Menu halamannya sendiri tombol
bawaan Streamlit (ui/app.py), bergaya lewat `.st-key-ids_nav` di theme.py.

**Warna aman di tema terang maupun gelap.** Elemen di sini memakai
``color: inherit`` + ``opacity``, yang benar tanpa perlu tahu temanya.

**Perataan kiri.** Item navigasi menyisip ``INSET_PX`` dari tepi (3px di
antaranya berupa garis aksen di sisi kiri item aktif; item tidak aktif memakai
garis transparan selebar sama supaya teksnya tetap sebaris). Seluruh baris teks
milik blok lain dirender lewat ``sidebar_line`` yang memakai sisipan yang sama,
sehingga merek, judul blok, isi progres, dan identitas berbaris rapi.
"""
from __future__ import annotations

from html import escape

import streamlit as st

# Sisipan kiri seluruh isi sidebar. Item navigasi mencapainya lewat
# border-left 3px + padding; baris teks lewat padding-left.
INSET_PX = 10


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
    logo = (f'<img src="{uri}" width="32" height="32" alt="" '
            f'style="flex:0 0 auto;">' if uri else "")
    # Ditarik ke atas (margin negatif) supaya merek berdiri sejajar dengan
    # bilah tombol sidebar, bukan mengambang di bawahnya.
    return (f'<div style="display:flex;align-items:center;gap:.6rem;'
            f'margin-top:-2rem;padding:2px 0 2px {INSET_PX}px;">{logo}'
            f'<span style="font-size:1.35rem;font-weight:700;'
            f'letter-spacing:.01em;line-height:1.2;">{escape(APP_NAME)}</span></div>')


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


