"""A sample whose probe has a measured spectrum can be described (and so validated for export).

``add_spectrum`` stores the arrays as packed float64 bytes, but the sample description read
them with ``json.loads``: ``json`` guessed an encoding for the bytes and raised
``UnicodeDecodeError`` ('utf-32-be'), so ``mmfdb.samples.validate_export`` failed for any
sample with a measured donor or acceptor spectrum.
"""

from __future__ import annotations

import json

from mmfdb.repository import MFDatabase
from mmfdb.samples.sample_manager import get_sample_full_description


def _db(tmp_path) -> MFDatabase:
    db = MFDatabase(str(tmp_path / "t.db"))
    db.add_user("user_default", "Default User")
    db.add_sample("s1", description="with spectra", measured_by_user_id="user_default")
    return db


def test_a_probe_with_a_stored_spectrum_is_described(tmp_path):
    db = _db(tmp_path)
    try:
        probe = db.add_probe(None, name="Donor", category="organic_dye")
        db.add_spectrum(probe, "emission", [500.0, 510.0, 520.0], [0.1, 1.0, 0.2])
        db.add_sample_probe("s1", probe, "donor")
        described = get_sample_full_description(db, "s1")
    finally:
        db.close()
    spectra = described["probes"][0]["spectra"]["emission"]
    assert spectra["wavelengths"] == [500.0, 510.0, 520.0]
    assert spectra["intensity_values"] == [0.1, 1.0, 0.2]


def test_a_spectrum_written_as_json_text_still_reads(tmp_path):
    db = _db(tmp_path)
    try:
        probe = db.add_probe(None, name="Old", category="organic_dye")
        db.add_spectrum(probe, "absorption", [400.0, 410.0], [0.5, 1.0])
        db.conn.execute(
            "UPDATE spectra SET wavelengths = ?, intensity_values = ? WHERE probe_id = ?",
            (json.dumps([400.0, 410.0]), json.dumps([0.5, 1.0]), probe),
        )
        db.add_sample_probe("s1", probe, "donor")
        described = get_sample_full_description(db, "s1")
    finally:
        db.close()
    assert described["probes"][0]["spectra"]["absorption"]["wavelengths"] == [400.0, 410.0]
