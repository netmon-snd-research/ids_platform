"""
PDF report generator in the style of a journal preprint (ReportLab).

Design goal: read like an Elsevier-style preprint. A full-width article head
(title, byline, an "info" column with history and keywords beside the
abstract), then a two-column body with numbered sections ("1.", "2.1."),
booktabs tables captioned above ("Tabel 1"), figures captioned below
("Gambar 1:"), a running head on every page and a "Halaman x dari y" footer.
Monochrome text; colour only inside the charts.

Every number and interpretive sentence is computed from the experiment's
ACTUAL data (metrics.json / extra_info / metadata) at render time, never
fabricated and never recomputed differently. Absent fields are skipped with an
honest note.

Family-aware semantics (stated explicitly as a methodological note, never
conflated): HIKARI metrics are weighted-average over ground-truth labels;
EVE-cbr metrics are attack-class on the natural holdout (labels derived from
Suricata alerts). Operational counts (detected / missed / false alarm) are
ALWAYS derived from the confusion matrix (attack = positive class).

Structure:
  Article head   title, byline, info column (history + keywords), Abstrak
  1. Konfigurasi Eksperimen
  2. Hasil dan Metrik            (Tabel: metrik utama + catatan semantik)
  3. Interpretasi Keamanan       (Tabel kuadran + Gambar CM + sorotan)
  4. Analisis Metrik             (verdict per metrik, dihitung)
  5. Fitur Berpengaruh           (Gambar + makna per-fitur; defensif)
  6. Diagnostik                  (ROC, learning curve / dual-holdout, per-kelas)
  7. Catatan Metodologis
  8. Reproducibility

Signature and return type (bytes) are preserved exactly so the call sites in
ui/ keep working. ``_confusion_breakdown`` is public-by-use (imported by
ui/components/result_views.py and tests) and kept intact.

Rules: No database access. No UI imports at module level. Reads provided data
only. No em dashes anywhere in the rendered text.
"""
import io
from xml.sax.saxutils import escape

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as _canvas

from orchestrator import run_mode as _run_mode
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, FrameBreak, NextPageTemplate,
    Paragraph, Spacer, Table, TableStyle, Image, HRFlowable, KeepTogether,
)

# ── Page geometry (A4, two columns) ────────────────────────────────────────
_PAGE_W, _PAGE_H = A4
_MARGIN_X = 1.9*cm
_MARGIN_TOP = 2.3*cm
_MARGIN_BOTTOM = 2.2*cm
_TEXT_W = _PAGE_W - 2*_MARGIN_X
_COL_GAP = 0.6*cm
_COL_W = (_TEXT_W - _COL_GAP) / 2
_BODY_H = _PAGE_H - _MARGIN_TOP - _MARGIN_BOTTOM

# ── Monochrome text palette ────────────────────────────────────────────────
_INK   = HexColor('#000000')   # body text and rules, as in a journal preprint
_GREY  = HexColor('#555555')   # byline, running head, footer
_RULE  = HexColor('#000000')

# Verdict colours are still returned by the verdict helpers (their callers may
# rely on the tuple shape) but the PDF prints verdicts as plain text.
_MUTED = HexColor('#717784')
_GOOD  = HexColor('#4a7c59')
_WARN  = HexColor('#9c7b3a')
_CRIT  = HexColor('#9e5b52')

# ── Chart palette (seaborn "colorblind", the usual look of paper figures) ──
_C_BLUE   = '#0173b2'
_C_ORANGE = '#de8f05'
_C_GREY   = '#949494'
_C_TEXT   = '#222222'
_C_GRID   = '#d9d9d9'

# Built-in Type-1 fonts: Times for prose and headings, Helvetica for tables,
# captions, running head and footer (as in the Elsevier preprint class).
_SERIF = "Times-Roman"
_SERIF_B = "Times-Bold"
_SERIF_I = "Times-Italic"
_SANS = "Helvetica"
_SANS_B = "Helvetica-Bold"
_MONO = "Courier"


# ─── Entry point (signature preserved) ────────────────────────────────────

def generate_report(
    experiment_id: str,
    dataset_type: str,
    dataset_path: str,
    dataset_hash: str,
    pipeline_id: str,
    pipeline_info: dict,
    metrics: dict,
    metadata: dict | None = None,
    label_mapping: dict | None = None,
    feature_names: list[str] | None = None,
) -> bytes:
    """Generate the preprint-style PDF and return it as bytes."""
    ctx = _build_context(
        experiment_id=experiment_id, dataset_type=dataset_type,
        dataset_path=dataset_path, dataset_hash=dataset_hash,
        pipeline_id=pipeline_id, pipeline_info=pipeline_info or {},
        metrics=metrics or {}, metadata=metadata or {},
        label_mapping=label_mapping, feature_names=feature_names,
    )
    # Numbering counters for sections / subsections / tables / figures.
    ctx["_counters"] = {"sec": 0, "sub": 0, "tab": 0, "fig": 0}

    # BAHASA laporan ditetapkan SEKALI, di sini, lalu dibawa di `ctx`. Membaca
    # bahasa aktif berulang kali saat menggambar akan membuat satu laporan bisa
    # separuh berganti bila pengguna mengubah bahasa di tengah pembuatan.
    #
    # Impor dilakukan di dalam fungsi, bukan di tingkat modul: `utils/` dan
    # `orchestrator/` tidak boleh bergantung pada lapisan antarmuka.
    from ui.i18n.core import current_lang, lookup

    ctx["lang"] = current_lang()
    ctx["_t"] = lambda key, **values: (
        lookup(key, ctx["lang"]).format(**values) if values
        else lookup(key, ctx["lang"]))

    styles = _build_styles()

    front = _article_head(styles, ctx)
    body: list = []
    for section_fn in (
        _section_1_konfigurasi,
        _section_2_hasil,
        _section_3_keamanan,
        _section_4_analisis,
        _section_5_fitur,
        _section_6_diagnostik,
        _section_7_metodologi,
        _section_8_reproducibility,
    ):
        try:
            section_fn(body, styles, ctx)
        except Exception as e:
            body.append(Paragraph(
                f"<i>(Bagian gagal di-render: {type(e).__name__}: "
                f"{escape(str(e))})</i>",
                styles["note"],
            ))

    buffer = io.BytesIO()
    doc = _build_doc(buffer, front, ctx)
    doc.build([NextPageTemplate("later")] + front + [FrameBreak()] + body,
              canvasmaker=_numbered_canvas(ctx))
    buffer.seek(0)
    return buffer.read()


# ─── Page templates, running head, "Halaman x dari y" ─────────────────────

def _flow_height(flowables, width) -> float:
    """Height the article head needs at ``width``, so page one can give it a
    full-width frame of exactly that size and start the two columns below."""
    total = 0.0
    for i, f in enumerate(flowables):
        _, h = f.wrap(width, _BODY_H)
        total += h + f.getSpaceAfter()
        if i:
            total += f.getSpaceBefore()
    return total


def _build_doc(buffer, front, ctx) -> BaseDocTemplate:
    head_h = min(_flow_height(front, _TEXT_W) + 3*mm, _BODY_H - 6*cm)
    gap = 5*mm
    no_pad = dict(leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)

    def _col(x, h, fid):
        return Frame(x, _MARGIN_BOTTOM, _COL_W, h, id=fid, **no_pad)

    left_x = _MARGIN_X
    right_x = _MARGIN_X + _COL_W + _COL_GAP
    first_col_h = _BODY_H - head_h - gap
    first = PageTemplate(id="first", frames=[
        Frame(_MARGIN_X, _MARGIN_BOTTOM + first_col_h + gap, _TEXT_W, head_h,
              id="head", **no_pad),
        _col(left_x, first_col_h, "c1"),
        _col(right_x, first_col_h, "c2"),
    ], onPage=_page_decor(ctx))
    later = PageTemplate(id="later", frames=[
        _col(left_x, _BODY_H, "c1"),
        _col(right_x, _BODY_H, "c2"),
    ], onPage=_page_decor(ctx))

    doc = BaseDocTemplate(
        buffer, pagesize=A4,
        leftMargin=_MARGIN_X, rightMargin=_MARGIN_X,
        topMargin=_MARGIN_TOP, bottomMargin=_MARGIN_BOTTOM,
        title=f"Laporan Eksperimen {ctx['pipeline_id']}",
        author="ReproIDS",
    )
    doc.addPageTemplates([first, later])
    return doc


def _page_decor(ctx):
    """Running head (centred, sans) and the footer's left half. The page
    number on the right needs the total, so the canvas draws it at save."""
    t = ctx["_t"]
    head = t("rpt.main_title")
    tag = t("rpt.footer_tag")
    who = f"ReproIDS · {ctx['pipeline_id'] or t('rpt.na')}: "

    def _draw(canv, _doc):
        canv.saveState()
        canv.setFillColor(_INK)
        canv.setFont(_SANS, 8.5)
        canv.drawCentredString(_PAGE_W / 2, _PAGE_H - 1.45*cm, head)

        y_rule = _MARGIN_BOTTOM - 0.75*cm
        canv.setStrokeColor(_RULE)
        canv.setLineWidth(0.4)
        canv.line(_MARGIN_X, y_rule, _PAGE_W - _MARGIN_X, y_rule)
        y_txt = y_rule - 0.38*cm
        canv.setFont(_SANS, 8)
        canv.drawString(_MARGIN_X, y_txt, who)
        canv.setFont(_SERIF_I, 8.5)
        canv.drawString(_MARGIN_X + canv.stringWidth(who, _SANS, 8), y_txt, tag)
        canv.restoreState()
    return _draw


def _numbered_canvas(ctx):
    """Canvas class that defers every page until the total is known, then
    stamps "Halaman x dari y" bottom-right."""
    t = ctx["_t"]

    class _NumberedCanvas(_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self.setFont(_SANS, 8)
                self.setFillColor(_INK)
                self.drawRightString(
                    _PAGE_W - _MARGIN_X, _MARGIN_BOTTOM - 0.75*cm - 0.38*cm,
                    t("rpt.page_of", page=self._pageNumber, total=total))
                super().showPage()
            super().save()

    return _NumberedCanvas

# ─── Context builder (single read of all inputs) ──────────────────────────

def _build_context(**kw) -> dict:
    """Collect every piece of data the sections may need into one dict.

    Defensive: missing fields become None / [] / {}; sections decide how to
    render absence. No defaults that lie about values (e.g. accuracy=0).
    """
    md = kw.get("metadata") or {}
    env = md.get("environment") if isinstance(md.get("environment"), dict) else {}
    metrics = kw.get("metrics") or {}
    pinfo = kw.get("pipeline_info") or {}

    created_at = md.get("created_at")
    completed_at = md.get("completed_at")
    wall_clock = _wall_clock(created_at, completed_at)

    dataset_type = kw.get("dataset_type")
    label_mapping = kw.get("label_mapping") or md.get("label_mapping")

    breakdown = _confusion_breakdown(metrics.get("confusion_matrix"), label_mapping)

    return {
        "experiment_id": kw.get("experiment_id"),
        "dataset_type": dataset_type,
        "is_eve": dataset_type == "EVE_SURICATA",
        "dataset_path": kw.get("dataset_path"),
        "dataset_hash": kw.get("dataset_hash") or "N/A",
        "pipeline_id": kw.get("pipeline_id"),
        "label_mapping": label_mapping,
        "feature_names": kw.get("feature_names") or md.get("feature_names"),
        "paper": pinfo.get("paper"),
        "algorithm": pinfo.get("algorithm"),
        "preprocessing_steps": pinfo.get("preprocessing_steps") or [],
        "feature_selection": pinfo.get("feature_selection"),
        "fixed_params": pinfo.get("fixed_params") or {},
        # Mode & parameter dibaca dari METADATA artefak, yaitu apa yang
        # benar-benar dipakai saat run itu, bukan dari definisi pipeline pada
        # kode saat ini. Artefak lama tidak punya keduanya; NULL/absen dibaca
        # sebagai run RESMI, sama seperti di basis data.
        "run_mode": _run_mode.normalize_run_mode(md.get("run_mode")),
        "run_mode_recorded": bool(md.get("run_mode")),
        "params_used": md.get("params_used") if isinstance(md.get("params_used"), dict) else {},
        "params_locked": md.get("params_locked") if isinstance(md.get("params_locked"), dict) else {},
        "params_changed": list(md.get("params_changed") or []),
        "train_test_split": pinfo.get("train_test_split") or {},
        "anti_leakage_info": pinfo.get("anti_leakage"),
        "metrics_policy": pinfo.get("metrics_policy"),
        "runtime_warning": pinfo.get("runtime_warning"),
        "metrics": metrics,
        "accuracy": metrics.get("accuracy"),
        "precision": metrics.get("precision"),
        "recall": metrics.get("recall"),
        "f1_score": metrics.get("f1_score"),
        "roc_auc": metrics.get("roc_auc"),
        "confusion_matrix": metrics.get("confusion_matrix"),
        "breakdown": breakdown,
        "feature_importance": metrics.get("feature_importance") or [],
        "classification_report": metrics.get("classification_report") or {},
        "learning_curve": metrics.get("learning_curve") if isinstance(
            metrics.get("learning_curve"), dict
        ) and "error" not in (metrics.get("learning_curve") or {}) else None,
        "natural_holdout": metrics.get("natural_holdout") if isinstance(metrics.get("natural_holdout"), dict) else {},
        "balanced_holdout": metrics.get("balanced_holdout") if isinstance(metrics.get("balanced_holdout"), dict) else {},
        "anti_leakage": metrics.get("anti_leakage") if isinstance(metrics.get("anti_leakage"), dict) else {},
        "evaluation": metrics.get("evaluation") if isinstance(metrics.get("evaluation"), dict) else {},
        "selected_combo": metrics.get("selected_combo") if isinstance(metrics.get("selected_combo"), dict) else {},
        "created_at": created_at or "N/A",
        "completed_at": completed_at or "N/A",
        "wall_clock": wall_clock,
        "env": env,
        "python_version": env.get("python_version"),
        "sklearn_version": env.get("sklearn_version"),
        "pandas_version": env.get("pandas_version"),
        "numpy_version": env.get("numpy_version"),
        "is_docker": env.get("is_docker"),
        "docker_image_version": env.get("docker_image_version"),
        "platform_str": env.get("platform"),
    }


def _wall_clock(created_at, completed_at) -> str | None:
    if not created_at or not completed_at:
        return None
    try:
        s = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        e = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
        secs = (e - s).total_seconds()
        if secs < 0:
            return None
        if secs < 60:
            return f"{secs:.1f} s"
        if secs < 3600:
            return f"{secs/60:.2f} min"
        return f"{secs/3600:.2f} h"
    except Exception:
        return None


# ─── Operational engine (PRESERVED: computed from real numbers) ──────────

def _norm_label_mapping(label_mapping) -> dict:
    """Return {int_index: name} from a mapping that may have str or int keys."""
    out = {}
    if isinstance(label_mapping, dict):
        for k, v in label_mapping.items():
            try:
                out[int(k)] = str(v)
            except (ValueError, TypeError):
                continue
    return out


_ATTACK_WORDS = ("attack", "malicious", "malign", "intrusion", "anomaly", "serangan")


def _confusion_breakdown(cm, label_mapping) -> dict | None:
    """Translate a binary 2x2 confusion matrix into operational counts.

    Convention (sklearn): rows = actual, cols = predicted, label order ascending
    [0, 1]. Positive class (attack) is detected by name when possible, else
    defaults to index 1. Returns None for non-binary / empty / all-zero matrices
    so callers can hide the operational section cleanly.
    """
    if not cm or not isinstance(cm, (list, tuple)) or len(cm) != 2:
        return None
    try:
        c = [[int(cm[0][0]), int(cm[0][1])], [int(cm[1][0]), int(cm[1][1])]]
    except (ValueError, TypeError, IndexError):
        return None

    total = c[0][0] + c[0][1] + c[1][0] + c[1][1]
    if total <= 0:
        return None

    names = _norm_label_mapping(label_mapping)
    pos = 1
    for idx, name in names.items():
        if any(w in name.lower() for w in _ATTACK_WORDS):
            pos = idx
            break
    neg = 0 if pos == 1 else 1

    attack_name = names.get(pos, "Serangan")
    normal_name = names.get(neg, "Normal")

    tp = c[pos][pos]
    fn = c[pos][neg]
    fp = c[neg][pos]
    tn = c[neg][neg]

    attack_total = tp + fn
    normal_total = tn + fp
    pred_attack = tp + fp

    def _safe(n, d):
        return (n / d) if d else None

    big, small = max(attack_total, normal_total), min(attack_total, normal_total)
    imbalance_ratio = (big / small) if small else None

    a_rec = _safe(tp, attack_total)
    a_prec = _safe(tp, pred_attack)
    a_f1 = None
    if a_rec is not None and a_prec is not None and (a_rec + a_prec) > 0:
        a_f1 = 2 * a_prec * a_rec / (a_prec + a_rec)

    return {
        "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        "total": total,
        "attack_total": attack_total, "normal_total": normal_total,
        "pred_attack": pred_attack,
        "attack_recall": a_rec,
        "attack_precision": a_prec,
        "attack_f1": a_f1,
        "fp_rate": _safe(fp, normal_total),
        "specificity": _safe(tn, normal_total),
        "attack_share": _safe(attack_total, total),
        "imbalance_ratio": imbalance_ratio,
        "attack_name": attack_name,
        "normal_name": normal_name,
        "pos_index": pos, "neg_index": neg,
    }


def _pct(x) -> str:
    return f"{x*100:.1f}%" if isinstance(x, (int, float)) else "[tidak tersedia]"


def _grp(n) -> str:
    """Thousands-grouped integer string (e.g. 135.046) using dot separator."""
    try:
        return f"{int(n):,}".replace(",", ".")
    except (ValueError, TypeError):
        return str(n)


def _recall_verdict(r):
    """AMBANGNYA TIDAK BERUBAH; hanya label & klausanya kini berupa kunci."""
    if r is None:
        return _MUTED, "vd.unavailable", ""
    if r >= 0.95:
        return _GOOD, "vd.excellent", "vd.recall_excellent"
    if r >= 0.85:
        return _GOOD, "vd.good", "vd.recall_good"
    if r >= 0.60:
        return _WARN, "vd.attention", "vd.recall_attention"
    return _CRIT, "vd.weak", "vd.recall_weak"


def _precision_verdict(p):
    if p is None:
        return _MUTED, "vd.unavailable", ""
    if p >= 0.90:
        return _GOOD, "vd.excellent", "vd.precision_excellent"
    if p >= 0.75:
        return _GOOD, "vd.good", "vd.precision_good"
    if p >= 0.50:
        return _WARN, "vd.attention", "vd.precision_attention"
    return _CRIT, "vd.weak", "vd.precision_weak"


def _f1_verdict(f):
    if f is None:
        return _MUTED, "vd.unavailable", ""
    if f >= 0.90:
        return _GOOD, "vd.excellent", "vd.f1_excellent"
    if f >= 0.75:
        return _GOOD, "vd.good", "vd.f1_good"
    if f >= 0.55:
        return _WARN, "vd.attention", "vd.f1_attention"
    return _CRIT, "vd.weak", "vd.f1_weak"


def _auc_verdict(a):
    """AMBANGNYA TIDAK BERUBAH; hanya label & klausanya kini berupa kunci."""
    if a is None:
        return _MUTED, "vd.unavailable", ""
    if a >= 0.90:
        return _GOOD, "vd.excellent", "vd.auc_excellent"
    if a >= 0.80:
        return _GOOD, "vd.good", "vd.auc_good"
    if a >= 0.70:
        return _WARN, "vd.attention", "vd.auc_attention"
    return _CRIT, "vd.weak", "vd.auc_weak"


# ─── Network meaning of features (PRESERVED) ──────────────────────────────

_FEATURE_EXACT = {
    "bytes_per_sec": "rpt.feat_bytes_per_sec",
    "pkts_per_sec": "rpt.feat_pkts_per_sec",
    "bytes_per_pkt": "rpt.feat_bytes_per_pkt",
    "total_bytes": "rpt.feat_total_bytes",
    "total_pkts": "rpt.feat_total_pkts",
    "duration": "rpt.feat_flow_duration",
    "bytes_toserver": "rpt.feat_bytes_toserver",
    "pkts_toserver": "rpt.feat_pkts_toserver",
    "pkts_toclient": "rpt.feat_pkts_toclient",
    "bytes_toserver_ratio": "rpt.feat_bytes_toserver_ratio",
    "pkts_toserver_ratio": "rpt.feat_pkts_toserver_ratio",
    "src_port": "rpt.feat_src_port",
    "src_port_class": "rpt.feat_src_port_class",
    "dest_port_class": "rpt.feat_dest_port_class",
    "unique_dest_port_window": "rpt.feat_unique_dest_port_window",
    "unique_dest_ip_window": "rpt.feat_unique_dest_ip_window",
    "event_count_window": "rpt.feat_event_count_window",
    "no_alert_count_window": "rpt.feat_no_alert_count_window",
    "total_bytes_window": "rpt.feat_total_bytes_window",
    "total_pkts_window": "rpt.feat_total_pkts_window",
    "bytes_per_event_window": "rpt.feat_bytes_per_event_window",
    "pkts_per_event_window": "rpt.feat_pkts_per_event_window",
    "ts_hour": "rpt.feat_ts_hour",
    "app_proto_h": "rpt.feat_app_proto_h",
    "interaction_bytes_rate_packet_rate": "rpt.feat_interaction_bytes_rate_packet_rate",
    "interaction_total_bytes_duration": "rpt.feat_interaction_total_bytes_duration",
    "interaction_total_pkts_duration": "rpt.feat_interaction_total_pkts_duration",
    "flow_duration": "rpt.feat_flow_duration",
    "down_up_ratio": "rpt.feat_down_up_ratio_flow",
    "fwd_subflow_bytes": "rpt.feat_fwd_subflow_bytes",
    "bwd_subflow_bytes": "rpt.feat_bwd_subflow_bytes",
    "fwd_init_window_size": "rpt.feat_fwd_init_window_size",
    "bwd_init_window_size": "rpt.feat_bwd_init_window_size",
}


def _feature_network_meaning(name: str, t) -> str | None:
    """Arti jaringan sebuah fitur, diturunkan dari namanya.

    Mengembalikan kalimat yang SUDAH diterjemahkan, atau None bila nama fitur
    tidak memuat token jaringan yang dikenali. Pencocokannya identik dengan
    versi sebelumnya; hanya kalimatnya yang kini berasal dari katalog.
    """
    if not name:
        return None
    key = str(name).lower()
    if key in _FEATURE_EXACT:
        return t(_FEATURE_EXACT[key])

    # Awalan dicatat dulu, dipasang belakangan secara BERSARANG: urutan
    # katanya boleh berbeda antar bahasa, jadi tidak boleh disambung.
    wrappers = []
    work = key
    if work.startswith("log_"):
        wrappers.append("rpt.feat_log_of")
        work = work[4:]
    if work.startswith("interaction_"):
        wrappers.append("rpt.feat_interaction_of")
        work = work[len("interaction_"):]

    def _wrap(text: str) -> str:
        for wrapper in wrappers:
            text = t(wrapper, meaning=text)
        return text

    if work in _FEATURE_EXACT:
        return _wrap(t(_FEATURE_EXACT[work]))

    rules = [
        ("init_window", "rpt.feat_init_window"),
        ("window_size", "rpt.feat_window_size"),
        ("duration", "rpt.feat_duration_short"),
        ("iat", "rpt.feat_iat"),
        ("per_sec", "rpt.feat_per_sec"),
        ("_rate", "rpt.feat_rate"),
        ("down_up_ratio", "rpt.feat_down_up_ratio"),
        ("ratio", "rpt.feat_ratio"),
        ("unique_dest_port", "rpt.feat_unique_dest_port"),
        ("unique_dest_ip", "rpt.feat_unique_dest_ip"),
        ("port_class", "rpt.feat_port_class"),
        ("dest_port", "rpt.feat_dest_port"),
        ("src_port", "rpt.feat_src_port"),
        ("toserver", "rpt.feat_toserver"),
        ("toclient", "rpt.feat_toclient"),
        ("subflow", "rpt.feat_subflow"),
        ("payload", "rpt.feat_payload"),
        ("header", "rpt.feat_header"),
        ("flag", "rpt.feat_flag"),
        ("bulk", "rpt.feat_bulk"),
        ("active", "rpt.feat_active"),
        ("idle", "rpt.feat_idle"),
        ("seg_size", "rpt.feat_seg_size"),
        ("window", "rpt.feat_window"),
        ("bytes", "rpt.feat_bytes"),
        ("pkts", "rpt.feat_pkts"),
        ("packet", "rpt.feat_pkts"),
        ("alert", "rpt.feat_alert"),
        ("event", "rpt.feat_event"),
        ("proto", "rpt.feat_proto"),
        ("hour", "rpt.feat_hour"),
        ("flow", "rpt.feat_flow"),
    ]
    for token, meaning_key in rules:
        if token in work:
            return _wrap(t(meaning_key))
    return None



# ─── Style sheet (Times prose, Helvetica tables/captions) ─────────────────

def _build_styles() -> dict:
    base = getSampleStyleSheet()["Normal"]

    def s(name, **kw):
        kw.setdefault("textColor", _INK)
        return ParagraphStyle(name, parent=base, **kw)

    return {
        # Article head
        "title": s("Title", fontName=_SERIF, fontSize=19, leading=23,
                   spaceAfter=3*mm),
        "byline": s("Byline", fontName=_SERIF, fontSize=11, leading=14,
                    textColor=_GREY, spaceAfter=2*mm),
        "affil": s("Affil", fontName=_SERIF_I, fontSize=7.5, leading=9.5),
        "head_label": s("HeadLabel", fontName=_SERIF, fontSize=8, leading=10),
        "info": s("Info", fontName=_SERIF, fontSize=8, leading=10),
        "info_i": s("InfoI", fontName=_SERIF_I, fontSize=8, leading=10),
        "abstract": s("Abstract", fontName=_SERIF, fontSize=8.5, leading=10.8,
                      alignment=TA_JUSTIFY),
        "box": s("Box", fontName=_SERIF, fontSize=9, leading=11.5,
                 alignment=TA_JUSTIFY),
        # Body
        "section": s("Section", fontName=_SERIF_B, fontSize=11, leading=13.5,
                     spaceBefore=4.5*mm, spaceAfter=2*mm, keepWithNext=1),
        "subsection": s("Subsec", fontName=_SERIF_B, fontSize=10, leading=12.5,
                        spaceBefore=3*mm, spaceAfter=1.5*mm, keepWithNext=1),
        "body": s("Body", fontName=_SERIF, fontSize=10, leading=12.2,
                  alignment=TA_JUSTIFY, firstLineIndent=3.5*mm),
        "body_flat": s("BodyFlat", fontName=_SERIF, fontSize=10, leading=12.2,
                       alignment=TA_LEFT, spaceAfter=1*mm),
        "note": s("Note", fontName=_SERIF, fontSize=8.5, leading=10.5,
                  alignment=TA_JUSTIFY),
        # Tables and captions
        "cap": s("Cap", fontName=_SANS, fontSize=8, leading=10,
                 spaceAfter=1.5*mm),
        "fig_cap": s("FigCap", fontName=_SANS, fontSize=8, leading=10,
                     alignment=TA_CENTER, spaceBefore=1*mm),
        "th": s("TH", fontName=_SANS_B, fontSize=7.8, leading=9.5),
        "th_num": s("THNum", fontName=_SANS_B, fontSize=7.8, leading=9.5,
                    alignment=TA_RIGHT),
        "td": s("TD", fontName=_SANS, fontSize=7.8, leading=9.5),
        "td_num": s("TDNum", fontName=_SANS, fontSize=7.8, leading=9.5,
                    alignment=TA_RIGHT),
        "t_note": s("TNote", fontName=_SANS, fontSize=7, leading=8.8,
                    alignment=TA_JUSTIFY),
    }


# ─── Numbering + reusable blocks ──────────────────────────────────────────

def _next(ctx, key) -> int:
    ctx["_counters"][key] += 1
    return ctx["_counters"][key]


def _section(story, styles, ctx, title: str) -> None:
    n = _next(ctx, "sec")
    ctx["_counters"]["sub"] = 0
    story.append(Paragraph(f"{n}.&nbsp;&nbsp;{title}", styles["section"]))


def _subsection(story, styles, ctx, title: str) -> None:
    n = _next(ctx, "sub")
    sec = ctx["_counters"]["sec"]
    story.append(Paragraph(f"{sec}.{n}.&nbsp;&nbsp;{title}",
                           styles["subsection"]))


def _cell(value, style):
    if hasattr(value, "wrap"):          # already a flowable
        return value
    return Paragraph(str(value), style)


def _table(story, styles, ctx, caption: str, rows, fracs, *,
           num_cols=(), note: str | None = None) -> None:
    """Booktabs table at column width, captioned ABOVE ("Tabel n" in bold on
    its own line, the caption under it), optional note BELOW. Caption, table
    and note are kept together so a caption never strands at a column foot."""
    n = _next(ctx, "tab")
    cap = Paragraph(f"<b>{ctx['_t']('rpt.table_label')} {n}</b><br/>{caption}",
                    styles["cap"])
    data = []
    for r, row in enumerate(rows):
        out = []
        for c, value in enumerate(row):
            num = c in num_cols
            if r == 0:
                out.append(_cell(value, styles["th_num" if num else "th"]))
            else:
                out.append(_cell(value, styles["td_num" if num else "td"]))
        data.append(out)
    tbl = Table(data, colWidths=[f * _COL_W for f in fracs], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEABOVE", (0, 0), (-1, 0), 0.8, _RULE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, _RULE),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, _RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    block = [cap, tbl]
    if note:
        block += [Spacer(1, 1.2*mm), Paragraph(note, styles["t_note"])]
    story.append(Spacer(1, 1.5*mm))
    story.append(KeepTogether(block))
    story.append(Spacer(1, 2.5*mm))


def _figure(story, styles, ctx, img: io.BytesIO, caption: str, *,
            width_frac: float = 1.0, max_h: float = 11*cm) -> None:
    """Figure at column width, true aspect ratio, captioned BELOW."""
    iw, ih = ImageReader(img).getSize()
    img.seek(0)
    w = _COL_W * width_frac
    h = w * ih / iw
    if h > max_h:
        w, h = w * max_h / h, max_h
    n = _next(ctx, "fig")
    story.append(Spacer(1, 1.5*mm))
    story.append(KeepTogether([
        Image(img, width=w, height=h),
        Paragraph(f"<b>{ctx['_t']('rpt.figure_label')} {n}:</b> {caption}",
                  styles["fig_cap"]),
    ]))
    story.append(Spacer(1, 2.5*mm))


def _boxed(text: str, styles, width=None):
    """A note set between two thin rules, like the Listing/Algorithm blocks of
    a preprint. No fill, no colour: it has to survive a grayscale printer."""
    t = Table([[Paragraph(text, styles["box"])]],
              colWidths=[width or _COL_W])
    t.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 0.6, _RULE),
        ("LINEBELOW", (0, -1), (-1, -1), 0.6, _RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _spaced_caps(text: str) -> str:
    """"ARTICLE INFO" letter-spacing, done with spaces (ReportLab has no
    tracking): letters one space apart, words three."""
    words = str(text).upper().split()
    return "&nbsp;&nbsp;&nbsp;".join(" ".join(w) for w in words)


def _none_or(v, fallback):
    """Nilai, atau penanda kosong. `fallback` WAJIB diisi pemanggil.

    Dulu penandanya adalah bawaan berbahasa Indonesia, sehingga bocor ke
    setiap sel kosong pada laporan Inggris. Tanpa bawaan, kelalaian yang sama
    menjadi galat yang terlihat, bukan teks salah bahasa yang diam.
    """
    if v is None or v == "":
        return fallback
    return str(v)


#: Placeholder for an empty table cell. A hyphen, never a dash.
_EMPTY = "-"


# ─── Article head (title, byline, info column, abstract) ──────────────────

def _compose_abstract(ctx) -> str:
    """3 to 5 computed sentences (abstract-like). Never a blank template."""
    t = ctx["_t"]
    algo = ctx["algorithm"] or t("rpt.abs_algo_fallback")
    ds = (t("rpt.abs_dataset_eve") if ctx["is_eve"]
          else (ctx["dataset_type"] or t("rpt.abs_dataset_fallback")))
    sem = t("rpt.abs_semantics_eve" if ctx["is_eve"]
            else "rpt.abs_semantics_weighted")
    b = ctx["breakdown"]
    if b and b["attack_recall"] is not None:
        _, rlabel_key, _ = _recall_verdict(b["attack_recall"])
        f1 = b["attack_f1"]
        # Dua kalimat UTUH, bukan satu kalimat plus tempelan: klausa F1 yang
        # dulu disambung membuat urutan katanya terkunci pada tata bahasa
        # Indonesia.
        values = dict(
            algo=algo, dataset=ds, total=_grp(b["total"]),
            attacks=_grp(b["attack_total"]), normals=_grp(b["normal_total"]),
            recall=_pct(b["attack_recall"]), tp=_grp(b["tp"]),
            precision=_pct(b["attack_precision"]), missed=_grp(b["fn"]),
            false_alarms=_grp(b["fp"]), verdict=t(rlabel_key), semantics=sem)
        if isinstance(f1, (int, float)):
            return t("rpt.abs_main_f1", f1=f"{f1:.4f}", **values)
        return t("rpt.abs_main", **values)
    # Fallback when the confusion matrix is unavailable/non-binary.
    parts = []
    if isinstance(ctx["accuracy"], (int, float)):
        parts.append(f"accuracy {ctx['accuracy']:.4f}")
    if isinstance(ctx["f1_score"], (int, float)):
        parts.append(f"F1 {ctx['f1_score']:.4f}")
    if isinstance(ctx["roc_auc"], (int, float)):
        parts.append(f"AUC {ctx['roc_auc']:.4f}")
    metr = (", ".join(parts)) if parts else t("rpt.abs_metrics_fallback")
    return t("rpt.abs_no_confusion", algo=algo, dataset=ds, metrics=metr,
             semantics=sem)


#: Mode eksekusi → kunci kamus. Konstanta di `orchestrator/run_mode` TIDAK
#: diubah; pemetaannya hidup di sini, di lapisan keluaran.
_RUN_MODE_LABEL_KEYS = {
    _run_mode.RUN_MODE_OFFICIAL: "mode.official_label",
    _run_mode.RUN_MODE_EXPLORATION: "mode.exploration_label",
}
_RUN_MODE_HINT_KEYS = {
    _run_mode.RUN_MODE_OFFICIAL: "mode.official_hint",
    _run_mode.RUN_MODE_EXPLORATION: "mode.exploration_hint",
}


def _article_head(styles, ctx) -> list:
    """Everything above the two columns on page one, as a flowable list. Its
    height is measured to size page one's full-width frame."""
    t = ctx["_t"]
    na = t("rpt.na")
    out = [Paragraph(t("rpt.main_title"), styles["title"])]

    byline = escape(_none_or(ctx["pipeline_id"], na))
    if ctx["algorithm"]:
        byline += f"&nbsp;&nbsp;·&nbsp;&nbsp;{escape(str(ctx['algorithm']))}"
    out.append(Paragraph(byline, styles["byline"]))
    out.append(Paragraph(t("rpt.subtitle"), styles["affil"]))
    out.append(Paragraph(
        f"Experiment ID {escape(_none_or(ctx['experiment_id'], na))}",
        styles["affil"]))
    out.append(Spacer(1, 4*mm))

    # Penanda run eksplorasi di HALAMAN PERTAMA, sebelum satu angka pun
    # terbaca: laporan ini bisa beredar terpisah dari aplikasi.
    if _run_mode.is_exploration(ctx["run_mode"]):
        out.append(_boxed("<b>" + t("rpt.exploration_badge") + "</b> "
                          + t("rpt.exploration_warning"), styles,
                          width=_TEXT_W))
        out.append(Spacer(1, 4*mm))

    # Left column, the journal's "ARTICLE INFO": history and keywords.
    info_w = 4.9*cm
    abs_w = _TEXT_W - info_w - 0.7*cm
    dataset_kw = (t("rpt.abs_dataset_eve") if ctx["is_eve"]
                  else ctx["dataset_type"])
    keywords = [k for k in (
        ctx["algorithm"], dataset_kw, t("rpt.kw_intrusion"), t("rpt.kw_ml"),
        t(_RUN_MODE_LABEL_KEYS[ctx["run_mode"]]),
    ) if k]
    info = [
        Paragraph(_spaced_caps(t("rpt.info_h")), styles["head_label"]),
        HRFlowable(width="100%", thickness=0.4, color=_RULE,
                   spaceBefore=1.5*mm, spaceAfter=1.5*mm),
        Paragraph(t("rpt.history_h"), styles["info_i"]),
        Paragraph(t("rpt.lbl_created", time=escape(_none_or(ctx["created_at"], na))),
                  styles["info"]),
        Paragraph(t("rpt.lbl_completed", time=escape(_none_or(ctx["completed_at"], na))),
                  styles["info"]),
        Paragraph(t("rpt.lbl_report_generated", time=datetime.utcnow().strftime(
            "%Y-%m-%d %H:%M UTC")), styles["info"]),
        Spacer(1, 2*mm),
        Paragraph(t("rpt.keywords_h"), styles["info_i"]),
    ] + [Paragraph(escape(str(k)), styles["info"]) for k in keywords]
    abstract = [
        Paragraph(_spaced_caps(t("rpt.abstract_h")), styles["head_label"]),
        HRFlowable(width="100%", thickness=0.4, color=_RULE,
                   spaceBefore=1.5*mm, spaceAfter=1.5*mm),
        Paragraph(_compose_abstract(ctx), styles["abstract"]),
    ]
    head = Table([[info, "", abstract]],
                 colWidths=[info_w, _TEXT_W - info_w - abs_w, abs_w])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    out.append(head)
    out.append(HRFlowable(width="100%", thickness=0.4, color=_RULE,
                          spaceBefore=3*mm, spaceAfter=0))
    return out


# ─── 1. Konfigurasi Eksperimen ────────────────────────────────────────────

def _section_1_konfigurasi(story, styles, ctx):
    t = ctx["_t"]
    na = t("rpt.na")
    _section(story, styles, ctx, t("rpt.sec_config"))

    if ctx["is_eve"]:
        fmt = t("rpt.fmt_ndjson")
        label_origin = t("rpt.label_origin_eve")
    else:
        fmt = t("rpt.fmt_csv")
        label_origin = t("rpt.label_origin_hikari")

    # "Experiment ID", "Pipeline", "Dataset" adalah nama teknis yang sengaja
    # TIDAK diterjemahkan, sama seperti nama kolom pada ekspor CSV, supaya
    # laporan tetap dapat dibaca lintas bahasa.
    rows = [
        [t("rpt.col_item"), t("rpt.col_value")],
        ["Experiment ID", escape(_none_or(ctx["experiment_id"], na))],
        ["Pipeline", escape(_none_or(ctx["pipeline_id"], na))],
        [t("rpt.lbl_algorithm"), escape(_none_or(ctx["algorithm"], na))],
        [t("rpt.lbl_paper"), escape(_none_or(ctx["paper"], na))],
        ["Dataset", escape(_none_or(ctx["dataset_type"], na))],
        [t("rpt.lbl_dataset_format"), fmt],
        [t("rpt.lbl_label_origin"), label_origin],
        [t("rpt.lbl_source_file"), escape(_none_or(ctx["dataset_path"], na))],
        [t("rpt.lbl_run_mode"), t(_RUN_MODE_LABEL_KEYS[ctx["run_mode"]])
         + ". " + t(_RUN_MODE_HINT_KEYS[ctx["run_mode"]])],
        # Bahasa laporan DICATAT pada laporannya sendiri, supaya pembaca tahu
        # dalam bahasa apa kalimat-kalimatnya ditulis.
        [t("rpt.lbl_language"), t("rpt.language_name")],
    ]
    if ctx["feature_selection"]:
        rows.append(["Feature selection", escape(str(ctx["feature_selection"]))])
    b = ctx["breakdown"]
    if b:
        rows.append([t("rpt.lbl_class_distribution"),
                     t("rpt.class_distribution_value", total=_grp(b["total"]),
                       attacks=_grp(b["attack_total"]),
                       share=_pct(b["attack_share"]),
                       normals=_grp(b["normal_total"]))])
    _table(story, styles, ctx, t("rpt.cap_config"), rows, [0.36, 0.64])

    steps = ctx["preprocessing_steps"]
    if steps:
        story.append(Paragraph(
            f"<b>{t('rpt.lbl_preprocessing')}</b> "
            + "; ".join(escape(str(s)) for s in steps) + ".", styles["body"]))

    # Parameter yang BENAR-BENAR dipakai run ini bila artefaknya mencatatnya;
    # bila tidak (artefak lama), definisi pipeline pada kode saat ini. Perbedaan
    # asal-usul itu dikatakan di judul & keterangan tabel, bukan disamarkan.
    used = ctx["params_used"]
    locked_cfg = ctx["params_locked"] or ctx["fixed_params"]
    cfg = used or ctx["fixed_params"]
    changed = set(ctx["params_changed"] or [])
    if cfg:
        if used:
            heading = t("rpt.params_heading_used_changed" if changed
                        else "rpt.params_heading_used")
            caption = t("rpt.params_caption_used_changed" if changed
                        else "rpt.params_caption_used")
            rows_cfg = [["Parameter", t("rpt.col_value"),
                         t("rpt.col_locked_value")]]
            for k, v in cfg.items():
                mark = "*" if k in changed else ""
                base = locked_cfg.get(k, _EMPTY)
                rows_cfg.append([escape(f"{k}{mark}"), escape(str(v)),
                                 escape(str(base)) if k in changed
                                 else t("rpt.val_same")])
            fracs = [0.38, 0.31, 0.31]
        else:
            heading = t("rpt.params_heading_locked")
            caption = t("rpt.params_caption_locked")
            rows_cfg = ([["Parameter", t("rpt.col_value")]]
                        + [[escape(str(k)), escape(str(v))]
                           for k, v in cfg.items()])
            fracs = [0.45, 0.55]
        _subsection(story, styles, ctx, heading)
        _table(story, styles, ctx, caption, rows_cfg, fracs)


# ─── 2. Hasil dan Metrik ──────────────────────────────────────────────────

def _section_2_hasil(story, styles, ctx):
    t = ctx["_t"]
    na = t("rpt.na")
    _section(story, styles, ctx, t("rpt.sec_results"))

    # "weighted" adalah istilah metrik yang dipakai apa adanya di kedua bahasa.
    _avg = t("rpt.scope_eve") if ctx["is_eve"] else "weighted"

    def _fmt(v):
        return f"{v:.6f}" if isinstance(v, (int, float)) else na

    rows = [[t("rpt.col_metric"), t("rpt.col_value"), t("rpt.col_scope")]]
    rows += [
        ["Accuracy", _fmt(ctx["accuracy"]), t("rpt.scope_all_classes")],
        ["Precision", _fmt(ctx["precision"]), _avg],
        ["Recall", _fmt(ctx["recall"]), _avg],
        ["F1-score", _fmt(ctx["f1_score"]), _avg],
    ]
    if ctx["roc_auc"] is not None:
        rows.append(["ROC-AUC", _fmt(ctx["roc_auc"]), t("rpt.scope_binary")])

    # Mandatory metric-semantics footnote (family-aware; never conflated).
    foot = t("rpt.foot_metric_eve" if ctx["is_eve"]
             else "rpt.foot_metric_hikari")
    _table(story, styles, ctx, t("rpt.cap_metrics"), rows, [0.3, 0.27, 0.43],
           num_cols=[1], note=foot)


# ─── 3. Interpretasi Keamanan (dihitung dari confusion matrix) ────────────

def _section_3_keamanan(story, styles, ctx):
    t = ctx["_t"]
    _section(story, styles, ctx, t("rpt.sec_security"))

    b = ctx["breakdown"]
    if not b:
        story.append(Paragraph(t("rpt.sec_no_confusion"), styles["body"]))
        return

    an, nn = b["attack_name"], b["normal_name"]
    story.append(Paragraph(t("rpt.sec_quadrant_intro", attack=an),
                           styles["body"]))

    quad = [
        [t("rpt.col_category"), t("rpt.col_count"), t("rpt.col_meaning")],
        [t("rpt.quad_tp"), _grp(b["tp"]), t("rpt.quad_tp_note", attack=an)],
        [t("rpt.quad_fn"), _grp(b["fn"]),
         t("rpt.quad_fn_note", attack=an, normal=nn)],
        [t("rpt.quad_fp"), _grp(b["fp"]), t("rpt.quad_fp_note", normal=nn)],
        [t("rpt.quad_tn"), _grp(b["tn"]), t("rpt.quad_tn_note", normal=nn)],
    ]
    _table(story, styles, ctx, t("rpt.cap_quadrants"), quad,
           [0.3, 0.17, 0.53], num_cols=[1])

    # Computed security sentence.
    story.append(Paragraph(
        t("rpt.sec_summary_sentence", missed=_grp(b["fn"]),
          total=_grp(b["attack_total"]), recall=_pct(b["attack_recall"]),
          fp=_grp(b["fp"]), fpr=_pct(b["fp_rate"])), styles["body"]))

    boxes = []
    if b["attack_total"]:
        miss_pct = b["fn"] / b["attack_total"]
        boxes.append("<b>" + t("rpt.callout_missed_label") + "</b> "
                     + t("rpt.callout_missed_body", missed=_grp(b["fn"]),
                         total=_grp(b["attack_total"]), pct=_pct(miss_pct)))
    if b["normal_total"] and b["fp_rate"] is not None:
        boxes.append("<b>" + t("rpt.callout_fp_label") + "</b> "
                     + t("rpt.callout_fp_body", fp=_grp(b["fp"]),
                         total=_grp(b["normal_total"]), pct=_pct(b["fp_rate"])))
    if boxes:
        story.append(Spacer(1, 2*mm))
        story.append(_boxed("<br/><br/>".join(boxes), styles))

    try:
        _figure(story, styles, ctx, _render_network_confusion_matrix(b, t),
                t("rpt.cap_confusion"), width_frac=0.92)
    except Exception as e:
        story.append(Paragraph(t("rpt.err_render_confusion", error=e),
                               styles["note"]))


#: Kunci klausa penilaian → kunci KALIMAT UTUH untuk metrik yang kalimatnya
#: memang berubah menurut penilaian. Pemetaan hidup di lapisan penyajian;
#: ambang penilaian ada di `_f1_verdict`/`_auc_verdict` dan tidak berubah.
_F1_DESC_KEYS = {
    "vd.f1_excellent": "rpt.desc_f1_excellent",
    "vd.f1_good": "rpt.desc_f1_good",
    "vd.f1_attention": "rpt.desc_f1_attention",
    "vd.f1_weak": "rpt.desc_f1_weak",
}
_AUC_DESC_KEYS = {
    "vd.auc_excellent": "rpt.desc_auc_excellent",
    "vd.auc_good": "rpt.desc_auc_good",
    "vd.auc_attention": "rpt.desc_auc_attention",
    "vd.auc_weak": "rpt.desc_auc_weak",
}

#: Legenda skala ROC-AUC. SATU sumber untuk kedua bahasa: disisipkan sebagai
#: nilai, tidak ditulis ulang per bahasa, sehingga angkanya mustahil bergeser.
_AUC_SCALE = {"perfect": "1,0", "chance": "0,5"}


# ─── 4. Analisis Metrik (verdict per metrik, dihitung) ────────────────────

def _section_4_analisis(story, styles, ctx):
    t = ctx["_t"]
    _section(story, styles, ctx, t("rpt.sec_analysis"))
    b = ctx["breakdown"]

    if b:
        recall_val, precision_val, f1_val = b["attack_recall"], b["attack_precision"], b["attack_f1"]
        story.append(Paragraph(t("rpt.analysis_intro_attack"), styles["body"]))
    else:
        recall_val, precision_val, f1_val = ctx["recall"], ctx["precision"], ctx["f1_score"]
        story.append(Paragraph(t("rpt.analysis_intro_plain"), styles["body"]))
    story.append(Spacer(1, 1.5*mm))

    items = []
    if recall_val is not None:
        _, label_key, _ = _recall_verdict(recall_val)
        desc = (t("rpt.desc_recall_detail", pct=_pct(recall_val),
                  missed=_grp(b["fn"]), total=_grp(b["attack_total"])) if b
                else t("rpt.desc_recall", pct=_pct(recall_val)))
        items.append((t("rpt.metric_recall"), recall_val, t(label_key), desc))
    if precision_val is not None:
        _, label_key, _ = _precision_verdict(precision_val)
        desc = (t("rpt.desc_precision_detail", pct=_pct(precision_val),
                  alarms=_grp(b["pred_attack"]), fp=_grp(b["fp"])) if b
                else t("rpt.desc_precision", pct=_pct(precision_val)))
        items.append((t("rpt.metric_precision"), precision_val, t(label_key), desc))
    if f1_val is not None:
        _, label_key, clause_key = _f1_verdict(f1_val)
        items.append((t("rpt.metric_f1"), f1_val, t(label_key),
                      t(_F1_DESC_KEYS.get(clause_key, "rpt.desc_f1"))))
    if ctx["accuracy"] is not None:
        imbalanced = bool(b and b["imbalance_ratio"] and b["imbalance_ratio"] >= 1.5)
        label = t("vd.accuracy_careful" if imbalanced
                  else "vd.accuracy_informative")
        desc = (t("rpt.desc_accuracy_imbalanced", pct=_pct(ctx["accuracy"]),
                  share=_pct(b["attack_share"])) if imbalanced
                else t("rpt.desc_accuracy", pct=_pct(ctx["accuracy"])))
        items.append((t("rpt.metric_accuracy"), ctx["accuracy"], label, desc))
    if ctx["roc_auc"] is not None:
        _, label_key, clause_key = _auc_verdict(ctx["roc_auc"])
        items.append((t("rpt.metric_auc"), ctx["roc_auc"], t(label_key),
                      t(_AUC_DESC_KEYS.get(clause_key, "rpt.desc_auc"),
                        **_AUC_SCALE)))

    # Run-in headings, like a LaTeX \paragraph: the verdict is printed as
    # words, not colour, so it survives a grayscale printer.
    for name, value, label, desc in items:
        story.append(Paragraph(
            f"<b>{name} ({value:.4f}; {label}).</b> {desc}", styles["body"]))


# ─── 5. Fitur Berpengaruh ─────────────────────────────────────────────────

def _section_5_fitur(story, styles, ctx):
    t = ctx["_t"]
    _section(story, styles, ctx, t("rpt.sec_features"))
    fi = ctx["feature_importance"]
    if not fi:
        story.append(Paragraph(
            t("rpt.fi_unavailable",
              algo=ctx["algorithm"] or t("rpt.fi_algo_fallback")),
            styles["body"]))
        return

    story.append(Paragraph(t("rpt.fi_intro"), styles["body"]))
    try:
        _figure(story, styles, ctx, _render_feature_importance(fi, t),
                t("rpt.cap_feature_importance"), max_h=13*cm)
    except Exception as e:
        story.append(Paragraph(t("rpt.err_render_fi", error=e),
                               styles["note"]))

    _subsection(story, styles, ctx, t("rpt.fi_meanings_heading"))
    shown = 0
    for item in fi[:6]:
        name = item.get("feature")
        if not name:
            continue
        meaning = _feature_network_meaning(name, t) or t("rpt.feat_unmapped")
        story.append(Paragraph(
            f"<font face='{_MONO}' size='9'>{escape(str(name))}</font>: "
            f"{meaning}.", styles["body_flat"]))
        shown += 1
        if shown >= 5:
            break


# ─── 6. Diagnostik (ROC, learning curve / dual-holdout, per-class) ────────

def _section_6_diagnostik(story, styles, ctx):
    t = ctx["_t"]
    _section(story, styles, ctx, t("rpt.sec_diagnostics"))
    metrics = ctx["metrics"]
    rendered_any = False

    # ROC
    if ctx["roc_auc"] is not None or "roc_curve" in metrics:
        _subsection(story, styles, ctx, t("rpt.sub_roc"))
        try:
            _figure(story, styles, ctx, _render_roc_curve(metrics, t),
                    t("rpt.cap_roc"), width_frac=0.92)
            rendered_any = True
        except Exception as e:
            story.append(Paragraph(t("rpt.err_render_roc", error=e),
                                   styles["note"]))

    # Learning curve (HIKARI) OR dual-holdout comparison (EVE-cbr)
    if ctx["learning_curve"]:
        _subsection(story, styles, ctx, "Learning Curve")
        try:
            _figure(story, styles, ctx,
                    _render_learning_curve(ctx["learning_curve"]),
                    t("rpt.cap_learning_curve"))
            rendered_any = True
        except Exception as e:
            story.append(Paragraph(t("rpt.err_render_lc", error=e),
                                   styles["note"]))
    else:
        nat, bal = ctx["natural_holdout"], ctx["balanced_holdout"]
        if nat and bal:
            _subsection(story, styles, ctx, t("rpt.sub_dual_holdout"))
            story.append(Paragraph(t("rpt.dual_holdout_intro"),
                                   styles["body"]))
            labels = [("precision_attack", "Precision (attack)"), ("recall_attack", "Recall (attack)"),
                      ("f1_attack", "F1 (attack)"), ("auc", "AUC"), ("accuracy", "Accuracy")]

            def _f(x):
                return f"{x:.4f}" if isinstance(x, (int, float)) else _EMPTY

            rows = [[t("rpt.col_metric"), "Natural-holdout",
                     "Balanced-holdout"]]
            for k, lab in labels:
                if k in nat or k in bal:
                    rows.append([lab, _f(nat.get(k)), _f(bal.get(k))])
            if len(rows) > 1:
                _table(story, styles, ctx, t("rpt.cap_dual_holdout"), rows,
                       [0.38, 0.31, 0.31], num_cols=[1, 2])
                rendered_any = True

    # Per-class report (HIKARI)
    rep = ctx["classification_report"]
    class_rows = {k: v for k, v in rep.items() if isinstance(v, dict)} if rep else {}
    if class_rows:
        _subsection(story, styles, ctx, t("rpt.sub_per_class"))
        rows = [[t("rpt.col_class"), "Prec.", "Recall", "F1", "Support"]]
        for cls, m in class_rows.items():
            rows.append([escape(str(cls)), f"{m.get('precision', 0):.4f}",
                         f"{m.get('recall', 0):.4f}",
                         f"{m.get('f1-score', 0):.4f}",
                         _grp(m.get('support', 0))])
        _table(story, styles, ctx, t("rpt.cap_per_class"), rows,
               [0.28, 0.17, 0.17, 0.17, 0.21], num_cols=[1, 2, 3, 4])
        rendered_any = True

    if not rendered_any:
        story.append(Paragraph(t("rpt.no_extra_diagnostics"), styles["body"]))


# ─── 7. Catatan Metodologis ───────────────────────────────────────────────

def _section_7_metodologi(story, styles, ctx):
    t = ctx["_t"]
    _section(story, styles, ctx, t("rpt.sec_methodology"))
    notes = []
    if ctx["is_eve"]:
        notes.append(t("rpt.note_semantics_eve"))
        notes.append(t("rpt.note_label_origin_eve"))
        al = ctx["anti_leakage"]
        if al:
            # Potongan-potongan ini SEMUANYA dari katalog, jadi menggabungnya
            # tidak dapat mencampur bahasa.
            parts = []
            if al.get("group_split"):
                parts.append(t("rpt.leak_group_split", value=al["group_split"]))
            if al.get("pipeline_scaling"):
                parts.append(t("rpt.leak_pipeline_scaling"))
            if al.get("dual_holdout"):
                parts.append(t("rpt.leak_dual_holdout"))
            if al.get("forbidden_feature_guard"):
                parts.append(t("rpt.leak_forbidden_guard"))
            if parts:
                notes.append(t("rpt.note_antileak", parts="; ".join(parts)))
    else:
        notes.append(t("rpt.note_semantics_hikari"))
        notes.append(t("rpt.note_label_origin_hikari"))
        notes.append(t("rpt.note_antileak_hikari"))

    b = ctx["breakdown"]
    if b and b["attack_share"] is not None:
        notes.append(t("rpt.note_class_balance", share=_pct(b["attack_share"]),
                       attacks=_grp(b["attack_total"]),
                       normals=_grp(b["normal_total"])))

    algo = (ctx["algorithm"] or "").lower()
    if "svc" in algo or "svm" in algo:
        notes.append(t("rpt.note_svc_limit"))
    if ctx["runtime_warning"]:
        notes.append(escape(str(ctx["runtime_warning"])))

    for n in notes:
        story.append(Paragraph(n, styles["body"]))


# ─── 8. Reproducibility ───────────────────────────────────────────────────

def _section_8_reproducibility(story, styles, ctx):
    t = ctx["_t"]
    na = t("rpt.na")
    _section(story, styles, ctx, t("rpt.sec_reproducibility"))
    story.append(Paragraph(t("rpt.repro_intro"), styles["body"]))
    # Seed dibaca dari parameter yang TERCATAT untuk run ini. Menuliskan "42"
    # apa adanya akan berbohong pada run eksplorasi yang mengubah seed, justru
    # pada baris yang menjadi dasar klaim dapat-diulang.
    seed = (ctx["params_used"] or {}).get("random_state")
    if seed is None:
        # Tidak tercatat: tampilkan nilai bawaan platform, tetap lewat kunci
        # yang sama supaya kalimatnya tidak bercabang.
        seed_text = t("rpt.seed_locked", seed=42)
    elif "random_state" in set(ctx["params_changed"] or []):
        base = (ctx["params_locked"] or {}).get("random_state", 42)
        seed_text = t("rpt.seed_adjusted", seed=seed, base=base)
    else:
        seed_text = t("rpt.seed_locked", seed=seed)

    rows = [
        [t("rpt.col_item"), t("rpt.col_value")],
        ["Dataset SHA-256", escape(str(ctx["dataset_hash"]))],
        [t("rpt.lbl_seed"), seed_text],
        ["Python", _none_or(ctx["python_version"], na)],
        ["scikit-learn", _none_or(ctx["sklearn_version"], na)],
        ["pandas / numpy", f"{_none_or(ctx['pandas_version'], na)} / {_none_or(ctx['numpy_version'], na)}"],
        ["Platform", escape(_none_or(ctx["platform_str"], na))],
        ["Docker", t("rpt.yes") if ctx["is_docker"]
         else (t("rpt.no") if ctx["is_docker"] is False
               else t("rpt.not_recorded"))],
    ]
    if ctx["wall_clock"]:
        rows.append([t("rpt.lbl_wall_clock"), ctx["wall_clock"]])
    _table(story, styles, ctx, t("rpt.cap_repro"), rows, [0.36, 0.64])
    story.append(Paragraph(t("rpt.repro_how"), styles["body"]))
    if _run_mode.is_exploration(ctx["run_mode"]):
        # Run eksplorasi TETAP dapat diulang (parameternya tercatat), tetapi
        # bukan dasar klaim replikasi paper. Dua hal berbeda, dikatakan terpisah.
        story.append(Paragraph(t("rpt.repro_exploration"), styles["body"]))


# ─── Chart render helpers (sized for one column, paper-figure look) ───────

def _chart_axes(ax) -> None:
    ax.tick_params(colors=_C_TEXT, labelsize=7)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#888888")
        ax.spines[s].set_linewidth(0.6)


def _render_network_confusion_matrix(b: dict, t) -> io.BytesIO:
    """Confusion matrix labeled in network terms, drawn as a Blues heatmap.
    Built from the breakdown dict. Rows=Aktual, cols=Prediksi, order
    [Normal, Serangan]."""
    an, nn = b["attack_name"], b["normal_name"]
    grid = np.array([[b["tn"], b["fp"]], [b["fn"], b["tp"]]], dtype=float)
    quad_labels = [[t("rpt.cm_tn"), t("rpt.cm_fp")],
                   [t("rpt.cm_fn"), t("rpt.cm_tp")]]

    fig, ax = plt.subplots(figsize=(3.4, 2.9))
    im = ax.imshow(grid, cmap="Blues", vmin=0, vmax=max(grid.max(), 1))
    for i in range(2):
        for j in range(2):
            dark = grid[i][j] > grid.max() * 0.55
            color = "white" if dark else _C_TEXT
            ax.text(j, i - 0.12, f"{int(grid[i][j]):,}".replace(",", "."),
                    ha="center", va="center", fontsize=10, fontweight="bold",
                    color=color)
            ax.text(j, i + 0.2, quad_labels[i][j], ha="center", va="center",
                    fontsize=6.5, color=color)
    ax.set_xticks([0, 1])
    ax.set_yticks([0, 1])
    ax.set_xticklabels([t("rpt.cm_predicted", name=nn),
                        t("rpt.cm_predicted", name=an)], fontsize=7)
    ax.set_yticklabels([t("rpt.cm_actual", name=nn),
                        t("rpt.cm_actual", name=an)],
                       fontsize=7, rotation=90, va="center")
    ax.tick_params(length=0, colors=_C_TEXT)
    for spine in ax.spines.values():
        spine.set_visible(False)
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.ax.tick_params(labelsize=6)
    cbar.outline.set_visible(False)
    fig.tight_layout()
    return _save(fig)


def _render_roc_curve(metrics: dict, t) -> io.BytesIO:
    fig, ax = plt.subplots(figsize=(3.4, 3.0))
    if "roc_curve" in metrics:
        roc = metrics["roc_curve"]
        fpr, tpr = roc.get("fpr"), roc.get("tpr")
        if isinstance(fpr, list) and isinstance(tpr, list) and fpr and tpr:
            ax.plot(fpr, tpr, label=f"ROC (AUC = {metrics.get('roc_auc', 0):.4f})",
                    linewidth=1.6, color=_C_BLUE)
        elif isinstance(fpr, dict):
            for cls_name in fpr:
                ax.plot(fpr[cls_name], tpr[cls_name], label=str(cls_name), linewidth=1.2)
    elif "roc_curves_per_class" in metrics:
        for cls_name, curve in metrics["roc_curves_per_class"].items():
            ax.plot(curve["fpr"], curve["tpr"], label=str(cls_name), linewidth=1.2)
    ax.plot([0, 1], [0, 1], linestyle="--", linewidth=0.9, color=_C_GREY,
            label=t("rpt.chart_random_guess"))
    ax.set_xlabel("False Positive Rate", fontsize=7.5, color=_C_TEXT)
    ax.set_ylabel("True Positive Rate (Recall)", fontsize=7.5, color=_C_TEXT)
    ax.set_title(t("rpt.chart_roc_title",
                   auc=f"{metrics.get('roc_auc', 0):.4f}"),
                 color=_C_TEXT, fontsize=8)
    ax.legend(loc="lower right", frameon=False, fontsize=6.5)
    ax.grid(True, color=_C_GRID, linewidth=0.5)
    _chart_axes(ax)
    fig.tight_layout()
    return _save(fig)


def _render_feature_importance(feature_importance: list[dict], t) -> io.BytesIO:
    n_total = len(feature_importance)
    fi = feature_importance[:20]
    fig, ax = plt.subplots(figsize=(3.6, max(2.4, len(fi) * 0.2 + 0.8)))
    ax.barh([item["feature"] for item in reversed(fi)],
            [item["importance"] for item in reversed(fi)],
            color=_C_BLUE, height=0.7)
    ax.set_xlabel("Importance", fontsize=7.5, color=_C_TEXT)
    title = (t("rpt.chart_fi_title_total", shown=len(fi), total=n_total)
             if n_total > len(fi)
             else t("rpt.chart_fi_title", shown=len(fi)))
    ax.set_title(title, color=_C_TEXT, fontsize=8)
    ax.grid(True, axis="x", color=_C_GRID, linewidth=0.5)
    ax.set_axisbelow(True)
    _chart_axes(ax)
    ax.tick_params(axis="y", labelsize=6.3)
    fig.tight_layout()
    return _save(fig)


def _render_learning_curve(lc: dict) -> io.BytesIO:
    train_sizes = lc["train_sizes"]
    train_mean = lc["train_scores_mean"]
    train_std = lc.get("train_scores_std", [0] * len(train_sizes))
    val_mean = lc.get("val_scores_mean", lc.get("test_scores_mean", []))
    val_std = lc.get("val_scores_std", [0] * len(train_sizes))
    fig, ax = plt.subplots(figsize=(3.6, 2.6))
    ax.fill_between(train_sizes, [m - s for m, s in zip(train_mean, train_std)],
                    [m + s for m, s in zip(train_mean, train_std)], alpha=0.15, color=_C_BLUE)
    if val_mean:
        ax.fill_between(train_sizes, [m - s for m, s in zip(val_mean, val_std)],
                        [m + s for m, s in zip(val_mean, val_std)], alpha=0.15, color=_C_ORANGE)
    ax.plot(train_sizes, train_mean, 'o-', color=_C_BLUE, label="Training", linewidth=1.4, markersize=3)
    if val_mean:
        ax.plot(train_sizes, val_mean, 'o-', color=_C_ORANGE, label="Validation", linewidth=1.4, markersize=3)
    ax.set_xlabel("Training Set Size", fontsize=7.5, color=_C_TEXT)
    ax.set_ylabel("F1 Score (Weighted)", fontsize=7.5, color=_C_TEXT)
    ax.set_title("Learning Curve", color=_C_TEXT, fontsize=8)
    ax.legend(loc="lower right", frameon=False, fontsize=6.5)
    ax.grid(True, color=_C_GRID, linewidth=0.5)
    _chart_axes(ax)
    fig.tight_layout()
    return _save(fig)


def _save(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=200, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf
