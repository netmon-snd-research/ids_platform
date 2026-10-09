"""
Kepemilikan dan visibilitas dataset di ``storage/datasets/``.

Dataset diunggah sebagai berkas biasa di satu folder; tabel ``datasets``
mencatat siapa pengunggahnya dan apakah ia PUBLIK atau PRIVAT. Berkas tanpa
baris (dataset lama, atau yang disalin langsung ke server) diperlakukan
publik tanpa pemilik.

Aturan:

* Dataset publik terlihat dan dapat dipakai siapa saja, termasuk pengunjung.
* Dataset privat hanya terlihat dan dapat dipakai pemiliknya dan Research
  Admin. "Privat" berarti tersembunyi di aplikasi, BUKAN terenkripsi: siapa
  pun yang memegang server tetap dapat membaca berkasnya.
* Menghapus hanya boleh oleh pemiliknya atau Research Admin; dataset tanpa
  pemilik hanya oleh Research Admin. Berkasnya benar-benar dihapus. Catatan
  eksperimen yang memakainya TETAP ada dan tetap terbaca pemiliknya, tetapi
  tidak dapat dijalankan ulang. Dataset yang sedang dipakai run yang masih
  mengantre atau berjalan tidak dapat dihapus.

Penegakannya ada DI SINI dan dipanggil lapis aksi (``create_and_run_experiment``
dan ``delete_dataset``), bukan hanya oleh tampilan yang menyembunyikan tombol.
"""
from __future__ import annotations

import logging
from pathlib import Path

from config.settings import DATASETS_DIR
from database.db import _retry_on_locked, get_connection
from utils.timestamps import now_iso

logger = logging.getLogger(__name__)

VIS_PUBLIC = "public"
VIS_PRIVATE = "private"
VISIBILITIES = (VIS_PUBLIC, VIS_PRIVATE)

#: Run yang masih hidup. Berkasnya tidak boleh hilang dari bawah worker.
_ACTIVE_STATUSES = ("QUEUED", "RUNNING")


class DatasetError(Exception):
    """Penolakan yang pesannya boleh ditampilkan; ``key`` untuk i18n."""

    def __init__(self, message: str, *, key: str = "", values: dict | None = None):
        super().__init__(message)
        self.key = key
        self.values = values or {}


# ── Baca ──────────────────────────────────────────────────────────────────

def all_rows(db_path: str | None = None) -> dict[str, dict]:
    """{nama berkas: baris} untuk setiap dataset yang tercatat."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT * FROM datasets").fetchall()
    finally:
        conn.close()
    return {r["filename"]: dict(r) for r in rows}


def registry_name(path: str | Path) -> str | None:
    """Nama berkas bila ``path`` berada langsung di ``storage/datasets/``.

    Dataset di luar folder itu (mis. dataset yang menyatu dengan research
    pipeline kontribusi) tidak diatur modul ini.
    """
    p = Path(path)
    try:
        if p.resolve().parent == Path(DATASETS_DIR).resolve():
            return p.name
    except OSError:                          # pragma: no cover - defensif
        return None
    return None


def info(filename: str, rows: dict | None = None) -> dict:
    """Pemilik dan visibilitas. Berkas tak tercatat: publik tanpa pemilik."""
    row = (all_rows() if rows is None else rows).get(filename) or {}
    vis = row.get("visibility")
    return {"owner": row.get("owner") or None,
            "visibility": vis if vis in VISIBILITIES else VIS_PUBLIC,
            "uploaded_at": row.get("uploaded_at") or ""}


def _sees_all(user: dict | None) -> bool:
    from orchestrator.auth_service import is_account_active, is_research_admin
    return is_account_active(user) and is_research_admin(user)


def can_see(user: dict | None, filename: str, rows: dict | None = None) -> bool:
    """Apakah ``user`` (None = pengunjung) boleh melihat dan memakai dataset ini."""
    meta = info(filename, rows)
    if meta["visibility"] == VIS_PUBLIC:
        return True
    username = (user or {}).get("username")
    return bool(username) and (meta["owner"] == username or _sees_all(user))


def can_delete(user: dict | None, filename: str, rows: dict | None = None) -> bool:
    """Pemilik (akun aktif) atau Research Admin. Tanpa pemilik: hanya admin."""
    from orchestrator.auth_service import is_account_active

    if not is_account_active(user):
        return False
    if _sees_all(user):
        return True
    owner = info(filename, rows)["owner"]
    return bool(owner) and owner == user.get("username")


def visible_paths(paths, user: dict | None, rows: dict | None = None) -> list:
    """Saring daftar path menjadi yang boleh dilihat ``user``."""
    rows = all_rows() if rows is None else rows
    out = []
    for p in paths or []:
        name = registry_name(p)
        if name is None or can_see(user, name, rows):
            out.append(p)
    return out


def dataset_available(path: str | Path) -> bool:
    """Berkasnya masih ada. Mengikuti cadangan nama-berkas ``sha256_file``:
    path dari lingkungan lain dicari ulang di ``storage/datasets/``."""
    p = Path(path)
    return p.is_file() or (Path(DATASETS_DIR) / p.name).is_file()


def access_blocker(user: dict | None, dataset_path: str | Path) -> str | None:
    """Kunci i18n alasan ``user`` tidak dapat menjalankan dataset ini, atau None."""
    if not dataset_available(dataset_path):
        return "err.dataset_deleted"
    name = registry_name(dataset_path) or registry_name(
        Path(DATASETS_DIR) / Path(dataset_path).name)
    if name is not None and not can_see(user, name):
        return "err.dataset_private"
    return None


def usage(filename: str, db_path: str | None = None) -> dict:
    """{"total": n, "active": n} eksperimen yang memakai dataset ini."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT dataset_path, status FROM experiments").fetchall()
    finally:
        conn.close()
    total = active = 0
    for r in rows:
        if Path(str(r["dataset_path"] or "")).name == filename:
            total += 1
            active += r["status"] in _ACTIVE_STATUSES
    return {"total": total, "active": active}


# ── Tulis ─────────────────────────────────────────────────────────────────

@_retry_on_locked()
def record(filename: str, *, owner: str | None, visibility: str,
           db_path: str | None = None) -> None:
    """Catat dataset yang BARU SAJA tersimpan. Visibilitas tak dikenal: publik."""
    if visibility not in VISIBILITIES:
        visibility = VIS_PUBLIC
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO datasets (filename, owner, visibility,
                                                uploaded_at)
               VALUES (?, ?, ?, ?)""",
            (filename, owner, visibility, now_iso()))
        conn.commit()
    finally:
        conn.close()


@_retry_on_locked()
def _forget(filename: str, db_path: str | None = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM datasets WHERE filename = ?", (filename,))
        conn.commit()
    finally:
        conn.close()


def delete_dataset(filename: str, *, actor: dict | None,
                   db_path: str | None = None) -> dict:
    """Hapus berkas dataset dan catatannya. Mengembalikan ``usage`` sebelumnya.

    Izin dibaca ulang dari basis data, bukan dari salinan sesi: akun yang baru
    dinonaktifkan tidak dapat menghapus apa pun.
    """
    from orchestrator.auth_service import _fresh_identity
    from orchestrator.chunked_upload import safe_dataset_name

    name = safe_dataset_name(filename or "")
    target = Path(DATASETS_DIR) / (name or "")
    if name is None or not target.is_file():
        raise DatasetError(f"Dataset {filename} tidak ditemukan.",
                           key="err.dataset_not_found",
                           values={"filename": filename})
    rows = all_rows(db_path)
    if not can_delete(_fresh_identity(actor, db_path), name, rows):
        raise DatasetError("Hanya pengunggahnya atau Research Admin yang "
                           "dapat menghapus dataset ini.",
                           key="err.dataset_delete_denied")
    used = usage(name, db_path)
    if used["active"]:
        raise DatasetError(f"Dataset {name} sedang dipakai {used['active']} "
                           f"eksperimen yang masih berjalan.",
                           key="err.dataset_in_use",
                           values={"filename": name, "count": used["active"]})
    target.unlink()
    _forget(name, db_path)
    logger.info("Dataset %s dihapus oleh %s (%d eksperimen memakainya)",
                name, (actor or {}).get("username"), used["total"])
    return used
