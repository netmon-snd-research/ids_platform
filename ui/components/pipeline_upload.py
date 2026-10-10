"""
Logika unggah & validasi script pipeline — MURNI, tanpa Streamlit.

Alur: bytes unggahan → baca sebagai TEKS → serahkan ke validator statis
(`orchestrator/pipeline_validator.py`) → payload siap-tampil (verdict, penyebab,
rincian per kategori), agregasi tingkat PAKET untuk unggahan banyak berkas, dan
generator cuplikan entri registry.

Yang me-render adalah halaman `ui/views/contribute.py`; modul ini sengaja bebas
dari Streamlit supaya seluruh aturannya dapat diuji tanpa runtime UI.

⚠️ SECURITY. Berkas yang diunggah adalah UNTRUSTED. Modul ini:
  - TIDAK pernah meng-import, `exec`, `eval`, atau menjalankan berkas itu;
    satu-satunya pemrosesan adalah decode teks + `ast.parse` di dalam validator,
  - TIDAK pernah menulis ke `pipelines/` atau direktori mana pun yang berada di
    jalur import platform. Penyimpanan (opsional, atas permintaan pengguna)
    hanya ke `storage/uploaded_pipelines/` — `storage/` bukan package Python
    (tanpa `__init__.py`) dan tidak pernah diimpor oleh kode platform,
  - TIDAK menyentuh registry. Lolos validasi ≠ aktif: pendaftaran pipeline tetap
    manual lewat kode + git.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from config.settings import STORAGE_DIR
from contracts.dataset_schemas import supported_datasets
# `_find_pipeline_class` sengaja dipakai ulang (walau privat) agar "kelas mana
# yang dianggap pipeline" punya SATU definisi — sama persis dengan yang dipakai
# validator Tahap 1. Menyalin logikanya berisiko menyimpang diam-diam.
from orchestrator.auth_service import require_upload
from orchestrator.pipeline_validator import (
    _find_pipeline_class, validate_pipeline_source,
)

# Script pipeline itu kecil (yang terbesar di repo ini < 30 KB). Batas longgar
# tetapi tegas, supaya berkas raksasa tidak pernah dibaca ke memori.
MAX_UPLOAD_BYTES = 1_000_000          # 1 MB

# Area staging: DI LUAR jalur import platform (storage/ bukan package Python).
STAGING_DIR = Path(STORAGE_DIR) / "uploaded_pipelines"

# Nama check dari validator Tahap 1 yang termasuk kategori KEAMANAN. Sisanya
# dianggap STRUKTUR. Satu tempat, supaya pengelompokan tampilan tidak tersebar.
SECURITY_CHECK_NAMES = frozenset({
    "import terlarang",
    "import di luar daftar",
    "pemanggilan terlarang",
    "refleksi atribut",
    "penulisan berkas",
    "atribut dunder terlarang",
})

GROUP_SECURITY = "Keamanan"
GROUP_STRUCTURE = "Struktur"

# Status "fail" dari validator (dipakai untuk menyaring temuan yang memblokir).
FAIL_STATUS = "fail"

# Urutan kepentingan untuk memilih SATU penyebab utama saat berkas tidak valid.
_CAUSE_PRIORITY = (
    "sintaks Python",
    "import terlarang",
    "pemanggilan terlarang",
    "atribut dunder terlarang",
    "penulisan berkas",
    "kelas pipeline",
    "method `run`",
    "method `get_info`",
    "get_info() mengembalikan dict",
)

# Nama berkas yang aman untuk staging: basename saja, karakter terbatas, .py.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+\.py$")


def classify_check(name: str) -> str:
    """Kelompok tampilan sebuah check: Keamanan atau Struktur."""
    return GROUP_SECURITY if name in SECURITY_CHECK_NAMES else GROUP_STRUCTURE


def _primary_cause(checks: list[dict]) -> str:
    """Penyebab utama dalam satu-dua kalimat, dari kegagalan paling menentukan."""
    failures = [c for c in checks if c["status"] == "fail"]
    if not failures:
        return ""
    chosen = None
    for name in _CAUSE_PRIORITY:
        match = [c for c in failures if c["name"] == name]
        if match:
            chosen = match[0]
            same = len(match)
            break
    else:
        chosen, same = failures[0], 1

    text = chosen["message"]
    if chosen.get("line"):
        text += f" (baris {chosen['line']})"
    if same > 1:
        text += f" Ditemukan {same} temuan sejenis lainnya."
    if len(failures) > same:
        text += f" Total {len(failures)} masalah · lihat rincian."
    return text


def decode_upload(data: bytes) -> tuple[str | None, str | None]:
    """(source, error). Hanya decode teks — tidak ada kode yang dijalankan."""
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return data.decode(encoding), None
        except UnicodeDecodeError:
            continue
    return None, ("Berkas bukan teks UTF-8 yang valid. Script pipeline harus "
                  "berupa berkas Python biasa (UTF-8).")


def review_upload(data: bytes, filename: str,
                  package_modules: tuple = ()) -> dict:
    """Lapisan murni: bytes unggahan → payload siap-tampil. Tidak pernah raise.

    Mengembalikan::

        {ok, error, filename, size, verdict: "valid"|"invalid"|"unreadable",
         cause, report: <ValidationReport.to_dict()|None>,
         groups: {"Struktur": [...], "Keamanan": [...]}}
    """
    payload: dict = {
        "ok": False, "error": None, "filename": filename or "",
        "size": len(data or b""), "verdict": "unreadable", "cause": "",
        "report": None, "groups": {GROUP_STRUCTURE: [], GROUP_SECURITY: []},
    }

    if not filename or not filename.lower().endswith(".py"):
        payload["error"] = "Hanya berkas `.py` yang dapat divalidasi."
        return payload
    if not data:
        payload["error"] = "Berkas kosong: tidak ada kode untuk divalidasi."
        return payload
    if len(data) > MAX_UPLOAD_BYTES:
        payload["error"] = (
            f"Berkas terlalu besar ({len(data):,} byte). Batas "
            f"{MAX_UPLOAD_BYTES:,} byte · script pipeline seharusnya jauh lebih "
            f"kecil dari itu.")
        return payload

    source, error = decode_upload(data)
    if source is None:
        payload["error"] = error
        return payload
    if not source.strip():
        payload["error"] = "Berkas kosong: tidak ada kode untuk divalidasi."
        return payload

    # Satu-satunya pemrosesan kode: validator STATIS (ast.parse). Tidak diimpor,
    # tidak dieksekusi.
    report = validate_pipeline_source(
        source, filename, package_modules).to_dict()

    payload["ok"] = True
    payload["report"] = report
    payload["verdict"] = "valid" if report["valid"] else "invalid"
    payload["cause"] = _primary_cause(report["checks"])
    for check in report["checks"]:
        payload["groups"][classify_check(check["name"])].append(check)
    payload["source"] = source
    return payload


# ── Metadata untuk cuplikan entri registry (STATIS, via AST) ──────────────
# Registry TETAP statis dan aktivasi TETAP manual: yang dihasilkan di sini
# hanyalah TEKS cuplikan untuk disalin pengembang ke config/pipeline_registry.py
# lewat git. Tidak ada berkas yang ditulis dan tidak ada registrasi runtime.

# Awalan placeholder — sengaja mencolok supaya tidak mungkin lolos ke registry
# tanpa diisi. Nilai yang tidak terbaca statis TIDAK PERNAH ditebak.
PLACEHOLDER = "PERLU_DIISI"

# Kunci entri registry, mengikuti bentuk nyata di config/pipeline_registry.py.
REGISTRY_FIELDS = ("dataset_type", "name", "paper", "algorithm", "class", "stages")


def _literal_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _get_info_literals(cls: ast.ClassDef) -> tuple[dict[str, str], list[str]]:
    """({kunci: nilai} dari dict literal yang dikembalikan get_info, semua string
    literal yang muncul di get_info). Hanya membaca; tidak mengeksekusi."""
    pairs: dict[str, str] = {}
    strings: list[str] = []
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "get_info":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                strings.append(sub.value)
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                for key, value in zip(sub.value.keys, sub.value.values):
                    k, v = _literal_str(key), _literal_str(value)
                    if k and v is not None:
                        pairs[k] = v
    return pairs, strings


def _emitted_stages(cls: ast.ClassDef) -> list[str]:
    """Label stage literal dari panggilan `_emit_progress(..., "Label")`, dalam
    urutan kemunculan di source. Ini nilai NYATA dari berkas, bukan karangan —
    tetapi kelengkapan/urutannya tetap perlu diperiksa pengembang."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(cls):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "_emit_progress":
            continue
        for arg in node.args:
            label = _literal_str(arg)
            if label:
                found.append((node.lineno, label))
    ordered: list[str] = []
    for _, label in sorted(found, key=lambda t: t[0]):
        if label not in ordered:
            ordered.append(label)
    return ordered


def extract_registry_metadata(source: str, filename: str = "") -> dict:
    """Metadata untuk cuplikan registry, DIBACA STATIS dari source.

    Yang tidak dapat ditentukan dari teks kode dibiarkan ``None`` (nanti menjadi
    placeholder di cuplikan) — tidak pernah ditebak. Tidak pernah raise.

    Mengembalikan ``{class_name, module_stem, dataset_type, algorithm, paper,
    stages, hints}``; ``hints`` berisi petunjuk yang ditemukan tetapi TIDAK
    cukup pasti untuk dijadikan nilai.
    """
    meta: dict = {
        "class_name": None, "module_stem": Path(filename or "").stem or None,
        "dataset_type": None, "algorithm": None, "paper": None,
        "stages": [], "hints": [],
    }
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return meta

    cls, _direct = _find_pipeline_class(tree)
    if cls is None:
        return meta
    meta["class_name"] = cls.name

    pairs, strings = _get_info_literals(cls)
    # Hanya nilai LITERAL yang dipakai. `paper` pada pipeline repo berupa
    # panggilan research_paper_credit(...) → bukan literal → tetap placeholder.
    for key in ("dataset_type", "algorithm", "paper"):
        if key in pairs:
            meta[key] = pairs[key]

    # dataset_type tidak tersedia sebagai kunci literal: bila di get_info ada
    # string yang PERSIS sama dengan salah satu dataset_type terdaftar (mis.
    # argumen research_paper_credit("HIKARI2021")), tawarkan sebagai PETUNJUK —
    # bukan sebagai nilai, karena kaitannya tidak pasti secara statis.
    if not meta["dataset_type"]:
        known = set(supported_datasets())
        candidates = sorted({s for s in strings if s in known})
        if len(candidates) == 1:
            meta["hints"].append(
                f"Terdeteksi literal `{candidates[0]}` di dalam `get_info()` · "
                f"kemungkinan dataset_type-nya, tetapi belum dipastikan; isi manual."
            )

    meta["stages"] = _emitted_stages(cls)
    return meta


def _py_str(value: str) -> str:
    """Literal string Python yang aman untuk ditempel ke dalam cuplikan."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_registry_snippet(meta: dict) -> str:
    """Cuplikan entri PIPELINE_REGISTRY siap-tempel (TEKS saja).

    Mengikuti bentuk entri nyata di config/pipeline_registry.py (6 kunci:
    dataset_type, name, paper, algorithm, class, stages). Nilai yang tidak
    terbaca statis muncul sebagai placeholder ``PERLU_DIISI_<kunci>``.
    """
    cls = meta.get("class_name") or f"{PLACEHOLDER}_NamaKelas"
    stem = meta.get("module_stem") or f"{PLACEHOLDER}_nama_modul"
    dtype = meta.get("dataset_type")
    prefix = dtype.lower() if dtype else f"{PLACEHOLDER}_paket"
    pipeline_id = f"{prefix}.{stem}"

    def field(key: str) -> str:
        value = meta.get(key)
        return _py_str(value) if value else _py_str(f"{PLACEHOLDER}_{key}")

    if meta.get("stages"):
        stages_lines = "\n".join(f"            {_py_str(s)}," for s in meta["stages"])
        stages_block = ("[   # dari _emit_progress() pada berkas: periksa urutan "
                        "& kelengkapannya\n" + stages_lines + "\n        ],")
    else:
        stages_block = (f"[{_py_str(f'{PLACEHOLDER}_stage_1')}],  "
                        f"# label progres, urut sesuai _emit_progress()")

    lines = [
        f"# 1) Import kelasnya di bagian atas config/pipeline_registry.py:",
        f"from pipelines.{PLACEHOLDER}_subdirektori.{stem} import {cls}",
        "",
        "# 2) Tambahkan entri ini ke dict PIPELINE_REGISTRY:",
        f'    "{pipeline_id}": {{',
        f'        "dataset_type": {field("dataset_type")},',
        f'        "name": {field("name")},',
        f'        "paper": {field("paper")},',
        f'        "algorithm": {field("algorithm")},',
        f'        "class": {cls},',
        f'        "stages": {stages_block}',
        "    },",
    ]
    if meta.get("hints"):
        lines.append("")
        lines += [f"# Petunjuk: {h}" for h in meta["hints"]]
    return "\n".join(lines)


# ── Validasi PAKET (beberapa berkas sekaligus) ────────────────────────────
# Aturan paket, ditegakkan di lapisan murni ini supaya dapat diuji tanpa UI:
#   1. SETIAP berkas divalidasi penuh — termasuk berkas pendukung, karena
#      berkas pendukung ikut dieksekusi saat pipeline berjalan nanti. Kegagalan
#      keamanan di berkas mana pun menggagalkan seluruh paket.
#   2. Harus ada TEPAT SATU entry point, yaitu berkas yang memuat kelas dengan
#      pewarisan LANGSUNG dari BasePipeline (definisi tak ambigu dari
#      pipelines/base.py). Nol → tidak jelas mana pipeline-nya; lebih dari satu
#      → ambigu saat didaftarkan.

ROLE_ENTRY = "entry point"
ROLE_SUPPORT = "pendukung"

# Pemeriksaan yang hanya masuk akal untuk ENTRY POINT. Berkas pendukung bukan
# pipeline, jadi "tidak ada kelas turunan BasePipeline" pada helper bukan cacat
# — tetapi seluruh aturan KEAMANAN tetap berlaku penuh untuknya, karena berkas
# pendukung ikut dieksekusi saat pipeline berjalan.
ENTRY_ONLY_CHECKS = frozenset({
    "kelas pipeline",
    "method `run`",
    "method `get_info`",
    "signature run()",
    "get_info() mengembalikan dict",
})


def has_direct_pipeline_class(source: str) -> bool:
    """True bila source memuat kelas yang mewarisi ``BasePipeline`` LANGSUNG.

    Statis (ast.parse) dan memakai ulang deteksi kelas milik validator, jadi
    "kelas pipeline" punya satu definisi di seluruh platform.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return False
    _cls, direct = _find_pipeline_class(tree)
    return bool(direct)


def review_package(files: list[tuple[str, bytes]],
                   descriptions: dict[str, str] | None = None) -> dict:
    """Validasi SEMUA berkas lalu terapkan aturan tingkat paket.

    ``files`` adalah [(nama, bytes)]. Mengembalikan::

        {valid, files: [...], entry_points: [nama], n_problem_files,
         cause, summary}

    **Satu paket boleh memuat BANYAK entry point.** Sebuah research pipeline
    kontribusi berdiri sendiri dan wajar membawa beberapa algoritma sekaligus —
    persis seperti keluarga bawaan (HIKARI2021 punya enam). Sebelumnya paket
    dengan lebih dari satu turunan ``BasePipeline`` DITOLAK dan pengunggah
    disuruh "tentukan satu entry point saja", sehingga satu pengajuan tidak
    pernah bisa menjadi lebih dari satu algoritma.

    Yang TIDAK berubah: setiap berkas entry point tetap diperiksa penuh
    (struktur + keamanan), dan paket tanpa satu pun entry point tetap ditolak.

    Setiap elemen ``files`` adalah payload ``review_upload`` + ``role`` +
    ``description``. Tidak pernah menjalankan berkas apa pun.
    """
    descriptions = descriptions or {}
    reviewed: list[dict] = []
    entry_points: list[str] = []

    # Nama modul berkas LAIN di paket ini. Sebuah paket multi-berkas wajar
    # mengimpor pendukungnya sendiri, dan impor itu bukan pustaka asing:
    # berkasnya ikut diunggah dan ikut diperiksa di perulangan yang sama.
    sepaket = tuple(str(n).rsplit("/", 1)[-1][:-3] for n, _ in files
                    if str(n).lower().endswith(".py"))

    for name, data in files:
        saudara = tuple(m for m in sepaket
                        if m != str(name).rsplit("/", 1)[-1][:-3])
        item = review_upload(data, name, saudara)
        item["description"] = (descriptions.get(name) or "").strip()
        source = item.get("source") or ""
        is_entry = bool(source) and has_direct_pipeline_class(source)
        item["role"] = ROLE_ENTRY if is_entry else ROLE_SUPPORT
        if is_entry:
            entry_points.append(name)

        checks = (item.get("report") or {}).get("checks", [])
        if is_entry:
            blocking = [c for c in checks if c["status"] == FAIL_STATUS]
        else:
            # Pendukung: hanya kegagalan KEAMANAN yang menggagalkan paket.
            # Pemeriksaan khas entry point dibuang dari tampilan agar tidak
            # terbaca sebagai cacat pada berkas yang memang bukan pipeline.
            blocking = [c for c in checks
                        if c["status"] == FAIL_STATUS and c["name"] not in ENTRY_ONLY_CHECKS]
            item["groups"][GROUP_STRUCTURE] = [
                c for c in item["groups"][GROUP_STRUCTURE]
                if c["name"] not in ENTRY_ONLY_CHECKS
            ]
            # …dan dari LAPORANNYA. Panel peninjauan menghitung serta
            # menampilkan `report["checks"]`, bukan `groups`, sehingga
            # pembuangan di atas tidak pernah sampai ke layar: berkas
            # pendukung tetap memperlihatkan "✗ kelas pipeline: tidak
            # ditemukan kelas turunan BasePipeline" tepat di bawah kalimat
            # yang menyatakan kontrak pipeline TIDAK berlaku baginya.
            laporan = item.get("report")
            if isinstance(laporan, dict) and laporan.get("checks"):
                laporan["checks"] = [
                    c for c in laporan["checks"]
                    if c.get("name") not in ENTRY_ONLY_CHECKS
                ]
        item["blocking_failures"] = blocking
        item["package_ok"] = bool(item["ok"]) and not blocking
        reviewed.append(item)

    problems = [f for f in reviewed if not f["package_ok"]]
    n_problem = len(problems)

    if not reviewed:
        cause = "Belum ada berkas yang diunggah."
    elif len(entry_points) == 0:
        cause = ("Tidak ada berkas yang memuat kelas turunan `BasePipeline`. "
                 "Tepat satu berkas harus menjadi entry point pipeline.")
    elif n_problem:
        first = problems[0]
        blocking = first.get("blocking_failures") or []
        detail = (first.get("error")
                  or (blocking[0]["message"] if blocking else None)
                  or first.get("cause")
                  or "ada pemeriksaan yang gagal")
        if blocking and blocking[0].get("line"):
            detail += f" (baris {blocking[0]['line']})"
        cause = f"`{first['filename']}`: {detail}"
        if n_problem > 1:
            cause += f" ({n_problem} berkas bermasalah · lihat rincian per berkas.)"
    else:
        cause = ""

    # Sah bila ADA entry point (satu atau lebih) dan tidak ada berkas
    # bermasalah. Batas atasnya dilepas: banyak entry point berarti
    # banyak algoritma dalam satu research pipeline, bukan cacat.
    valid = bool(reviewed) and len(entry_points) >= 1 and n_problem == 0
    summary = (f"Valid · {len(reviewed)} berkas lolos, "
               f"{len(entry_points)} algoritma."
               if valid else
               f"Tidak valid · {n_problem} dari {len(reviewed)} berkas bermasalah."
               if n_problem else "Tidak valid: aturan entry point belum terpenuhi.")

    return {
        "valid": valid,
        "files": reviewed,
        "entry_points": entry_points,
        "n_problem_files": n_problem,
        "cause": cause,
        "summary": summary,
    }


# Kunci formulir metadata → kunci meta yang dipakai cuplikan registry.
FORM_FIELDS = ("name", "dataset_type", "algorithm", "paper")


def merge_form_metadata(meta: dict, form: dict | None) -> dict:
    """Gabungkan metadata formulir ke metadata hasil pembacaan statis.

    Isian formulir MENANG karena berasal dari manusia yang mendaftarkan
    pipeline; field yang dikosongkan tetap memakai nilai statis (atau menjadi
    placeholder di cuplikan). Tidak ada nilai yang ditebak platform.
    """
    merged = dict(meta or {})
    for key in FORM_FIELDS:
        value = (form or {}).get(key)
        if isinstance(value, str) and value.strip():
            merged[key] = value.strip()
    return merged


def safe_staging_name(filename: str) -> str | None:
    """Nama berkas yang aman untuk staging, atau None bila tidak layak.

    Nama yang memuat komponen direktori DITOLAK (bukan dipotong diam-diam):
    peramban selalu mengirim basename, jadi separator pada nama unggahan adalah
    anomali yang layak ditolak secara eksplisit. Selebihnya hanya
    huruf/angka/`._-` dan wajib berakhiran `.py`, sehingga tidak ada unggahan
    yang bisa menulis ke luar STAGING_DIR.
    """
    name = filename or ""
    if "/" in name or "\\" in name or name != Path(name).name:
        return None
    if name in ("", ".", "..") or not _SAFE_NAME.match(name):
        return None
    return name


def save_to_staging(source: str, filename: str, *, user: dict | None) -> Path:
    """Simpan source ke area staging untuk ditinjau manusia.

    Menulis TEKS yang barusan divalidasi (bukan bytes mentah) ke
    ``storage/uploaded_pipelines/``. Lokasi ini tidak pernah diimpor platform,
    jadi menyimpan di sini TIDAK mengaktifkan apa pun.

    ``user`` WAJIB (keyword-only, tanpa default) supaya pemanggil tidak bisa
    lupa: izinnya diperiksa DI SINI, bukan hanya dengan menyembunyikan tombol.
    Raise PermissionDenied bila tidak berhak, ValueError bila nama tidak aman.
    """
    require_upload(user)
    safe = safe_staging_name(filename)
    if safe is None:
        raise ValueError(f"Nama berkas tidak aman untuk staging: {filename!r}")
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    target = STAGING_DIR / safe
    target.write_text(source, encoding="utf-8")
    return target


# ── Notebook (.ipynb) ─────────────────────────────────────────────────────
# Notebook diterima sebagai SUMBER, bukan sebagai format yang disimpan. Ia
# diubah menjadi teks `.py` di sini, lalu menempuh jalur yang sama persis
# dengan berkas `.py`: validasi AST, hash, penyimpanan, dan pemuat paket — yang
# memang hanya dapat mengimpor `.py`. Satu jalur berarti satu set aturan
# keamanan; jalur kedua khusus notebook akan menjadi tempat aturan tertinggal.
#
# ⚠️ Konversinya STATIS: JSON notebook dibaca, sel kode digabung. Tidak ada
# kernel, tidak ada `nbconvert`, tidak ada sel yang dijalankan. Output dan sel
# markdown dibuang, karena output dapat berukuran megabyte dan tidak pernah
# dibutuhkan pipeline.

NOTEBOOK_SUFFIX = ".ipynb"

#: Notebook membawa output (gambar base64, tabel), jadi jauh lebih besar dari
#: kodenya. Batas ini untuk JSON mentahnya; hasil konversinya tetap tunduk pada
#: MAX_UPLOAD_BYTES seperti berkas `.py` lain.
MAX_NOTEBOOK_BYTES = 20_000_000       # 20 MB

#: Baris khas IPython yang bukan Python: magic (`%matplotlib inline`,
#: `%%time`) dan perintah shell (`!pip install …`, `files = !ls`).
_IPY_LINE = re.compile(r"^\s*(%|!)|^\s*[A-Za-z_][\w.]*\s*=\s*[!%]")

#: Magic sel yang isinya tetap Python biasa; hanya baris magic-nya yang
#: dijadikan komentar.
_WRAPPING_CELL_MAGICS = frozenset({"time", "timeit", "capture", "prun"})

#: Pernyataan tingkat atas yang hanya MENDEFINISIKAN sesuatu. Sisanya dijalankan
#: saat modul dimuat — pada notebook, itu biasanya seluruh pelatihan.
_DEFINING_NODES = (ast.Import, ast.ImportFrom, ast.FunctionDef,
                   ast.AsyncFunctionDef, ast.ClassDef)


class NotebookError(ValueError):
    """Berkas `.ipynb` tidak dapat dibaca sebagai notebook."""


def _cell_text(cell: dict) -> str:
    raw = cell.get("source", cell.get("input", ""))
    return "".join(raw) if isinstance(raw, list) else str(raw or "")


def _notebook_cells(nb: dict) -> list[dict]:
    if isinstance(nb.get("cells"), list):                    # nbformat 4
        return nb["cells"]
    sheets = nb.get("worksheets")                            # nbformat 3
    if isinstance(sheets, list) and sheets and isinstance(sheets[0], dict):
        return sheets[0].get("cells") or []
    raise NotebookError("Struktur notebook tidak dikenali (tidak ada daftar sel).")


def _runs_at_import(node: ast.AST) -> bool:
    """Apakah pernyataan tingkat atas ini MENJALANKAN sesuatu saat diimpor."""
    if isinstance(node, _DEFINING_NODES):
        return False
    if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)):
        return False                                         # docstring
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        # Konstanta (`TEST_SIZE = 0.3`) sah; `df = pd.read_csv(...)` tidak.
        return any(isinstance(n, ast.Call) for n in ast.walk(node))
    return True


def notebook_to_source(data: bytes, filename: str = "") -> dict:
    """Ubah bytes `.ipynb` menjadi source `.py`. MURNI dan STATIS.

    Mengembalikan ``{name, source, code_cells, disabled_lines, runs_at_import,
    notes}``. ``name`` adalah nama berkas `.py` hasilnya (stem yang sama).
    Baris magic/shell IPython dijadikan komentar, bukan dibuang, supaya
    kontributor dapat melihat apa yang tidak ikut dijalankan.

    Raise ``NotebookError`` bila berkasnya bukan notebook yang dapat dibaca.
    """
    import json

    if len(data) > MAX_NOTEBOOK_BYTES:
        raise NotebookError(
            f"Notebook berukuran {len(data):,} byte, melebihi batas "
            f"{MAX_NOTEBOOK_BYTES:,} byte. Hapus output sel lalu unggah ulang.")
    try:
        nb = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise NotebookError(f"Berkas bukan notebook JSON yang valid: {e}") from e
    if not isinstance(nb, dict):
        raise NotebookError("Berkas bukan notebook JSON yang valid.")

    stem = Path(filename or "notebook.ipynb").stem
    code_cells = [c for c in _notebook_cells(nb)
                  if isinstance(c, dict) and c.get("cell_type") == "code"]

    disabled = 0
    parts = [f"# Dikonversi dari {stem}{NOTEBOOK_SUFFIX}: {len(code_cells)} sel kode.\n"
             f"# Sel markdown dan output tidak disertakan.\n"]
    for i, cell in enumerate(code_cells, 1):
        lines = _cell_text(cell).splitlines()
        if not any(line.strip() for line in lines):
            continue
        # Magic sel yang hanya MEMBUNGKUS (`%%time`) menyisakan isi Python
        # yang sah; yang MENGGANTI bahasa sel (`%%bash`, `%%writefile`)
        # menjadikan seluruh isinya bukan Python, jadi ikut dinonaktifkan.
        first = lines[0].strip() if lines else ""
        cell_magic = (first.startswith("%%")
                      and first[2:].split(" ")[0] not in _WRAPPING_CELL_MAGICS)
        out = []
        for line in lines:
            if cell_magic or _IPY_LINE.match(line):
                out.append(f"# [ipynb, dinonaktifkan] {line}")
                disabled += 1
            else:
                out.append(line)
        parts.append(f"\n# %% sel {i}\n" + "\n".join(out) + "\n")
    source = "".join(parts)

    runs = 0
    try:
        runs = sum(_runs_at_import(n) for n in ast.parse(source).body)
    except (SyntaxError, ValueError, RecursionError):
        pass        # validator yang melaporkan sintaksnya, dengan nomor baris

    notes = []
    if disabled:
        notes.append(f"{disabled} baris magic/shell IPython dijadikan komentar "
                     f"dan tidak akan dijalankan.")
    if runs:
        notes.append(f"{runs} pernyataan tingkat atas akan DIJALANKAN saat modul "
                     f"dimuat (misalnya membaca data atau melatih model). Pindahkan "
                     f"ke dalam `run()`: pipeline hanya boleh bekerja di sana.")
    return {"name": f"{stem}.py", "source": source, "code_cells": len(code_cells),
            "disabled_lines": disabled, "runs_at_import": runs, "notes": notes}


class ConvertedUpload:
    """Hasil konversi yang berperilaku seperti berkas unggahan `.py`.

    Halaman unggah membaca ``.name`` dan ``.getvalue()`` di banyak tempat
    (deteksi algoritma, fase, ukuran, validasi paket, pengajuan). Membungkus
    hasilnya di sini membuat semua tempat itu menerima notebook tanpa satu
    pun diubah.
    """

    def __init__(self, converted: dict, original_name: str):
        self.name = converted["name"]
        self.original_name = original_name
        self.notes = list(converted.get("notes") or [])
        self.code_cells = converted.get("code_cells", 0)
        self._data = converted["source"].encode("utf-8")

    def getvalue(self) -> bytes:
        return self._data


def normalize_pipeline_uploads(uploaded) -> tuple[list, list[str]]:
    """Ubah setiap `.ipynb` di antara unggahan menjadi ``ConvertedUpload``.

    Mengembalikan ``(berkas, galat)``. Berkas `.py` dilewatkan apa adanya.
    Notebook yang tidak terbaca, atau yang namanya bertabrakan dengan berkas
    `.py` di paket yang sama, tidak ikut dan dijelaskan di ``galat``.
    """
    files, errors = [], []
    names = {f.name for f in (uploaded or [])
             if not str(f.name).lower().endswith(NOTEBOOK_SUFFIX)}
    for f in uploaded or []:
        if not str(f.name).lower().endswith(NOTEBOOK_SUFFIX):
            files.append(f)
            continue
        try:
            converted = notebook_to_source(f.getvalue(), f.name)
        except NotebookError as e:
            errors.append(f"`{f.name}`: {e}")
            continue
        if converted["name"] in names:
            errors.append(f"`{f.name}` menjadi `{converted['name']}`, yang juga "
                          f"diunggah sebagai berkas `.py`. Unggah salah satunya saja.")
            continue
        names.add(converted["name"])
        files.append(ConvertedUpload(converted, f.name))
    return files, errors
