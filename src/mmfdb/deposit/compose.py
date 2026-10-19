"""Compose a record by hand: an empty record, data put into it, its Zenodo metadata.

A record does not have to come from a build script. Someone depositing a
handful of measurements makes one in four steps, and each is a function here
so the GUI and the command line do the same thing:

1. :func:`new_record` -- an empty record folder (``measurements/``,
   ``calibrations/``) with its review state beside it and a metadata template.
2. :func:`add_data` -- files or folders put into the record as one item, a
   measurement or a calibration of a group. Files are hard-linked when the
   source is on the same disk (no second copy of a gigabyte), copied otherwise;
   where they came from is kept in the review state, not shipped.
3. the Zenodo metadata, edited in :attr:`DepositRecord.metadata` and checked
   by :func:`zenodo_problems` -- the same check the gate applies, because the
   metadata is a reviewed item like any folder (:data:`METADATA_ITEM`).
4. :func:`~mmfdb.deposit.record.scan` -- the items a reviewer then judges.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path

from .record import DepositRecord, content_files, is_clutter

__all__ = ["ACCESS_RIGHTS", "LICENSES", "RELATIONS", "UPLOAD_TYPES", "add_data", "new_record",
           "template", "zenodo_problems", "valid_name"]

#: A record name is its folder name and the archive's: lower case, no spaces.
RECORD_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,99}$")
#: An item name is one folder: anything a file system and a URL accept unquoted.
ITEM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")

#: Zenodo's vocabularies (deposition metadata, REST API).
UPLOAD_TYPES = ("dataset", "software", "publication", "poster", "presentation", "image",
                "video", "lesson", "physicalobject", "other")
PUBLICATION_TYPES = ("annotationcollection", "book", "section", "conferencepaper",
                     "datamanagementplan", "article", "patent", "preprint", "deliverable",
                     "milestone", "proposal", "report", "softwaredocumentation",
                     "taxonomictreatment", "technicalnote", "thesis", "workingpaper", "other")
IMAGE_TYPES = ("figure", "plot", "drawing", "diagram", "photo", "other")
ACCESS_RIGHTS = ("open", "embargoed", "restricted", "closed")
LICENSES = ("cc-by-4.0", "cc0-1.0", "cc-by-sa-4.0", "cc-by-nc-4.0", "mit", "apache-2.0",
            "bsd-3-clause", "gpl-3.0-or-later")
RELATIONS = ("isSupplementTo", "isSupplementedBy", "isPartOf", "hasPart", "isDerivedFrom",
             "isSourceOf", "cites", "isCitedBy", "references", "isReferencedBy",
             "isNewVersionOf", "isPreviousVersionOf", "isDocumentedBy", "documents",
             "isIdenticalTo", "isAlternateIdentifier", "compiles", "isCompiledBy")
ORCID = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_name(name: str) -> str:
    """Why *name* cannot be a record name, or ``""`` when it can."""
    if RECORD_NAME.match(name or ""):
        return ""
    return ("a record name is 3-100 characters: lower-case letters, digits, '.', '_' or '-', "
            "starting with a letter or digit (it becomes the folder and the archive name)")


def template(title: str = "") -> dict:
    """The Zenodo metadata of a new record: every field a deposition needs, empty."""
    return {"title": title, "upload_type": "dataset", "description": "", "creators": [],
            "keywords": [], "access_right": "open", "license": "cc-by-4.0",
            "publication_date": _dt.date.today().isoformat()}


def new_record(root: str | Path, name: str, title: str = "") -> DepositRecord:
    """Make an empty record ``root/name`` with its review state and a metadata template."""
    problem = valid_name(name)
    if problem:
        raise ValueError(problem)
    root = Path(root)
    folder = root / name
    if folder.exists() or (root / f"{name}.deposit.json").exists():
        raise FileExistsError(f"{folder} already exists")
    for sub in ("measurements", "calibrations"):
        (folder / sub).mkdir(parents=True)
    rec = DepositRecord(folder=folder, title=title, metadata=template(title))
    rec._note("created", by_hand=True)
    rec.save()
    return rec


def _target(rec: DepositRecord, role: str, name: str, group: str) -> tuple[str, Path]:
    if not ITEM_NAME.match(name or ""):
        raise ValueError("an item name is one folder name: letters, digits, '.', '_', '+', '-'")
    if role == "measurement":
        item_id = f"measurements/{name}"
    elif role == "calibration":
        if not ITEM_NAME.match(group or ""):
            raise ValueError("a calibration belongs to a group (setup and date, e.g. "
                             "2014-08-12_hydraharp): name the group")
        item_id = f"calibrations/{group}/{name}"
    else:
        raise ValueError(f"role must be 'measurement' or 'calibration', not {role!r}")
    return item_id, rec.folder / item_id


def _place(src: Path, dst: Path, link: bool) -> str:
    """Put one file at *dst*: hard link on the same disk, else copy. Returns how."""
    if dst.exists():
        if dst.stat().st_size == src.stat().st_size and (
                os.path.samefile(src, dst) or dst.read_bytes() == src.read_bytes()):
            return "present"
        raise FileExistsError(f"{dst} exists with other content -- remove it first")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if link:
        try:
            os.link(src, dst)
            return "linked"
        except OSError:
            pass
    shutil.copy2(src, dst)
    return "copied"


def add_data(rec: DepositRecord, sources: Iterable[str | Path], role: str, name: str,
             group: str = "", link: bool = True) -> dict:
    """Put files and folders into the record as the item *role*/*name* (and *group*).

    A folder keeps its inner layout; a file lands at the item's top. Nothing
    is overwritten: a file of the same name and other content refuses the
    whole call before anything is placed. The sources are remembered in
    :attr:`DepositRecord.sources` (shown to the reviewer as the origin, never
    shipped). Returns ``{"item": id, "linked": n, "copied": n, "present": n}``.
    """
    item_id, dest = _target(rec, role, name, group)
    plan: list[tuple[Path, Path]] = []
    for s in sources:
        s = Path(s).expanduser()
        if s.is_dir():
            plan += [(f, dest / s.name / f.relative_to(s)) for f in content_files(s)]
        elif s.is_file() and not is_clutter(s):
            plan.append((s, dest / s.name))
        elif not s.exists():
            raise FileNotFoundError(s)
    if not plan:
        raise ValueError("nothing to add: no files in what was chosen")
    for src, dst in plan:  # refuse before placing anything
        if dst.exists() and not (dst.stat().st_size == src.stat().st_size
                                 and (os.path.samefile(src, dst)
                                      or dst.read_bytes() == src.read_bytes())):
            raise FileExistsError(f"{dst.relative_to(rec.folder)} exists with other content")
    counts = {"linked": 0, "copied": 0, "present": 0}
    for src, dst in plan:
        counts[_place(src, dst, link)] += 1
    origins = rec.sources.setdefault(item_id, [])
    for s in sources:
        if str(s) not in origins:
            origins.append(str(s))
    rec._note("data added", item=item_id, **counts)
    rec.save()
    return {"item": item_id, **counts}


def zenodo_problems(meta: dict | None) -> list[str]:
    """What keeps *meta* from being a complete, valid Zenodo deposition (empty = fine)."""
    m = meta or {}
    out = []
    if not str(m.get("title", "")).strip():
        out.append("title is empty")
    if not str(m.get("description", "")).strip():
        out.append("description is empty")
    ut = m.get("upload_type", "")
    if ut not in UPLOAD_TYPES:
        out.append(f"upload_type {ut!r} is not one of {', '.join(UPLOAD_TYPES)}")
    if ut == "publication" and m.get("publication_type") not in PUBLICATION_TYPES:
        out.append("a publication needs a publication_type")
    if ut == "image" and m.get("image_type") not in IMAGE_TYPES:
        out.append("an image needs an image_type")
    creators = m.get("creators") or []
    if not creators:
        out.append("no creators")
    for i, c in enumerate(creators, 1):
        name = str(c.get("name", "")) if isinstance(c, dict) else ""
        if ", " not in name or not all(p.strip() for p in name.split(", ", 1)):
            out.append(f"creator {i}: name must be 'Family, Given' (is {name!r})")
        orcid = c.get("orcid", "") if isinstance(c, dict) else ""
        if orcid and not ORCID.match(orcid):
            out.append(f"creator {i}: ORCID {orcid!r} is not 0000-0000-0000-0000")
    ar = m.get("access_right", "")
    if ar not in ACCESS_RIGHTS:
        out.append(f"access_right {ar!r} is not one of {', '.join(ACCESS_RIGHTS)}")
    if ar in ("open", "embargoed") and not m.get("license"):
        out.append("open or embargoed access needs a license")
    if ar == "embargoed" and not DATE.match(str(m.get("embargo_date", ""))):
        out.append("embargoed access needs an embargo_date (YYYY-MM-DD)")
    if ar == "restricted" and not str(m.get("access_conditions", "")).strip():
        out.append("restricted access needs access_conditions")
    pd = m.get("publication_date")
    if pd and not DATE.match(str(pd)):
        out.append(f"publication_date {pd!r} is not YYYY-MM-DD")
    kw = m.get("keywords", [])
    if not isinstance(kw, list) or not all(isinstance(k, str) and k.strip() for k in kw):
        out.append("keywords must be a list of words")
    for i, r in enumerate(m.get("related_identifiers") or [], 1):
        if not isinstance(r, dict) or not r.get("identifier") or r.get("relation") not in RELATIONS:
            out.append(f"related identifier {i}: needs an identifier and a relation "
                       f"({', '.join(RELATIONS[:4])}, …)")
    return out
