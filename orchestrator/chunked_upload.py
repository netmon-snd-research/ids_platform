"""
Unggahan dataset BERTAHAP: berkas dikirim per potongan, dapat dilanjutkan.

Kenapa tidak memakai ``st.file_uploader``: uploader bawaan mengirim SELURUH
berkas dalam satu request HTTP dan menahannya utuh di RAM server
(``MemoryUploadedFileManager``). Lewat jalur browser → VPS → VPN → server lab,
satu putus sesaat di menit ke-40 menggagalkan berkas 5 GB seluruhnya
("AxiosError: Network Error"), dan berkas 20 GB berarti 20 GB RAM.

Di sini berkas dikirim per potongan (``CHUNK_BYTES``) ke rute HTTP milik
aplikasi ini sendiri, di port yang SAMA dengan Streamlit (dipasang lewat
``st.App`` di ``ui/serve.py``). Setiap potongan ditulis langsung ke disk.
Potongan yang gagal dicoba ulang oleh browser; unggahan yang terputus
dilanjutkan dari byte terakhir yang diterima — juga setelah halaman dimuat
ulang atau server dijalankan ulang, karena berkas parsialnya dikenali dari
(pengguna, nama, ukuran).

Izin: rute hanya menerima TOKEN acak yang dibuat halaman untuk pengguna yang
sudah lolos ``can_upload``. Menyimpan berkasnya ke ``storage/datasets/`` tetap
lewat ``save_dataset_upload``, yang menegakkan ``require_upload`` lagi.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
from pathlib import Path

from config.settings import DATASETS_DIR, STORAGE_DIR

#: Batas ukuran satu dataset yang diunggah lewat peramban.
#:
#: 20 GB, bukan lebih: CSV di atas pagu worker / 1,5 (32000 / 1,5 ≈ 21 GB pada
#: server lab) toh dikunci `dataset_ram_blocker` di Run Experiment. Berkas yang
#: lebih besar tetap dapat masuk lewat tab Daftarkan dari server.
MAX_DATASET_UPLOAD_BYTES = 20 * 1024 * 1024 * 1024

DATASET_EXTENSIONS = (".csv", ".ndjson", ".jsonl", ".json")
_SAFE_DATASET_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

#: Ukuran potongan yang dikirim peramban. Cukup kecil agar satu request selesai
#: dalam hitungan detik (tidak kena batas waktu/ukuran proxy di VPS), cukup
#: besar agar 20 GB tidak menjadi puluhan ribu request.
CHUNK_BYTES = 8 * 1024 * 1024
#: Batas yang DITERIMA server per request — longgar di atas CHUNK_BYTES.
MAX_CHUNK_BYTES = 16 * 1024 * 1024

#: Awalan rute HTTP. Di luar `/_stcore/` milik Streamlit.
ROUTE_PREFIX = "/ids-upload"

#: Umur token unggah. Unggahan yang lebih lama dari ini cukup dilanjutkan
#: dengan memilih berkasnya lagi (token baru, berkas parsial yang sama).
TOKEN_TTL_S = 24 * 3600
#: Berkas parsial yang tidak disentuh selama ini dianggap ditinggalkan.
STALE_PART_S = 72 * 3600

CHUNK_DIR = Path(STORAGE_DIR) / "_upload_tmp" / "chunked"

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

_lock = threading.Lock()
#: token -> {"user": str, "expires": float, "upload": dict | None}
_tokens: dict[str, dict] = {}


class UploadError(Exception):
    """Penolakan yang pesannya boleh ditampilkan ke pengunggah."""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def safe_dataset_name(filename: str) -> str | None:
    """Nama berkas dataset yang aman, atau None bila tidak layak.

    Menolak (bukan memotong) nama ber-separator, nama aneh, dan ekstensi di
    luar daftar — sehingga unggahan tidak pernah dapat menulis ke luar
    `storage/datasets/`.
    """
    name = filename or ""
    if "/" in name or "\\" in name or name != Path(name).name:
        return None
    if name in ("", ".", "..") or not _SAFE_DATASET_NAME.match(name):
        return None
    if Path(name).suffix.lower() not in DATASET_EXTENSIONS:
        return None
    return name


# ── Token & keadaan (dipakai halaman Streamlit) ───────────────────────────

def mint_token(username: str) -> str:
    """Token unggah baru untuk ``username``. Pemanggil SUDAH memeriksa izinnya."""
    token = secrets.token_hex(16)
    with _lock:
        _tokens[token] = {"user": str(username),
                          "expires": time.time() + TOKEN_TTL_S,
                          "upload": None}
    return token


def token_valid(token: str | None) -> bool:
    with _lock:
        entry = _tokens.get(token or "")
        return bool(entry) and entry["expires"] > time.time()


def upload_state(token: str | None) -> dict | None:
    """Salinan keadaan unggahan milik token ini, atau None."""
    with _lock:
        entry = _tokens.get(token or "")
        if not entry or not entry["upload"]:
            return None
        return dict(entry["upload"])


def discard(token: str | None, *, delete_part: bool = True) -> None:
    """Lupakan token ini; hapus berkas parsialnya bila diminta."""
    with _lock:
        entry = _tokens.pop(token or "", None)
    upload = (entry or {}).get("upload")
    if delete_part and upload:
        try:
            Path(upload["path"]).unlink(missing_ok=True)
        except OSError:                       # pragma: no cover - defensif
            pass


def cleanup_stale_parts(max_age_s: float = STALE_PART_S) -> int:
    """Hapus berkas parsial yang ditinggalkan. Mengembalikan jumlahnya."""
    if not CHUNK_DIR.is_dir():
        return 0
    batas = time.time() - max_age_s
    dibuang = 0
    for part in CHUNK_DIR.glob("*.part"):
        try:
            if part.stat().st_mtime < batas:
                part.unlink()
                dibuang += 1
        except OSError:                       # pragma: no cover - defensif
            continue
    return dibuang


# ── Operasi unggah (dipakai rute HTTP) ────────────────────────────────────

def _entry(token: str) -> dict:
    if not _TOKEN_RE.match(token or ""):
        raise UploadError("Token unggah tidak valid.", 403)
    entry = _tokens.get(token)
    if not entry or entry["expires"] <= time.time():
        raise UploadError("Sesi unggah kedaluwarsa. Muat ulang halaman.", 403)
    return entry


def _part_path(user: str, name: str, size: int) -> Path:
    # Dikenali dari (pengguna, nama, ukuran), jadi memilih berkas yang sama
    # lagi melanjutkan berkas parsial yang sama — juga dengan token baru.
    key = hashlib.sha256(f"{user}\0{name}\0{size}".encode()).hexdigest()[:32]
    return CHUNK_DIR / f"{key}.part"


def begin(token: str, filename: str, size: int) -> dict:
    """Mulai atau LANJUTKAN unggahan. Mengembalikan keadaannya."""
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise UploadError("Ukuran berkas tidak valid.")
    name = safe_dataset_name(filename)
    if name is None:
        raise UploadError(
            "Nama berkas tidak valid. Gunakan huruf/angka/._- tanpa folder, "
            "berekstensi .csv, .ndjson, .jsonl, atau .json.")
    if size <= 0:
        raise UploadError("Berkas kosong.")
    if size > MAX_DATASET_UPLOAD_BYTES:
        raise UploadError(
            f"Berkas melebihi batas unggah {MAX_DATASET_UPLOAD_BYTES // 1024 ** 3} GB. "
            "Salin ke storage/datasets/ di server, lalu pakai tab "
            "Daftarkan dari server.", 413)
    if (Path(DATASETS_DIR) / name).exists():
        raise UploadError(f"Berkas {name} sudah ada di storage/datasets/.", 409)

    with _lock:
        entry = _entry(token)
        path = _part_path(entry["user"], name, size)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()
        received = path.stat().st_size
        if received > size:                   # sisa unggahan rusak: mulai ulang
            path.write_bytes(b"")
            received = 0
        entry["upload"] = {"filename": name, "size": size, "path": str(path),
                           "received": received, "done": received == size}
        return dict(entry["upload"])


def write_chunk(token: str, offset: int, data: bytes) -> dict:
    """Tulis satu potongan di ``offset``. Ketat: hanya MENYAMBUNG di ujung.

    Offset yang tidak sama dengan jumlah byte yang sudah diterima ditolak
    (409) beserta angka yang benar, supaya peramban menyelaraskan diri —
    misalnya potongan yang sebenarnya sudah tertulis tetapi jawabannya hilang
    di jalan, lalu dikirim ulang.
    """
    with _lock:
        entry = _entry(token)
        upload = entry["upload"]
        if not upload:
            raise UploadError("Unggahan belum dimulai.", 409, received=0)
        path = Path(upload["path"])
        received = path.stat().st_size if path.exists() else 0
        upload["received"] = received
        if offset != received:
            raise UploadError("Posisi potongan tidak cocok.", 409,
                              received=received)
        if received + len(data) > upload["size"]:
            raise UploadError("Potongan melewati ukuran berkas.", 400,
                              received=received)
        with open(path, "ab") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        upload["received"] = received + len(data)
        upload["done"] = upload["received"] == upload["size"]
        entry["expires"] = max(entry["expires"], time.time() + 3600)
        return dict(upload)


def cancel(token: str) -> None:
    """Batalkan unggahan token ini: berkas parsialnya DIHAPUS, tokennya tetap.

    Token tetap berlaku supaya kontrol yang sama langsung dapat dipakai untuk
    berkas lain tanpa memuat ulang halaman.
    """
    with _lock:
        entry = _entry(token)
        upload, entry["upload"] = entry["upload"], None
    if upload:
        try:
            Path(upload["path"]).unlink(missing_ok=True)
        except OSError:                       # pragma: no cover - defensif
            pass


def status(token: str) -> dict:
    with _lock:
        entry = _entry(token)
        upload = entry["upload"]
        if not upload:
            return {"received": 0, "done": False}
        path = Path(upload["path"])
        upload["received"] = path.stat().st_size if path.exists() else 0
        upload["done"] = upload["received"] == upload["size"]
        return dict(upload)


# ── Rute HTTP (Starlette, dipasang di ui/serve.py) ────────────────────────

def routes() -> list:
    """Rute Starlette untuk ``st.App(..., routes=routes())``."""
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def _public(upload: dict) -> dict:
        return {"filename": upload.get("filename"), "size": upload.get("size"),
                "received": upload.get("received", 0),
                "done": bool(upload.get("done"))}

    def _error(e: UploadError) -> JSONResponse:
        return JSONResponse({"error": str(e), **e.extra}, status_code=e.status)

    async def begin_route(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Permintaan tidak valid."}, status_code=400)
        try:
            upload = await run_in_threadpool(
                begin, str(body.get("token") or ""), str(body.get("filename") or ""),
                body.get("size"))
        except UploadError as e:
            return _error(e)
        return JSONResponse({**_public(upload), "chunk": CHUNK_BYTES})

    async def chunk_route(request: Request) -> JSONResponse:
        token = request.path_params["token"]
        try:
            offset = int(request.query_params.get("offset", "-1"))
        except ValueError:
            return JSONResponse({"error": "Offset tidak valid."}, status_code=400)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_CHUNK_BYTES:
            return JSONResponse({"error": "Potongan terlalu besar."}, status_code=413)
        # Dibaca dengan batas, bukan `await request.body()` utuh: header
        # Content-Length boleh tidak ada atau berbohong.
        parts, total = [], 0
        async for piece in request.stream():
            total += len(piece)
            if total > MAX_CHUNK_BYTES:
                return JSONResponse({"error": "Potongan terlalu besar."},
                                    status_code=413)
            parts.append(piece)
        try:
            upload = await run_in_threadpool(write_chunk, token, offset,
                                             b"".join(parts))
        except UploadError as e:
            return _error(e)
        return JSONResponse(_public(upload))

    async def status_route(request: Request) -> JSONResponse:
        try:
            if request.method == "DELETE":
                await run_in_threadpool(cancel, request.path_params["token"])
                return JSONResponse({"received": 0, "done": False})
            upload = await run_in_threadpool(status, request.path_params["token"])
        except UploadError as e:
            return _error(e)
        return JSONResponse(_public(upload))

    return [
        Route(f"{ROUTE_PREFIX}/begin", begin_route, methods=["POST"]),
        Route(f"{ROUTE_PREFIX}/{{token}}/chunk", chunk_route, methods=["PUT"]),
        Route(f"{ROUTE_PREFIX}/{{token}}", status_route, methods=["GET", "DELETE"]),
    ]
