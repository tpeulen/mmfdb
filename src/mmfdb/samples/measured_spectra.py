"""Measured reference spectra for the curated probes.

The curated probes (Alexa488, Cy3, ...) carry spectra copied from the shipped
reference ``spectra.db``; ``data/curated_spectra_sources.json`` names the
reference probe behind each and the licence of its source. A curated probe
with no measured counterpart carries no spectrum: a fabricated curve (a
Gaussian, a handful of points) is computed from by the Förster calculator as
if it had been measured.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

_SOURCES = Path(__file__).resolve().parent.parent / "data" / "curated_spectra_sources.json"

#: Size in bytes of the 3-point float64 placeholder spectra older seeds carried.
PLACEHOLDER_BYTES = 24


def load_spectra_sources() -> dict:
    """Load the map from curated probes to their measured reference spectra.

    Returns
    -------
    dict
        ``min_points``, ``licences`` and ``probes`` of ``curated_spectra_sources.json``.
    """
    with open(_SOURCES, "r", encoding="utf-8") as f:
        return json.load(f)


def attach_measured_spectra(conn: sqlite3.Connection, *, only_missing: bool = False) -> int:
    """Store the measured spectra of the curated probes, with provenance.

    Each spectrum records its provenance as JSON in ``details`` (source,
    source_ref, retrieval time, licence, the reference probe and spectrum
    type it was taken from); the probe row records source, source_ref and
    retrieval time. The caller owns the transaction.

    Parameters
    ----------
    conn : sqlite3.Connection
        The MMFDB connection (rows are addressed by name).
    only_missing : bool, default False
        False replaces every spectrum of every live probe (seed generation:
        a curated probe absent from the map ends with none). True touches only
        mapped probes that currently have no spectrum, leaving anything else a
        user stored alone (schema migration of an installed database).

    Returns
    -------
    int
        Number of spectra stored.
    """
    from mmfdb.store.database_resolver import _default_reference_spectra_path

    config = load_spectra_sources()
    min_points = int(config["min_points"])
    reference = sqlite3.connect(str(_default_reference_spectra_path()))
    reference.row_factory = sqlite3.Row
    stored = 0
    try:
        rows = conn.execute(
            "SELECT probe_id, chromophore_name FROM probes WHERE deleted_at IS NULL"
        ).fetchall()
        for row in rows:
            name, probe_id = row[1], int(row[0])
            entry = config["probes"].get(name)
            if only_missing:
                if entry is None or conn.execute(
                    "SELECT 1 FROM spectra WHERE probe_id = ? AND deleted_at IS NULL", (probe_id,)
                ).fetchone():
                    continue
            else:
                conn.execute("DELETE FROM spectra WHERE probe_id = ?", (probe_id,))
                if entry is None:
                    continue
            ref = reference.execute(
                "SELECT probe_id, source, source_ref, retrieved_at FROM probes "
                "WHERE chromophore_name = ? AND deleted_at IS NULL AND source = ?",
                (entry["reference"], entry["source"]),
            ).fetchone()
            if ref is None:
                raise LookupError(
                    f"reference spectra.db has no probe {entry['reference']!r} "
                    f"from {entry['source']!r} for curated probe {name!r}"
                )
            for kind, ref_kind in entry["spectra"].items():
                src = reference.execute(
                    "SELECT wavelengths, intensity_values FROM spectra "
                    "WHERE probe_id = ? AND spectrum_type = ? AND deleted_at IS NULL",
                    (ref["probe_id"], ref_kind),
                ).fetchone()
                wl = np.frombuffer(src["wavelengths"], dtype=np.float64)
                inten = np.frombuffer(src["intensity_values"], dtype=np.float64)
                if wl.size < min_points:
                    raise ValueError(f"{name} {kind}: {wl.size} points < {min_points}")
                provenance = {
                    "source": ref["source"],
                    "source_ref": ref["source_ref"],
                    "retrieved_at": ref["retrieved_at"],
                    "licence": "; ".join(
                        config["licences"][part] for part in entry["source"].split(",")
                    ),
                    "reference_probe": entry["reference"],
                    "reference_spectrum_type": ref_kind,
                    "reference_database": "spectra.db",
                }
                if ref_kind != kind:
                    provenance["note"] = f"{ref_kind} spectrum used as the {kind} band shape"
                conn.execute(
                    "INSERT INTO spectra (probe_id, spectrum_type, wavelengths, "
                    "intensity_values, wavelength_unit, intensity_unit, details) "
                    "VALUES (?, ?, ?, ?, 'nm', 'normalized', ?)",
                    (
                        probe_id,
                        kind,
                        wl.tobytes(),
                        (inten / float(np.max(inten))).tobytes(),
                        json.dumps(provenance, ensure_ascii=False, sort_keys=True),
                    ),
                )
                stored += 1
            conn.execute(
                "UPDATE probes SET source = ?, source_ref = ?, retrieved_at = ? WHERE probe_id = ?",
                (ref["source"], ref["source_ref"], ref["retrieved_at"], probe_id),
            )
    finally:
        reference.close()
    return stored
