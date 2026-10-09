"""
Pure helpers for the "Progress & Status" dashboard.

No Streamlit / DB / Celery imports here — these functions operate on plain dicts
so they are trivially unit-testable with mocked data. Rendering (Streamlit) and
data fetching (orchestrator) live in ui/views/view_results.py and call these.

Design honesty: ``progress_view`` NEVER fabricates a percentage — it returns
None fields when granular Celery progress is unavailable (e.g. a QUEUED job, or
a broker/worker that is down), so the UI can show status + elapsed only.
"""
from datetime import datetime, timezone

# RUNNING shown before QUEUED; both are "in-flight".
_RUNNING_ORDER = {"RUNNING": 0, "QUEUED": 1}


def _epoch(iso) -> float:
    """Parse an ISO timestamp to epoch seconds; 0.0 on failure. Pure."""
    if not iso:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def visible_runs(experiments, *, device_id: str | None = None,
                 username: str | None = None, sees_all: bool = False) -> list:
    """Saring eksperimen yang boleh dilihat penonton ini. Pure.

    Berlaku untuk SEMUA eksperimen, yang masih berjalan maupun yang sudah
    selesai: riwayat seseorang adalah run miliknya sendiri. Sebuah eksperimen
    tampil bila penontonnya:

    * memulainya dari device (browser) yang sama — ``device_id`` cocok;
    * masuk dengan akun yang memulainya — ``username`` cocok dengan ``owner``,
      jadi riwayatnya ikut ke browser lain begitu ia masuk;
    * Research Admin (``sees_all``), yang perlu melihat semua run untuk
      membatalkan yang macet dan membaca seluruh riwayat.

    Run lama tanpa ``device_id`` dan tanpa ``owner`` karena itu hanya terlihat
    oleh Research Admin.
    """
    rows = list(experiments or [])
    if sees_all:
        return rows
    out = []
    for e in rows:
        if device_id and e.get("device_id") == device_id:
            out.append(e)
        elif username and e.get("owner") == username:
            out.append(e)
    return out


def select_running(experiments) -> list:
    """Return the in-flight experiments (RUNNING then QUEUED), newest-created
    first. Pure — safe to unit-test. Non-in-flight statuses are excluded."""
    items = [e for e in (experiments or []) if e.get("status") in _RUNNING_ORDER]
    return sorted(
        items,
        key=lambda e: (_RUNNING_ORDER[e["status"]], -_epoch(e.get("created_at"))),
    )


def progress_view(status_data) -> dict:
    """Extract display-ready granular progress from a status_data dict (the DB
    row plus an optional ``celery_progress`` payload attached by
    get_experiment_status). Returns:

        {overall_percent: int|None, stage_label: str|None,
         stage_index: int|None, stage_total: int|None}

    Never fabricates: when granular data is missing (QUEUED job, broker/worker
    down, or a session that did not submit the job) the fields are None and the
    caller shows status + elapsed only. Internal "[DIAG]" scaffolding stages are
    suppressed from the label.
    """
    out = {"overall_percent": None, "stage_label": None, "stage_name": None,
           "stage_index": None, "stage_total": None}
    if not isinstance(status_data, dict):
        return out

    cp = status_data.get("celery_progress") or {}
    ov = cp.get("overall_percent")
    if isinstance(ov, (int, float)):
        out["overall_percent"] = int(ov)

    si, tot = cp.get("stage_index"), cp.get("stage_total")
    name = cp.get("stage_name") or status_data.get("celery_stage")
    name = str(name) if name else ""
    if name.startswith("[DIAG]"):
        name = ""

    # Nama fase TELANJANG, tanpa "Fase i/n ·" di depannya. Dipakai tempat yang
    # hanya punya ruang untuk satu kata — kartu sidebar — sementara
    # `stage_label` yang lengkap tetap dipakai halaman yang memang melaporkan
    # posisi di antara seluruh fase.
    out["stage_name"] = name or None

    if isinstance(si, int) and isinstance(tot, int) and tot > 0 and name:
        out["stage_index"], out["stage_total"] = si, tot
        out["stage_label"] = f"Fase {si}/{tot} · {name}"
    elif name:
        out["stage_label"] = name
    return out


def elapsed_seconds(started_at, now_epoch=None):
    """Seconds since ``started_at`` (from the DB), or None if unknown. Pure
    (pass now_epoch in tests for determinism)."""
    s = _epoch(started_at)
    if not s:
        return None
    now = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    return max(0.0, now - s)


def format_elapsed(seconds) -> str:
    """Compact human elapsed string; '-' when unknown."""
    if seconds is None:
        return "-"
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"
