"""
Sesi login yang BERTAHAN saat halaman dimuat ulang.

Sebelumnya identitas hanya hidup di ``st.session_state``: refresh membuka sesi
Streamlit baru, dan Kontributor/Research Admin kembali menjadi pengunjung.
Modul ini menyimpan sesi login di basis data dan menandai browser lewat cookie
``ids_session``.

Alurnya:

1. Login berhasil di halaman → ``mint_claim`` membuat KODE TUKAR sekali pakai
   (berumur pendek, hanya di memori proses).
2. Komponen kecil di halaman mengirim kode itu ke ``POST /ids-auth/claim``.
   Rute itu membuat baris sesi dan memasang cookie ``HttpOnly``. Token
   panjangnya tidak pernah melewati JavaScript, jadi skrip di halaman tidak
   dapat membacanya.
3. Sesi Streamlit baru (refresh, tab baru) membaca cookie itu lewat
   ``st.context.cookies`` dan memulihkan identitasnya dengan ``resolve``.
4. Keluar → ``revoke`` menghapus barisnya; ``DELETE /ids-auth/session``
   membuang cookie-nya, tetapi hanya bila cookie itu milik sesi yang keluar
   atau sudah tidak berlaku. Tab lain yang keluar lebih dulu tidak dapat
   membuang cookie login yang lebih baru.

Yang disimpan di basis data hanya SHA-256 dari token, bukan token itu sendiri.
Peran dan status akun dibaca ulang dari tabel ``users`` setiap kali sesi
dipulihkan: akun yang dinonaktifkan tidak dapat dipulihkan, dan peran yang
diubah Research Admin langsung berlaku.

Rute HTTP hanya ada bila server dijalankan lewat ``ui/serve.py`` (seperti di
Docker). Dengan ``streamlit run ui/app.py`` rutenya tidak ada; login tetap
berfungsi, hanya tidak bertahan saat refresh.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
import uuid

from database.db import _retry_on_locked, get_connection
from database.models import STATUS_DISABLED
from orchestrator.auth_service import get_user
from utils.timestamps import now_iso

logger = logging.getLogger(__name__)

COOKIE_NAME = "ids_session"

#: Umur sesi login, mutlak sejak masuk (tidak diperpanjang oleh pemakaian).
SESSION_TTL_S = 7 * 24 * 3600

#: Umur kode tukar. Cukup untuk satu rerun dan satu request dari peramban.
CLAIM_TTL_S = 120

#: Awalan rute HTTP. Di luar `/_stcore/` milik Streamlit.
ROUTE_PREFIX = "/ids-auth"

#: Header wajib pada setiap request ke rute ini. Header khusus memaksa
#: peramban melakukan preflight CORS untuk request lintas situs, dan preflight
#: itu tidak pernah disetujui. Jadi situs lain tidak dapat menukarkan kode
#: miliknya ke peramban korban (login CSRF) atau memaksanya keluar.
GUARD_HEADER = "X-IDS-Session"

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CODE_RE = re.compile(r"^[0-9a-f]{64}$")
_SID_RE = re.compile(r"^[0-9a-f]{32}$")

_lock = threading.Lock()
#: kode tukar -> {"sid": str, "username": str, "expires": float}
_claims: dict[str, dict] = {}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


# ── Dipakai halaman Streamlit ─────────────────────────────────────────────

def mint_claim(username: str) -> tuple[str, str]:
    """``(sid, kode)`` untuk ``username`` yang BARU SAJA lolos ``authenticate``.

    ``sid`` adalah pengenal sesi yang disimpan halaman untuk mencabutnya saat
    keluar. Ia tidak memberi hak apa pun; yang memberi hak hanya token di
    cookie.
    """
    sid = uuid.uuid4().hex
    code = secrets.token_hex(32)
    now = time.time()
    with _lock:
        for stale in [c for c, e in _claims.items() if e["expires"] <= now]:
            _claims.pop(stale, None)
        _claims[code] = {"sid": sid, "username": str(username),
                         "expires": now + CLAIM_TTL_S}
    return sid, code


def resolve(token: str | None, db_path: str | None = None) -> tuple[dict, str] | None:
    """``(pengguna, sid)`` milik token cookie ini, atau None.

    None bila token tidak dikenal, sudah kedaluwarsa, dicabut, atau akunnya
    sudah tidak ada atau dinonaktifkan.
    """
    if not token or not _TOKEN_RE.match(token):
        return None
    _delete_expired(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id, username FROM login_sessions WHERE token_hash = ? "
            "AND expires_at > ?", (_hash(token), time.time())).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    user = get_user(row["username"], db_path)
    if user is None or user["status"] == STATUS_DISABLED:
        revoke(row["id"], db_path)
        return None
    return user, row["id"]


@_retry_on_locked()
def revoke(sid: str | None, db_path: str | None = None) -> None:
    """Cabut satu sesi: baris di basis data dan kode tukarnya bila belum dipakai."""
    if not sid:
        return
    with _lock:
        for code in [c for c, e in _claims.items() if e["sid"] == sid]:
            _claims.pop(code, None)
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM login_sessions WHERE id = ?", (sid,))
        conn.commit()
    finally:
        conn.close()


@_retry_on_locked()
def revoke_user(username: str, *, keep: str | None = None,
                db_path: str | None = None) -> None:
    """Cabut SEMUA sesi milik ``username``, kecuali sesi ``keep``.

    Dipanggil saat sandi diganti atau direset, dan saat akun dinonaktifkan:
    browser lain yang masih memegang cookie lama harus masuk ulang.
    """
    with _lock:
        for code in [c for c, e in _claims.items()
                     if e["username"] == username and e["sid"] != keep]:
            _claims.pop(code, None)
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM login_sessions WHERE username = ? AND id IS NOT ?",
                     (username, keep))
        conn.commit()
    finally:
        conn.close()


# ── Dipakai rute HTTP ─────────────────────────────────────────────────────

@_retry_on_locked()
def claim(code: str | None, db_path: str | None = None) -> str | None:
    """Tukar kode sekali pakai dengan token sesi baru, atau None bila ditolak."""
    if not code or not _CODE_RE.match(code):
        return None
    with _lock:
        entry = _claims.pop(code, None)
    if not entry or entry["expires"] <= time.time():
        return None
    user = get_user(entry["username"], db_path)
    if user is None or user["status"] == STATUS_DISABLED:
        return None
    token = secrets.token_urlsafe(32)
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO login_sessions (id, token_hash, username, created_at,
                                           expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entry["sid"], _hash(token), entry["username"], now_iso(),
             time.time() + SESSION_TTL_S))
        conn.commit()
    finally:
        conn.close()
    return token


def forget(token: str | None, sid: str | None,
           db_path: str | None = None) -> bool:
    """Apakah cookie bertoken ini boleh dibuang oleh sesi ``sid`` yang keluar.

    Ya bila cookie-nya milik ``sid`` (sesinya sekalian dicabut) atau sudah
    tidak menunjuk ke sesi yang berlaku. Tidak bila cookie-nya milik sesi
    LAIN yang masih berlaku: itu login yang lebih baru, mis. dari tab lain.
    """
    if not token or not _TOKEN_RE.match(token):
        return True
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM login_sessions WHERE token_hash = ? "
            "AND expires_at > ?", (_hash(token), time.time())).fetchone()
    finally:
        conn.close()
    if row is None:
        return True
    if sid and row["id"] == sid:
        revoke(sid, db_path)
        return True
    return False


@_retry_on_locked()
def _delete_expired(db_path: str | None = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM login_sessions WHERE expires_at <= ?",
                     (time.time(),))
        conn.commit()
    finally:
        conn.close()


# ── Rute HTTP (Starlette, dipasang di ui/serve.py) ────────────────────────

def routes() -> list:
    """Rute Starlette untuk ``st.App(..., routes=routes())``."""
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def _secure(request: Request) -> bool:
        # Di belakang Caddy, request ke Streamlit sendiri berupa HTTP polos;
        # skema aslinya ada di X-Forwarded-Proto. Memalsukan header ini hanya
        # membuat cookie lebih ketat, tidak pernah lebih longgar.
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        return proto.split(",")[0].strip().lower() == "https"

    def _guarded(request: Request) -> bool:
        return request.headers.get(GUARD_HEADER) == "1"

    async def claim_route(request: Request) -> JSONResponse:
        if not _guarded(request):
            return JSONResponse({"error": "Permintaan ditolak."}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Permintaan tidak valid."}, status_code=400)
        token = await run_in_threadpool(claim, str((body or {}).get("code") or ""))
        if token is None:
            return JSONResponse({"error": "Kode tidak berlaku."}, status_code=404)
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL_S, path="/",
                            secure=_secure(request), httponly=True,
                            samesite="lax")
        return response

    async def session_route(request: Request) -> JSONResponse:
        if not _guarded(request):
            return JSONResponse({"error": "Permintaan ditolak."}, status_code=403)
        sid = request.query_params.get("sid") or ""
        cleared = await run_in_threadpool(
            forget, request.cookies.get(COOKIE_NAME),
            sid if _SID_RE.match(sid) else None)
        response = JSONResponse({"cleared": cleared})
        if cleared:
            response.delete_cookie(COOKIE_NAME, path="/",
                                   secure=_secure(request), httponly=True,
                                   samesite="lax")
        return response

    return [
        Route(f"{ROUTE_PREFIX}/claim", claim_route, methods=["POST"]),
        Route(f"{ROUTE_PREFIX}/session", session_route, methods=["DELETE"]),
    ]
