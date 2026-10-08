"""
Titik masuk server: aplikasi Streamlit (ui/app.py) + rute HTTP milik platform.

Dijalankan dengan ``streamlit run ui/serve.py``. Streamlit mengenali objek
``st.App`` di berkas ini lalu menjalankannya sebagai aplikasi ASGI di port
yang sama (8501), dengan konfigurasi ``.streamlit/config.toml`` yang sama.
Halaman-halamannya tetap ``ui/app.py``; berkas ini hanya menambahkan rute
yang tidak dapat dibuat dari dalam skrip Streamlit:

* ``/ids-upload/...`` — unggahan dataset bertahap yang dapat dilanjutkan
  (lihat ``orchestrator/chunked_upload.py``).
* ``/ids-auth/...`` — memasang dan membuang cookie ``HttpOnly`` sesi login,
  supaya login bertahan saat halaman dimuat ulang (lihat
  ``orchestrator/login_session.py``).

Rute ditaruh di SINI, bukan di port lain, karena hanya port 8501 yang
diteruskan VPS ke server lab.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from orchestrator.chunked_upload import routes as upload_routes  # noqa: E402
from orchestrator.login_session import routes as session_routes  # noqa: E402

app = st.App(str(Path(__file__).resolve().parent / "app.py"),
             routes=upload_routes() + session_routes())
