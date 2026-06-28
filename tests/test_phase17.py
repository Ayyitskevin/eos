"""Phase 17 — production deploy + S3 object storage."""

import importlib
from pathlib import Path
from unittest.mock import MagicMock

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.object_store as object_store
import pytest


@pytest.fixture()
def s3_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("EOS_S3_ACCESS_KEY", "key")
    monkeypatch.setenv("EOS_S3_SECRET_KEY", "secret")
    for mod in (config, db, object_store, jobs):
        importlib.reload(mod)
    object_store.reset_client()
    config.ensure_dirs()
    db.migrate()
    yield
    object_store.reset_client()


def test_object_store_disabled_without_bucket(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("EOS_S3_BUCKET", raising=False)
    importlib.reload(config)
    importlib.reload(object_store)
    object_store.reset_client()
    assert not object_store.enabled()


def test_media_key_format(s3_env):
    key = object_store.media_key(
        studio_id="acme",
        gallery_id=42,
        sub="original",
        filename="abc.jpg",
    )
    assert key == "eos/media/acme/42/original/abc.jpg"


def test_upload_file(s3_env, tmp_path, monkeypatch):
    mock_client = MagicMock()
    monkeypatch.setattr(object_store, "get_client", lambda: mock_client)
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"data")
    assert object_store.upload_file(f, "eos/media/a/1/original/photo.jpg")
    mock_client.upload_file.assert_called_once()


def test_studio_storage_bytes(s3_env, monkeypatch):
    mock_client = MagicMock()
    mock_client.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Size": 1000}, {"Size": 2000}]}
    ]
    monkeypatch.setattr(object_store, "get_client", lambda: mock_client)
    assert object_store.studio_storage_bytes(studio_id="acme") == 3000