"""Pack a reviewed record folder into the single archive that is uploaded."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from .record import DepositRecord, _now, items_digest


def build_package(rec: DepositRecord, out_dir: str | Path) -> Path:
    """Write ``<out_dir>/<record>.zip`` (stored, one top folder) and record it.

    Stored rather than deflated: the payload is photon data that does not
    compress, and a stored archive can be checked and read member by member
    without inflating. The archive is tested after writing (every member's
    CRC); its size, MD5 (what the repository reports) and SHA-256 go into
    ``rec.package`` together with the item digest it was built from, so a
    later change to any item invalidates it.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{rec.name}.zip"
    tmp = target.with_suffix(".zip.tmp")
    files = sorted(p for p in rec.folder.rglob("*") if p.is_file())
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for p in files:
            z.write(p, f"{rec.name}/{p.relative_to(rec.folder).as_posix()}")
    with zipfile.ZipFile(tmp) as z:
        bad = z.testzip()
        if bad is not None:
            raise RuntimeError(f"{tmp}: member {bad} failed its CRC check")
    tmp.replace(target)
    md5, sha = hashlib.md5(), hashlib.sha256()
    with open(target, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            md5.update(block)
            sha.update(block)
    rec.package = {"path": str(target), "bytes": target.stat().st_size, "files": len(files),
                   "md5": md5.hexdigest(), "sha256": sha.hexdigest(),
                   "items_digest": items_digest(rec), "built_at": _now()}
    rec._note("package", path=str(target), md5=rec.package["md5"])
    return target
