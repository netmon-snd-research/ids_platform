"""
Popup tanda hidup pada run yang sedang berjalan: tambahan, bukan pengganti.

Tampilan pemuatan (bar progres, baris fase, kolom tahapan, tombol Batalkan)
TIDAK diubah. Popup ini hanya muncul ketika ada yang patut diperingatkan:

* ``sunyi``: worker masih hidup tetapi CPU-nya diam >= ``CPU_FLAT_MINUTES``.
  Pilihan: tunggu N menit lagi, atau batalkan run.
* ``tak_terdengar``: worker berhenti mengirim tanda hidup. Pilihan: jalankan
  ulang (kecuali RAM terakhirnya mendekati pagu, karena run yang sama akan
  mati di titik yang sama), atau tandai gagal.

Run yang bekerja normal tidak memunculkan apa pun, berapa pun lama tahapnya.

**Kenapa tidak lewat flag ``ui/components/dialogs``.** Modal lain dibuka oleh
tombol: tombol menulis flag, dan menutup berarti membuang flag. Popup ini
dibuka oleh KEADAAN run, jadi tidak ada flag untuk dibuang; menutupnya
(tombol maupun X/Esc) berarti MENUNDA untuk viewer ini selama N menit.
Penundaan itu disimpan per eksperimen dan per keadaan di ``session_state``:
bila keadaannya berubah (diam menjadi mati), popup langsung muncul lagi.
Karena itu ``on_dismiss`` di sini menulis penundaan, bukan membersihkan flag.

Tidak ada yang dihentikan otomatis: keputusan pemilik platform adalah run
yang sehat tidak pernah dihentikan karena lamanya.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

import streamlit as st

from ui.i18n import t
from workers.heartbeat import (
    LIVE_QUIET, LIVE_SILENT, classify_liveness,
)

#: Pilihan "tunggu N menit lagi", dan bawaannya (juga dipakai saat ditutup X).
SNOOZE_CHOICES = (5, 15, 30, 60)
DEFAULT_SNOOZE_MINUTES = 15

#: RAM terakhir setinggi ini terhadap pagu worker dibaca sebagai "kehabisan
#: memori": menjalankan ulang tidak akan menolong.
OOM_FRACTION = 0.85

SNOOZE_KEY = "_liveness_snooze"        # {experiment_id: {"state", "until"}}
SHOWN_KEY = "_liveness_shown"          # {"eid", "state"} popup yang sedang tampil
MINUTES_KEY = "_liveness_minutes"      # pilihan menit di dalam popup


# ── Keputusan (murni) ─────────────────────────────────────────────────────

def seconds_since(iso: Optional[str], now: float) -> Optional[float]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, now - dt.timestamp())


def liveness_state(status_data: dict, now: float) -> str:
    return classify_liveness(
        str(status_data.get("status") or ""),
        status_data.get("heartbeat"),
        bool(status_data.get("heartbeat_known")),
        running_for_s=seconds_since(status_data.get("started_at"), now),
    )


def popup_state(status_data: dict, snoozes: dict, now: float) -> Optional[str]:
    """Keadaan yang perlu dipopupkan, atau None.

    None bila run baik-baik saja, tidak dapat dinilai, atau keadaan yang SAMA
    sedang ditunda viewer ini.
    """
    state = liveness_state(status_data, now)
    if state not in (LIVE_QUIET, LIVE_SILENT):
        return None
    snooze = (snoozes or {}).get(status_data.get("id"))
    if snooze and snooze.get("state") == state and now < snooze.get("until", 0):
        return None
    return state


def snoozed(snoozes: dict, experiment_id: str, state: str, minutes: int,
            now: float) -> dict:
    """Salinan ``snoozes`` dengan penundaan baru untuk run & keadaan ini."""
    out = dict(snoozes or {})
    out[experiment_id] = {"state": state, "until": now + minutes * 60}
    return out


def worker_mem_limit_mb() -> Optional[float]:
    try:
        return float(os.getenv("WORKER_MEM_LIMIT_MB", "3500"))
    except ValueError:
        return None


def likely_out_of_memory(last: Optional[dict], limit_mb: Optional[float]) -> bool:
    """RAM terakhir yang tercatat mendekati pagu worker."""
    rss = (last or {}).get("rss_mb")
    return (isinstance(rss, (int, float)) and bool(limit_mb)
            and rss >= OOM_FRACTION * limit_mb)


def can_act_on_run(user: Optional[dict], owner: Optional[str]) -> bool:
    """Pemilik run atau Research Admin aktif.

    Run pengunjung (tanpa pemilik) berbagi satu ember, sama seperti pembatas
    beban menghitungnya: pengunjung boleh bertindak atas run pengunjung.
    """
    from orchestrator.auth_service import is_account_active, is_research_admin
    if user and is_account_active(user) and is_research_admin(user):
        return True
    if not owner:
        return not user
    return bool(user) and is_account_active(user) and user.get("username") == owner


def ram_text(mb) -> Optional[str]:
    if not isinstance(mb, (int, float)):
        return None
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"


def duration_text(seconds: Optional[float]) -> str:
    minutes = int((seconds or 0) // 60)
    if minutes >= 60:
        return t("re.live_hours", hours=minutes // 60, minutes=minutes % 60)
    return t("re.live_minutes", minutes=minutes)


def stage_label(status_data: dict) -> Optional[str]:
    progress = status_data.get("celery_progress") or {}
    name = progress.get("stage_name")
    index, total = progress.get("stage_index"), progress.get("stage_total")
    if name and index and total:
        return t("re.live_stage", index=index, total=total, name=name)
    stage = status_data.get("celery_stage")
    return str(stage) if stage and not str(stage).startswith("[DIAG]") else None


# ── Tampilan ──────────────────────────────────────────────────────────────

def _snooze(experiment_id: str, state: str, minutes: int) -> None:
    st.session_state[SNOOZE_KEY] = snoozed(
        st.session_state.get(SNOOZE_KEY), experiment_id, state, minutes,
        time.time())


def _on_dismiss() -> None:
    """X / Esc / klik di luar: sama dengan menunda selama bawaan."""
    shown = st.session_state.get(SHOWN_KEY) or {}
    if shown.get("eid") and shown.get("state"):
        _snooze(shown["eid"], shown["state"], DEFAULT_SNOOZE_MINUTES)


def _stop_watching() -> None:
    st.session_state.pop("polling_experiment_id", None)
    st.session_state.pop(SHOWN_KEY, None)


def _cancel(experiment_id: str) -> Optional[str]:
    """Batalkan lewat jalur yang sama dengan tombol Batalkan. Pesan galat
    atau None bila berhasil."""
    from orchestrator.experiment_service import cancel_experiment
    result = cancel_experiment(experiment_id)
    return None if result.get("success") else result.get("message")


def _rerun(status_data: dict, user: Optional[dict]) -> Optional[str]:
    """Tandai run lama gagal, lalu kirim run baru dengan pipeline, dataset,
    mode, dan parameter yang sama. Pesan galat atau None bila berhasil."""
    from orchestrator.experiment_service import create_and_run_experiment
    from orchestrator.run_mode import changed_keys, locked_params, params_of

    error = _cancel(status_data["id"])
    if error:
        return error
    pipeline_id = status_data.get("pipeline_id")
    used = params_of(status_data)
    overrides = {k: used[k] for k in changed_keys(used, locked_params(pipeline_id))}
    result = create_and_run_experiment(
        status_data.get("dataset_type"), status_data.get("dataset_path"),
        pipeline_id,
        owner=status_data.get("owner"),
        run_mode=status_data.get("run_mode"),
        param_overrides=overrides or None,
    )
    if not result.get("success"):
        return result.get("error") or result.get("message") or "?"
    if result.get("async_mode"):
        st.session_state["polling_experiment_id"] = result["experiment_id"]
    return None


def _body() -> None:
    shown = st.session_state.get(SHOWN_KEY) or {}
    data = shown.get("data") or {}
    state = shown.get("state")
    eid = shown.get("eid")
    now = time.time()
    user = shown.get("user")
    allowed = can_act_on_run(user, data.get("owner"))

    label = stage_label(data)
    running = duration_text(seconds_since(data.get("started_at"), now))

    if label:
        st.caption(t("re.live_running_for_stage", stage=label, duration=running))
    else:
        st.caption(t("re.live_running_for", duration=running))

    if state == LIVE_QUIET:
        hb = data.get("heartbeat") or {}
        text = t("re.live_quiet_body",
                 minutes=int((hb.get("cpu_idle_s") or 0) // 60))
        ram = ram_text(hb.get("rss_mb"))
        if ram:
            text += " " + t("re.live_ram_now", ram=ram)
        st.warning(f"**{t('re.live_quiet_title')}**\n\n{text}")
    else:
        last = data.get("heartbeat_last") or {}
        parts = []
        ts = last.get("ts")
        if isinstance(ts, (int, float)):
            parts.append(t("re.live_silent_since",
                           ago=duration_text(max(0.0, now - ts))))
        if likely_out_of_memory(last, worker_mem_limit_mb()):
            parts.append(t("re.live_silent_oom", ram=ram_text(last.get("rss_mb")),
                           limit=ram_text(worker_mem_limit_mb())))
        else:
            parts.append(t("re.live_silent_body"))
        st.error(f"**{t('re.live_silent_title')}**\n\n" + " ".join(parts))

    if not allowed:
        st.caption(t("re.live_not_owner"))

    minutes = DEFAULT_SNOOZE_MINUTES
    if state == LIVE_QUIET:
        minutes = st.selectbox(
            t("re.live_wait_label"), SNOOZE_CHOICES,
            index=SNOOZE_CHOICES.index(DEFAULT_SNOOZE_MINUTES),
            format_func=lambda m: t("re.live_minutes", minutes=m),
            key=MINUTES_KEY)

    error = None
    cols = st.columns(2)
    if state == LIVE_QUIET:
        if cols[0].button(t("re.live_btn_wait"), key="liveness_wait",
                          use_container_width=True):
            _snooze(eid, state, int(minutes))
            st.rerun()
        if allowed and cols[1].button(t("re.live_btn_cancel"), key="liveness_cancel",
                                      type="primary", use_container_width=True):
            error = _cancel(eid)
            if not error:
                _stop_watching()
                st.rerun()
    else:
        oom = likely_out_of_memory(data.get("heartbeat_last"), worker_mem_limit_mb())
        if allowed and not oom and cols[0].button(
                t("re.live_btn_rerun"), key="liveness_rerun",
                type="primary", use_container_width=True):
            error = _rerun(data, user)
            if not error:
                st.session_state.pop(SHOWN_KEY, None)
                st.rerun()
        if allowed:
            if cols[1].button(t("re.live_btn_mark_failed"), key="liveness_fail",
                              use_container_width=True):
                error = _cancel(eid)
                if not error:
                    _stop_watching()
                    st.rerun()
        elif cols[1].button(t("re.live_btn_close"), key="liveness_close",
                            use_container_width=True):
            _snooze(eid, state, DEFAULT_SNOOZE_MINUTES)
            st.rerun()
    if error:
        st.error(error)


if hasattr(st, "dialog"):
    try:
        _liveness_dialog = st.dialog(t("re.live_dialog_title"),
                                     on_dismiss=_on_dismiss)(_body)
    except TypeError:                       # pragma: no cover - Streamlit < 1.49
        _liveness_dialog = st.dialog(t("re.live_dialog_title"))(_body)
else:                                       # pragma: no cover - Streamlit lama
    def _liveness_dialog():
        with st.expander(t("re.live_dialog_title"), expanded=True):
            _body()


def maybe_show_liveness_popup(status_data: dict) -> None:
    """Munculkan popup bila run ini diam atau workernya berhenti.

    Dipanggil dari alur utama halaman pemantauan, SEBELUM jeda penyegaran,
    pada setiap putaran polling. Tidak menggambar apa pun pada keadaan lain.
    Galat apa pun di sini ditelan: popup tidak boleh merusak halaman pemuatan.
    """
    try:
        now = time.time()
        state = popup_state(status_data, st.session_state.get(SNOOZE_KEY) or {},
                            now)
        if state is None:
            st.session_state.pop(SHOWN_KEY, None)
            return
        from ui.views.login import current_user
        st.session_state[SHOWN_KEY] = {
            "eid": status_data.get("id"), "state": state,
            "data": status_data, "user": current_user(),
        }
        _liveness_dialog()
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Popup tanda hidup tidak dapat ditampilkan", exc_info=True)
