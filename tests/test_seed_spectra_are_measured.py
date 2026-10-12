"""Guard: the packaged seed ships only measured spectra, each with provenance."""

from __future__ import annotations

import json
import sqlite3

import numpy as np

from mmfdb.store.database_resolver import source_database_path

#: Fewest points a shipped spectrum may have. A fluorophore band is 25-40 nm
#: FWHM and a usable curve spans >= 120 nm (about four FWHM); at the coarsest
#: sampling that still resolves the band (<= 5 nm) that is 24 points, so 50
#: leaves a margin of two. The 3-point placeholders the seed once carried
#: (24 bytes) cannot trace a band, let alone integrate an overlap.
MIN_POINTS = 50

PROVENANCE_KEYS = {"source", "source_ref", "retrieved_at", "licence", "reference_database"}


def _rows():
    conn = sqlite3.connect(f"file:{source_database_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT p.chromophore_name, s.spectrum_type, s.wavelengths, s.intensity_values, s.details "
            "FROM spectra s JOIN probes p USING (probe_id) WHERE s.deleted_at IS NULL"
        ).fetchall()
    finally:
        conn.close()


def test_seed_has_spectra():
    assert _rows(), "the seed lost all its spectra"


def test_no_shipped_spectrum_is_a_placeholder():
    for row in _rows():
        wl = np.frombuffer(row["wavelengths"], dtype=np.float64)
        inten = np.frombuffer(row["intensity_values"], dtype=np.float64)
        label = f"{row['chromophore_name']} {row['spectrum_type']}"
        assert wl.size == inten.size >= MIN_POINTS, f"{label}: {wl.size} points"
        assert np.all(np.diff(wl) > 0), f"{label}: wavelengths not increasing"
        assert np.isfinite(inten).all() and inten.max() > 0, label


def test_every_shipped_spectrum_has_provenance():
    for row in _rows():
        label = f"{row['chromophore_name']} {row['spectrum_type']}"
        assert row["details"], f"{label}: no provenance"
        provenance = json.loads(row["details"])
        missing = PROVENANCE_KEYS - {k for k, v in provenance.items() if v}
        assert not missing, f"{label}: provenance lacks {sorted(missing)}"


def test_curated_probe_without_measurement_carries_no_spectrum():
    conn = sqlite3.connect(f"file:{source_database_path()}?mode=ro", uri=True)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM spectra s JOIN probes p USING (probe_id) "
            "WHERE p.chromophore_name = '2-aminopurine'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_migration_replaces_placeholders_of_an_installed_database(tmp_path):
    """A database installed from an older seed gets measured spectra, not 3 points."""
    import shutil

    from mmfdb.repository import MFDatabase
    from mmfdb.schema.schema import set_schema_version

    path = tmp_path / "installed.db"
    shutil.copy2(source_database_path(), path)
    conn = sqlite3.connect(path)
    triple = np.array([455.0, 495.0, 535.0]).tobytes()
    conn.execute(
        "UPDATE spectra SET wavelengths = ?, intensity_values = ?, details = 'Curated seed spectrum'",
        (triple, triple),
    )
    # A spectrum a user stored since is not a placeholder and must survive.
    conn.execute(
        "INSERT INTO probes (chromophore_name) VALUES ('my dye')",
    )
    conn.execute(
        "INSERT INTO spectra (probe_id, spectrum_type, wavelengths, intensity_values, details) "
        "SELECT probe_id, 'emission', ?, ?, 'mine' FROM probes WHERE chromophore_name = 'my dye'",
        (triple, triple),
    )
    set_schema_version(conn, 50)
    conn.commit()
    conn.close()

    MFDatabase(path).close()

    conn = sqlite3.connect(path)
    rows = {
        (n, t): len(w) // 8
        for n, t, w in conn.execute(
            "SELECT p.chromophore_name, s.spectrum_type, s.wavelengths "
            "FROM spectra s JOIN probes p USING (probe_id)"
        )
    }
    conn.close()
    assert rows[("Alexa488", "emission")] >= MIN_POINTS
    assert rows[("my dye", "emission")] == 3
    assert not any(name == "2-aminopurine" for name, _ in rows)
    assert all(n >= MIN_POINTS for (name, _), n in rows.items() if name != "my dye")
