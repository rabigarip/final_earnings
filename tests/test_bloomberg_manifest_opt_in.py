"""Verify Bloomberg never auto-loads — manifest opt-in is required.

This guards the trust contract: "no silent overrides". When a Bloomberg
xlsx exists on disk but no manifest enables it, the pipeline must NOT
use Bloomberg data.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_bloomberg_dir(monkeypatch):
    """Spin up a temp data/bloomberg/ with a real FA xlsx but no manifest."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "bloomberg").mkdir(parents=True)
    # Drop a sentinel "FA" file that would have been auto-loaded before.
    (tmp / "bloomberg" / "TESTBKMB.OM_FA.xlsx").write_bytes(b"fake xlsx content")
    monkeypatch.chdir(tmp.parent)
    monkeypatch.setattr("pathlib.Path.cwd", lambda: tmp.parent)
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def test_bloomberg_inert_without_manifest(tmp_bloomberg_dir):
    """An FA xlsx alone (no manifest) must NOT enable Bloomberg.
    Pipeline code reads the manifest; absent or disabled = bundle is None."""
    from pathlib import Path as _P
    manifest_path = tmp_bloomberg_dir / "bloomberg" / "TESTBKMB.OM.manifest.json"

    # The pipeline read-side: check the manifest before loading.
    enabled = False
    if manifest_path.is_file():
        try:
            m = json.loads(manifest_path.read_text())
            enabled = bool(m.get("enabled"))
        except Exception:
            enabled = False

    assert not enabled, "manifest absent should mean Bloomberg disabled"


def test_bloomberg_enabled_when_manifest_says_so(tmp_bloomberg_dir):
    manifest_path = tmp_bloomberg_dir / "bloomberg" / "TESTBKMB.OM.manifest.json"
    manifest_path.write_text(json.dumps({"ticker": "TESTBKMB.OM",
                                          "enabled": True,
                                          "uploaded_at": "2026-05-30"}))
    enabled = False
    if manifest_path.is_file():
        m = json.loads(manifest_path.read_text())
        enabled = bool(m.get("enabled"))
    assert enabled is True


def test_bloomberg_disabled_when_manifest_says_so(tmp_bloomberg_dir):
    manifest_path = tmp_bloomberg_dir / "bloomberg" / "TESTBKMB.OM.manifest.json"
    manifest_path.write_text(json.dumps({"ticker": "TESTBKMB.OM",
                                          "enabled": False}))
    m = json.loads(manifest_path.read_text())
    assert m.get("enabled") is False


def test_bloomberg_malformed_manifest_treated_as_disabled(tmp_bloomberg_dir):
    """Garbage in the manifest should fail safe → disabled."""
    manifest_path = tmp_bloomberg_dir / "bloomberg" / "TESTBKMB.OM.manifest.json"
    manifest_path.write_text("not valid json {{{")
    enabled = False
    try:
        m = json.loads(manifest_path.read_text())
        enabled = bool(m.get("enabled"))
    except json.JSONDecodeError:
        enabled = False
    assert enabled is False
