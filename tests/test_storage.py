"""Exercises the real Supabase client library against a local stand-in for the Storage API."""
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request, Response

from app.storage import LocalStorage, StorageError, SupabaseStorage

api = FastAPI()
state = {"buckets": {}, "objects": {}, "auth": None}


@api.get("/storage/v1/bucket/{bucket}")
def get_bucket(bucket: str, request: Request):
    state["auth"] = request.headers.get("apikey")
    if bucket not in state["buckets"]:
        return Response('{"statusCode":"404","error":"Bucket not found","message":"Bucket not found"}',
                        status_code=400, media_type="application/json")
    return {"id": bucket, "name": bucket, "owner": "", "public": state["buckets"][bucket],
            "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-01T00:00:00Z",
            "file_size_limit": None, "allowed_mime_types": None}


@api.post("/storage/v1/bucket")
async def create_bucket(request: Request):
    body = await request.json()
    state["buckets"][body["id"]] = body.get("public", False)
    return {"name": body["id"]}


@api.post("/storage/v1/object/{bucket}/{key:path}")
async def upload(bucket: str, key: str, request: Request):
    if request.headers.get("content-type", "").startswith("multipart/"):
        form = await request.form()
        upload_file = next(v for v in form.values() if hasattr(v, "read"))
        data = await upload_file.read()
    else:
        data = await request.body()
    state["objects"][key] = data
    return {"Id": "1", "Key": f"{bucket}/{key}"}


@api.get("/storage/v1/object/{bucket}/{key:path}")
def download(bucket: str, key: str):
    if key not in state["objects"]:
        return Response('{"statusCode":"404","error":"not_found","message":"Object not found"}',
                        status_code=400, media_type="application/json")
    return Response(state["objects"][key], media_type="application/octet-stream")


@api.delete("/storage/v1/object/{bucket}")
async def remove(bucket: str, request: Request):
    body = await request.json()
    for key in body["prefixes"]:
        state["objects"].pop(key, None)
    return []


@pytest.fixture(scope="module")
def url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture(autouse=True)
def clean():
    state.update(buckets={}, objects={}, auth=None)


def test_supabase_round_trip_in_a_new_private_bucket(url):
    store = SupabaseStorage(url, "test-service-key", "applypilot")
    assert state["buckets"] == {"applypilot": False}, "the bucket must be created private"
    assert state["auth"] == "test-service-key"

    store.put("resumes/1/resume.pdf", b"%PDF-data", "application/pdf")
    assert store.get("resumes/1/resume.pdf") == b"%PDF-data"

    store.delete("resumes/1/resume.pdf")
    with pytest.raises(StorageError):
        store.get("resumes/1/resume.pdf")


def test_a_public_bucket_is_refused(url):
    state["buckets"]["applypilot"] = True
    with pytest.raises(RuntimeError, match="public"):
        SupabaseStorage(url, "test-service-key", "applypilot")


def test_local_storage_round_trip(tmp_path):
    store = LocalStorage(tmp_path)
    store.put("a/b.jpg", b"image", "image/jpeg")
    assert store.get("a/b.jpg") == b"image"
    store.delete("a/b.jpg")
    with pytest.raises(StorageError):
        store.get("a/b.jpg")


def test_local_storage_adds_little_to_the_path_length(tmp_path):
    store = LocalStorage(tmp_path / "files")
    key = "resumes/b9f66db3d4434f3b990026ff06cddaff/resume.pdf"
    store.put(key, b"pdf", "application/pdf")
    saved = next((tmp_path / "files").iterdir())
    assert saved.is_file() and len(saved.name) <= 20
    assert store.get(key) == b"pdf"
