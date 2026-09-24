"""
Tanda hidup worker: apakah run yang sedang berjalan masih DIKERJAKAN.

Progres pipeline hanya bergerak di batas tahap. Satu tahap berat, misalnya
``fit()`` Random Forest pada data besar, dapat berlangsung puluhan menit tanpa
satu laporan pun, sehingga "bar tidak bergerak" tidak dapat membedakan tahap
berat yang sehat dari proses yang macet atau worker yang sudah mati. Modul ini
menjawab pertanyaan itu dari PROSESNYA, bukan dari bar:

* :class:`Heartbeat` adalah thread kecil di dalam task Celery yang setiap
  :data:`HEARTBEAT_INTERVAL_S` detik menulis waktu CPU dan RAM proses ke kunci
  Redis tersendiri ber-TTL. Kunci yang hilang berarti tidak ada lagi yang
  menulisnya: workernya mati.
* :func:`classify_liveness` adalah fungsi murni yang menerjemahkan payload itu
  menjadi satu keadaan untuk UI.
* :func:`read_heartbeats` dibaca UI dan penyapu stale.

Heartbeat adalah PENGAMATAN MURNI. Ia tidak menyentuh argumen, data, thread
pool, maupun lingkungan komputasi pipeline, dan setiap galatnya ditelan: tanda
hidup yang gagal ditulis tidak pernah boleh menggagalkan run.

Kuncinya TIDAK ditulis ke meta ``PROGRESS`` Celery. Meta itu ditulis thread
utama setiap kali tahap berganti; dua thread yang menulis ke tempat yang sama
akan saling menimpa nama tahapnya.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Jarak antartanda hidup.
HEARTBEAT_INTERVAL_S = 15
#: Umur kunci Redis. Empat kali interval: satu atau dua tulisan yang terlambat
#: (GIL sibuk, Redis tersendat) tidak membuat worker yang hidup tampak mati.
HEARTBEAT_TTL_S = HEARTBEAT_INTERVAL_S * 4
#: Tanpa tanda hidup selama ini, run RUNNING dianggap ditinggal worker dan
#: ditandai FAILED oleh penyapu stale.
HEARTBEAT_STALE_MINUTES = 10
#: CPU yang tidak bergerak selama ini membuat UI memperingatkan bahwa proses
#: tampak diam. Hanya peringatan: tidak ada yang dihentikan karenanya.
CPU_FLAT_MINUTES = 10
#: Pertambahan waktu CPU per interval yang dianggap "bekerja". Proses yang
#: benar-benar menunggu memakai jauh di bawah ini; satu inti yang sibuk memakai
#: sekitar HEARTBEAT_INTERVAL_S detik.
CPU_ACTIVE_DELTA_S = 1.0

#: Umur salinan "terakhir diketahui". Kunci hidup lenyap satu TTL setelah worker
#: mati, padahal justru sesudah itu UI perlu tahu RAM terakhirnya untuk
#: menebak sebabnya (kehabisan memori atau bukan), juga bila tidak ada yang
#: sedang membuka halamannya saat worker mati.
LAST_TTL_S = 24 * 3600

KEY_PREFIX = "ids:heartbeat:"
LAST_KEY_PREFIX = "ids:heartbeat-last:"

# Keadaan untuk UI.
LIVE_ACTIVE = "aktif"
LIVE_QUIET = "sunyi"
LIVE_SILENT = "tak_terdengar"
LIVE_UNKNOWN = "tak_diketahui"


def heartbeat_key(experiment_id: str) -> str:
    return f"{KEY_PREFIX}{experiment_id}"


def last_heartbeat_key(experiment_id: str) -> str:
    return f"{LAST_KEY_PREFIX}{experiment_id}"


# ── Pengukuran proses ─────────────────────────────────────────────────────

def _clock_ticks() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100


def _page_size() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, ValueError, OSError):
        return 4096


def parse_proc_stat(raw: str) -> Optional[tuple[int, int]]:
    """(ppid, tick CPU user+system) dari isi /proc/<pid>/stat, atau None.

    Nama proses (field 2) dapat memuat spasi dan kurung, jadi field sesudahnya
    dihitung dari ')' TERAKHIR, bukan dengan memecah seluruh baris.
    """
    fields = raw[raw.rfind(")") + 2:].split()
    try:
        return int(fields[1]), int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return None


def _proc_stat(pid: int) -> Optional[tuple[int, float]]:
    """(ppid, detik CPU user+system) proses ``pid``, atau None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("ascii", "replace")
    except OSError:
        return None
    parsed = parse_proc_stat(raw)
    if parsed is None:
        return None
    return parsed[0], parsed[1] / _clock_ticks()


def _proc_rss_mb(pid: int) -> Optional[float]:
    try:
        with open(f"/proc/{pid}/statm", "rb") as fh:
            resident_pages = int(fh.read().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return resident_pages * _page_size() / (1024 * 1024)


def _descendants(root: int) -> list[int]:
    """PID turunan ``root`` (proses anak joblib/loky), dari /proc."""
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    parent_of = {}
    for pid in pids:
        st = _proc_stat(pid)
        if st is not None:
            parent_of[pid] = st[0]
    found, frontier = [], [root]
    while frontier:
        cur = frontier.pop()
        for pid, ppid in parent_of.items():
            if ppid == cur and pid not in found:
                found.append(pid)
                frontier.append(pid)
    return found


def sample_process(pid: Optional[int] = None) -> dict:
    """Waktu CPU kumulatif (detik) dan RSS (MB) proses ini BESERTA turunannya.

    Turunan ikut dihitung karena joblib/loky menjalankan sebagian kerja di
    proses anak; hanya menghitung proses induk akan membuat run yang sibuk di
    anak-anaknya tampak diam. Di luar Linux (tanpa /proc), jatuh ke
    ``os.times()`` untuk proses ini saja dan RSS tidak diketahui.
    """
    pid = os.getpid() if pid is None else pid
    own = _proc_stat(pid)
    if own is None:
        t = os.times()
        return {"cpu_seconds": float(t.user + t.system), "rss_mb": None,
                "processes": 1}
    cpu = own[1]
    rss = _proc_rss_mb(pid)
    kids = _descendants(pid)
    for kid in kids:
        st = _proc_stat(kid)
        if st is not None:
            cpu += st[1]
        kid_rss = _proc_rss_mb(kid)
        if kid_rss is not None and rss is not None:
            rss += kid_rss
    return {"cpu_seconds": cpu, "rss_mb": rss, "processes": 1 + len(kids)}


# ── Penulis ───────────────────────────────────────────────────────────────

_CLIENTS: dict = {}


def _redis_client(url: str):
    """Satu klien per URL. Halaman progres membaca tanda hidup pada setiap
    putaran polling; klien baru setiap kali berarti koneksi baru setiap kali.
    Batas waktunya pendek supaya UI tidak pernah menggantung pada Redis."""
    client = _CLIENTS.get(url)
    if client is None:
        import redis
        client = redis.Redis.from_url(url, socket_connect_timeout=1.0,
                                      socket_timeout=1.0)
        _CLIENTS[url] = client
    return client


def redis_sink(url: str) -> Callable[[str, Optional[dict]], None]:
    """Sink yang menulis payload sebagai JSON ke dua kunci: kunci hidup ber-TTL
    pendek (adanya = worker hidup) dan salinan terakhir ber-TTL panjang.
    ``payload=None`` (run berakhir wajar) menghapus keduanya."""
    import json

    def _write(experiment_id: str, payload: Optional[dict]) -> None:
        client = _redis_client(url)
        if payload is None:
            client.delete(heartbeat_key(experiment_id),
                          last_heartbeat_key(experiment_id))
            return
        body = json.dumps(payload)
        client.set(heartbeat_key(experiment_id), body, ex=HEARTBEAT_TTL_S)
        client.set(last_heartbeat_key(experiment_id), body, ex=LAST_TTL_S)
    return _write


class Heartbeat:
    """Thread daemon yang menulis tanda hidup berkala untuk satu run.

    ``sink(experiment_id, payload)`` menulis payload; ``payload=None`` berarti
    hapus. ``sampler()`` mengembalikan ``{"cpu_seconds", "rss_mb", ...}``.
    Keduanya dapat disuntik supaya dapat diuji tanpa Redis dan tanpa /proc.

    Tidak menyimpan referensi ke data atau pipeline apa pun: yang diketahuinya
    hanya id eksperimen dan angka proses.
    """

    def __init__(self, experiment_id: str, sink: Callable[[str, Optional[dict]], None],
                 *, interval_s: float = HEARTBEAT_INTERVAL_S,
                 sampler: Callable[[], dict] = sample_process,
                 clock: Callable[[], float] = time.time):
        self.experiment_id = experiment_id
        self._sink = sink
        self._interval = float(interval_s)
        self._sampler = sampler
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None
        self._last_cpu: Optional[float] = None
        self._last_ts: Optional[float] = None
        self._last_active_ts: Optional[float] = None
        self.beats = 0

    # Satu tanda hidup. Dipisah dari loop supaya dapat diuji langsung.
    def beat(self) -> Optional[dict]:
        try:
            now = self._clock()
            sample = self._sampler() or {}
            cpu = sample.get("cpu_seconds")
            cpu_percent = None
            if self._started_at is None:
                self._started_at = now
                self._last_active_ts = now
            if cpu is not None and self._last_cpu is not None and self._last_ts is not None:
                dt = max(now - self._last_ts, 1e-6)
                delta = cpu - self._last_cpu
                cpu_percent = max(0.0, delta / dt * 100.0)
                if delta >= CPU_ACTIVE_DELTA_S * dt / self._interval:
                    self._last_active_ts = now
            if cpu is not None:
                self._last_cpu = cpu
            self._last_ts = now
            payload = {
                "ts": now,
                "pid": os.getpid(),
                "cpu_seconds": cpu,
                "cpu_percent": cpu_percent,
                "cpu_idle_s": max(0.0, now - self._last_active_ts),
                "rss_mb": sample.get("rss_mb"),
                "processes": sample.get("processes"),
                "interval_s": self._interval,
            }
            self._sink(self.experiment_id, payload)
            self.beats += 1
            return payload
        except Exception:
            logger.debug("heartbeat %s gagal ditulis", self.experiment_id,
                         exc_info=True)
            return None

    def _loop(self) -> None:
        while True:
            self.beat()
            if self._stop.wait(self._interval):
                return

    def start(self) -> "Heartbeat":
        try:
            self._thread = threading.Thread(
                target=self._loop, name=f"heartbeat-{self.experiment_id[:8]}",
                daemon=True)
            self._thread.start()
        except Exception:
            logger.warning("heartbeat %s tidak dapat dimulai", self.experiment_id,
                           exc_info=True)
            self._thread = None
        return self

    def stop(self) -> None:
        """Hentikan thread lalu hapus kuncinya. Tidak pernah melempar."""
        try:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=self._interval + 5)
        except Exception:
            logger.debug("heartbeat %s: join gagal", self.experiment_id,
                         exc_info=True)
        try:
            self._sink(self.experiment_id, None)
        except Exception:
            logger.debug("heartbeat %s: kunci tidak dapat dihapus",
                         self.experiment_id, exc_info=True)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ── Pembaca ───────────────────────────────────────────────────────────────

def read_heartbeats(experiment_ids, url: str) -> Optional[dict]:
    """``{experiment_id: payload | None}`` dari Redis, atau None bila tidak
    dapat DINILAI.

    None (bukan dict kosong) berarti "tidak tahu": Redis tidak terjangkau,
    atau Redis baru saja menyala sehingga kunci worker yang hidup belum sempat
    ditulis ulang. Pemanggil tidak boleh menyimpulkan "mati" dari None.
    """
    ids = list(experiment_ids)
    try:
        client = _redis_client(url)
        uptime = int(client.info("server").get("uptime_in_seconds", 0))
        if uptime < HEARTBEAT_TTL_S * 2:
            return None
        raw = client.mget([heartbeat_key(e) for e in ids]) if ids else []
    except Exception:
        logger.debug("heartbeat tidak dapat dibaca", exc_info=True)
        return None
    return {eid: _decode(value) for eid, value in zip(ids, raw)}


def _decode(value) -> Optional[dict]:
    import json
    if value is None:
        return None
    try:
        payload = json.loads(value)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_last_heartbeat(experiment_id: str, url: str) -> Optional[dict]:
    """Tanda hidup terakhir yang pernah ditulis untuk run ini, atau None."""
    try:
        return _decode(_redis_client(url).get(last_heartbeat_key(experiment_id)))
    except Exception:
        logger.debug("heartbeat terakhir tidak dapat dibaca", exc_info=True)
        return None


# ── Klasifikasi (murni) ───────────────────────────────────────────────────

def classify_liveness(status: str, heartbeat: Optional[dict], known: bool,
                      *, running_for_s: Optional[float] = None) -> str:
    """Satu keadaan tanda hidup untuk UI.

    * ``known`` False (mode sinkron, tanpa task Celery, Redis tak terjangkau)
      atau status bukan RUNNING → :data:`LIVE_UNKNOWN`: tampilan seperti biasa.
    * Heartbeat tidak ada → :data:`LIVE_SILENT`, KECUALI run baru saja mulai
      (belum sempat satu TTL): masih :data:`LIVE_UNKNOWN`.
    * CPU diam ≥ :data:`CPU_FLAT_MINUTES` → :data:`LIVE_QUIET`.
    * Selain itu → :data:`LIVE_ACTIVE`.
    """
    if not known or status != "RUNNING":
        return LIVE_UNKNOWN
    if not heartbeat:
        if running_for_s is not None and running_for_s < HEARTBEAT_TTL_S:
            return LIVE_UNKNOWN
        return LIVE_SILENT
    idle = heartbeat.get("cpu_idle_s")
    if isinstance(idle, (int, float)) and idle >= CPU_FLAT_MINUTES * 60:
        return LIVE_QUIET
    return LIVE_ACTIVE
