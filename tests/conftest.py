import os
import socket
import tempfile
import threading
import time

os.environ["APP_ENV"] = "dev"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="applypilot-test-")
os.environ["ALLOW_PRIVATE_URLS"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import app
from tests.fixture_site import reset, site


@pytest.fixture(scope="session")
def fixture_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(site, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture(scope="session")
def client():
    with TestClient(app, headers={"X-ApplyPilot": "1"}) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_site():
    reset()
