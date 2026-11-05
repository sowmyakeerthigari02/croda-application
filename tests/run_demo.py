"""Runs the dashboard on :8000 and the made-up employer site on :8001, for trying the flow by hand."""
import os
import tempfile
import threading

os.environ.setdefault("ALLOW_PRIVATE_URLS", "1")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="applypilot-demo-"))

import uvicorn

from tests.fixture_site import site

threading.Thread(
    target=lambda: uvicorn.Server(uvicorn.Config(site, port=8001, log_level="warning")).run(), daemon=True
).start()

from app.main import app

uvicorn.run(app, port=8000, log_level="warning")
