"""
Identitas dataset menurut ISINYA, bukan namanya.

Dua berkas dianggap dataset yang sama bila SHA-256 isinya sama, apa pun
namanya. Sebaliknya, nama yang kebetulan sama dengan isi berbeda adalah
dataset yang berbeda; ia disimpan dengan nama lain (``available_name``),
karena platform tidak pernah menimpa dataset yang sudah ada.

Hash dihitung seperlunya saja. Isi yang sama pasti berukuran sama, jadi hanya
dataset yang ukurannya PERSIS sama dengan berkas baru yang perlu di-hash, dan
biasanya tidak ada satu pun. Hash dataset lama disimpan di memori proses,
dikunci (path, ukuran, mtime), sehingga berkas yang berubah dihitung ulang.
"""
from __future__ import annotations

import threading
from pathlib import Path

from config.settings import DATASETS_DIR
from utils.hashing import sha256_file

#: Ekstensi dataset yang ikut dibandingkan. Sama dengan yang diterima unggahan.
DATASET_EXTENSIONS = (".csv", ".ndjson", ".jsonl", ".json")

_lock = threading.Lock()
#: (path, ukuran, mtime_ns) -> sha256
_hashes: dict[tuple[str, int, int], str] = {}


def file_hash(path: str | Path) -> str:
    """SHA-256 isi berkas, memakai cache selama berkasnya tidak berubah."""
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), st.st_size, st.st_mtime_ns)
    with _lock:
        known = _hashes.get(key)
    if known is not None:
        return known
    digest = sha256_file(str(p), chunk_size=1024 * 1024)
    with _lock:
        _hashes[key] = digest
    return digest


def find_same_content(path: str | Path,
                      datasets_dir: str | Path | None = None, *,
                      only=None) -> Path | None:
    """Dataset di ``storage/datasets/`` yang isinya identik dengan ``path``.

    Mengembalikan path dataset itu, atau None bila tidak ada. Berkas ``path``
    sendiri tidak pernah dianggap duplikat dirinya.

    ``only(path) -> bool`` membatasi pembandingnya, mis. hanya dataset yang
    boleh dilihat pengunggah: dataset privat orang lain tidak dapat ia pakai,
    jadi menolak salinannya membuat ia buntu.
    """
    p = Path(path)
    root = Path(datasets_dir or DATASETS_DIR)
    if not root.is_dir():
        return None
    size = p.stat().st_size
    me = p.resolve()
    candidates = []
    for f in sorted(root.iterdir()):
        if f.suffix.lower() not in DATASET_EXTENSIONS or not f.is_file():
            continue
        if only is not None and not only(f):
            continue
        try:
            if f.stat().st_size != size or f.resolve() == me:
                continue
        except OSError:                       # pragma: no cover - berkas hilang
            continue
        candidates.append(f)
    if not candidates:
        return None
    mine = file_hash(p)
    for f in candidates:
        try:
            if file_hash(f) == mine:
                return f
        except OSError:                       # pragma: no cover - berkas hilang
            continue
    return None


def available_name(filename: str, datasets_dir: str | Path | None = None) -> str:
    """``filename`` bila belum dipakai, selain itu ``<nama>_2.<ext>``, ``_3``, …"""
    root = Path(datasets_dir or DATASETS_DIR)
    if not (root / filename).exists():
        return filename
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 2
    while (root / f"{stem}_{n}{suffix}").exists():
        n += 1
    return f"{stem}_{n}{suffix}"
