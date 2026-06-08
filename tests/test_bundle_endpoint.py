"""Tests for /api/reports/{run_id}/bundle — the atomic zip download."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.api import download_report_bundle


@pytest.fixture
def tmp_outputs(tmp_path, monkeypatch):
    """Create a fake outputs directory with a pptx + provenance xlsx pair."""
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    pptx = out_dir / "TEST_20260530_120000_earnings_preview.pptx"
    xlsx = out_dir / "TEST_20260530_120000_earnings_preview.provenance.xlsx"
    pptx.write_bytes(b"PK\x03\x04" + b"fake pptx content padding" * 50)
    xlsx.write_bytes(b"PK\x03\x04" + b"fake xlsx content padding" * 30)
    monkeypatch.setattr("src.config.report_output_dir", lambda: out_dir)
    return out_dir, pptx, xlsx


def test_bundle_contains_both_files(tmp_outputs):
    out_dir, pptx, xlsx = tmp_outputs
    with patch("src.storage.db.load_run", return_value={
        "id": "run-test",
        "memo_path": pptx.name,
    }):
        response = download_report_bundle("run-test")
    # FastAPI StreamingResponse — read the body via the .body_iterator.
    chunks = []
    import asyncio
    async def _gather():
        async for chunk in response.body_iterator:
            chunks.append(chunk)
    asyncio.run(_gather())
    body = b"".join(chunks)
    assert body[:4] == b"PK\x03\x04", "zip magic header expected"
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = set(zf.namelist())
    assert pptx.name in names, "deck must be in the bundle"
    assert xlsx.name in names, "provenance must be in the bundle"


def test_bundle_works_when_xlsx_missing(tmp_outputs):
    """When provenance.xlsx is absent (rare), the zip still has the pptx
    rather than failing the whole download."""
    out_dir, pptx, xlsx = tmp_outputs
    xlsx.unlink()   # remove the xlsx
    with patch("src.storage.db.load_run", return_value={
        "id": "run-test",
        "memo_path": pptx.name,
    }):
        response = download_report_bundle("run-test")
    import asyncio
    chunks = []
    async def _gather():
        async for chunk in response.body_iterator:
            chunks.append(chunk)
    asyncio.run(_gather())
    body = b"".join(chunks)
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = set(zf.namelist())
    assert pptx.name in names
    assert xlsx.name not in names


def test_bundle_404_when_run_missing(tmp_outputs):
    from fastapi import HTTPException
    with patch("src.storage.db.load_run", return_value=None):
        with pytest.raises(HTTPException) as exc:
            download_report_bundle("nonexistent-run")
    assert exc.value.status_code == 404


def test_bundle_404_when_pptx_missing(tmp_outputs):
    from fastapi import HTTPException
    out_dir, pptx, xlsx = tmp_outputs
    pptx.unlink()
    with patch("src.storage.db.load_run", return_value={
        "id": "r", "memo_path": pptx.name,
    }):
        with pytest.raises(HTTPException) as exc:
            download_report_bundle("r")
    assert exc.value.status_code == 404


def test_bundle_filename_is_descriptive(tmp_outputs):
    out_dir, pptx, xlsx = tmp_outputs
    with patch("src.storage.db.load_run", return_value={
        "id": "r", "memo_path": pptx.name,
    }):
        response = download_report_bundle("r")
    cd = response.headers.get("content-disposition", "")
    assert ".zip" in cd
    assert "bundle" in cd.lower()
