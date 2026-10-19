"""A deposition record and its review state: ``<record>.deposit.json``.

A record is a folder that will be packed into ONE archive and uploaded as one
repository entry (a Zenodo deposition). Before anything leaves the machine a
person checks it, and this file is where that check is written down:

* ``items`` -- the units a reviewer judges: every measurement folder, every
  calibration folder, and the metadata files (README, flrCIF, provenance).
  Each carries where it came from, how many files and bytes it holds, a
  content digest, the ``flags`` that say why it needs a closer look (data added
  beyond the obvious, a role inferred rather than recorded) and its ``review``.
* ``review`` on an item is ``pending``, ``approved`` or ``rejected``, with who,
  when and a note. An approval is bound to the item's digest: when the content
  changes after approval, the item is pending again -- an approval of other
  bytes is not an approval of these.
* ``package`` -- the archive that is uploaded, with its size and checksums.
* ``remote`` -- what the repository holds after upload (deposition id, file
  names and MD5s), so local and remote can be compared at any time.

The file lives BESIDE the record folder (``records/<name>.deposit.json``), not
inside it, so the review is not shipped with the data.

:func:`blockers` is the gate every upload passes: it lists what still stands in
the way, and an upload proceeds only when it is empty.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "mmfdb.deposit/1"
STATUSES = ("pending", "approved", "rejected")
ROLES = ("measurement", "calibration", "metadata", "other")

#: Files a record carries at its root; each is one metadata item.
METADATA_FILES = ("README.md", "metadata.cif", "origins.json", "manifest_sha256.csv")
#: The repository's own metadata (title, creators, description, license) is
#: reviewed like a file: it is what the world reads first about the data.
METADATA_ITEM = "Zenodo metadata"


#: Files an operating system leaves in folders it shows (Finder, Explorer,
#: Spotlight). Browsing a record to check it must not change what is
#: reviewed or shipped, so these are neither scanned nor packed.
OS_CLUTTER = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", ".localized", "Icon\r"})
OS_CLUTTER_DIRS = frozenset({".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems",
                             "__MACOSX"})


def is_clutter(path: Path) -> bool:
    """Whether *path* is operating-system clutter rather than record content."""
    return (path.name in OS_CLUTTER or path.name.startswith("._")
            or any(part in OS_CLUTTER_DIRS for part in path.parts))


def content_files(base: Path) -> list[Path]:
    """Every file under *base* that is record content, sorted."""
    return sorted(p for p in base.rglob("*") if p.is_file() and not is_clutter(p))


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _reviewer() -> str:
    return os.environ.get("MMFDB_REVIEWER") or os.environ.get("USER") or "unknown"


def file_digest(path: Path, algorithm: str = "sha256") -> str:
    """Hex digest of one file, read in blocks."""
    h = hashlib.new(algorithm)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tree_digest(root: Path, files: Iterable[Path]) -> str:
    """Digest of a set of files: their relative paths and sha256s, in order."""
    h = hashlib.sha256()
    for p in sorted(files):
        h.update(f"{p.relative_to(root).as_posix()}\0{file_digest(p)}\n".encode())
    return h.hexdigest()


@dataclass
class Review:
    """One reviewer decision. ``digest`` is what was looked at."""

    status: str = "pending"
    by: str = ""
    at: str = ""
    note: str = ""
    digest: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "by": self.by, "at": self.at, "note": self.note,
                "digest": self.digest}

    @classmethod
    def from_dict(cls, d: dict | None) -> Review:
        d = d or {}
        return cls(**{k: str(d.get(k, "")) for k in ("status", "by", "at", "note", "digest")}
                   ) if d else cls()


@dataclass
class Item:
    """A unit of review: a folder (or a metadata file) of the record."""

    id: str
    role: str
    group: str = ""
    description: str = ""
    origin: str = ""
    files: int = 0
    bytes: int = 0
    digest: str = ""
    flags: list[str] = field(default_factory=list)
    #: What a machine check found wrong (e.g. dictionary conformance). Unlike a
    #: flag, an error blocks the upload whatever the review says.
    errors: list[str] = field(default_factory=list)
    review: Review = field(default_factory=Review)

    @property
    def status(self) -> str:
        """The effective status: an approval of other content counts as pending."""
        if self.review.status == "approved" and self.review.digest != self.digest:
            return "pending"
        return self.review.status or "pending"

    def to_dict(self) -> dict:
        return {"id": self.id, "role": self.role, "group": self.group,
                "description": self.description, "origin": self.origin, "files": self.files,
                "bytes": self.bytes, "digest": self.digest, "flags": list(self.flags),
                "errors": list(self.errors), "review": self.review.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> Item:
        return cls(id=d["id"], role=d.get("role", "other"), group=d.get("group", ""),
                   description=d.get("description", ""), origin=d.get("origin", ""),
                   files=int(d.get("files", 0)), bytes=int(d.get("bytes", 0)),
                   digest=d.get("digest", ""), flags=list(d.get("flags", [])),
                   errors=list(d.get("errors", [])), review=Review.from_dict(d.get("review")))


@dataclass
class DepositRecord:
    """The review state of one record folder, persisted as ``<folder>.deposit.json``."""

    folder: Path
    title: str = ""
    metadata: dict = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)
    package: dict = field(default_factory=dict)
    remote: dict = field(default_factory=dict)
    publish: Review = field(default_factory=Review)
    log: list[dict] = field(default_factory=list)
    #: Where hand-added data came from, per item id (shown as its origin, never shipped).
    sources: dict[str, list[str]] = field(default_factory=dict)

    # -- persistence -------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.folder.name

    @property
    def path(self) -> Path:
        """Where the review state lives: beside the folder, never inside it."""
        return self.folder.parent / f"{self.folder.name}.deposit.json"

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, "record": self.name, "title": self.title,
                "metadata": self.metadata, "items": [i.to_dict() for i in self.items],
                "package": self.package, "remote": self.remote,
                "publish": self.publish.to_dict(), "log": self.log, "sources": self.sources}

    def save(self) -> Path:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.path)
        return self.path

    @classmethod
    def load(cls, folder: str | Path) -> DepositRecord:
        folder = Path(folder)
        if folder.name.endswith(".deposit.json"):
            folder = folder.with_name(folder.name[: -len(".deposit.json")])
        d = json.loads((folder.parent / f"{folder.name}.deposit.json").read_text())
        if d.get("schema") != SCHEMA:
            raise ValueError(f"{folder}: not a {SCHEMA} record ({d.get('schema')!r})")
        return cls(folder=folder, title=d.get("title", ""), metadata=d.get("metadata", {}),
                   items=[Item.from_dict(i) for i in d.get("items", [])],
                   package=d.get("package", {}), remote=d.get("remote", {}),
                   publish=Review.from_dict(d.get("publish")), log=d.get("log", []),
                   sources=d.get("sources", {}))

    # -- the items ---------------------------------------------------------------

    def item(self, item_id: str) -> Item:
        for i in self.items:
            if i.id == item_id:
                return i
        raise KeyError(item_id)

    def _note(self, event: str, **kw: Any) -> None:
        self.log.append({"at": _now(), "by": _reviewer(), "event": event, **kw})

    def set_review(self, item_id: str, status: str, note: str = "", by: str = "") -> Item:
        """Record a reviewer's decision on one item, bound to its current digest."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, not {status!r}")
        it = self.item(item_id)
        it.review = Review(status=status, by=by or _reviewer(), at=_now(), note=note,
                           digest=it.digest if status != "pending" else "")
        self._note("review", item=item_id, status=status, note=note)
        return it

    def approve(self, item_id: str, note: str = "", by: str = "") -> Item:
        return self.set_review(item_id, "approved", note, by)

    def reject(self, item_id: str, note: str = "", by: str = "") -> Item:
        return self.set_review(item_id, "rejected", note, by)

    def approve_publish(self, note: str = "", by: str = "") -> Review:
        """Approve publishing THIS package (bound to its MD5; a rebuild voids it)."""
        if not self.package.get("md5"):
            raise ValueError("no package to approve for publishing")
        self.publish = Review(status="approved", by=by or _reviewer(), at=_now(), note=note,
                              digest=self.package["md5"])
        self._note("publish approval", md5=self.package["md5"], note=note)
        return self.publish

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(STATUSES, 0)
        for i in self.items:
            out[i.status] += 1
        return out


def _folder_items(root: Path, top: str) -> list[Path]:
    base = root / top
    return sorted(base.iterdir()) if base.is_dir() else []


def origins_describer(folder: str | Path) -> Callable[[str], dict]:
    """Describe items from the record's ``origins.json`` (origin + content), if any.

    The provenance a record ships already says where each folder came from;
    the reviewer should see the same text, not a second copy of it.
    """
    try:
        loc = json.loads((Path(folder) / "origins.json").read_text()).get("original_locations", {})
    except (FileNotFoundError, ValueError):
        loc = {}

    def describe(item_id: str) -> dict:
        d = loc.get(item_id) or {}
        return {k: v for k, v in (("origin", d.get("server", "")),
                                  ("description", d.get("content", ""))) if v}

    return describe


def scan(folder: str | Path, describe: Callable[[str], dict] | None = None,
         title: str = "", metadata: dict | None = None,
         flags: dict[str, list[str]] | None = None) -> DepositRecord:
    """Build (or refresh) the review state of a record folder.

    Items are every ``measurements/<x>``, every ``calibrations/<group>/<x>``,
    and each root metadata file. ``describe(item_id)`` may supply ``group``,
    ``description``, ``origin`` and ``flags`` for an item (default: the
    record's ``origins.json``); ``flags`` maps item ids to the reasons a
    reviewer must look closer -- the builder's knowledge, kept out of the
    shipped files. Existing reviews are
    kept; an item whose digest changed shows as pending again (see
    :attr:`Item.status`), and an item that disappeared is dropped and logged.
    """
    folder = Path(folder)
    if describe is None:
        describe = origins_describer(folder)
    try:
        rec = DepositRecord.load(folder)
    except FileNotFoundError:
        rec = DepositRecord(folder=folder)
    if title:
        rec.title = title
    if metadata is not None:
        rec.metadata = metadata
    old = {i.id: i for i in rec.items}
    units: list[tuple[str, str, str, list[Path]]] = []
    for m in _folder_items(folder, "measurements"):
        if m.is_dir():
            units.append((f"measurements/{m.name}", "measurement", "", content_files(m)))
    for g in _folder_items(folder, "calibrations"):
        for c in sorted(g.iterdir()) if g.is_dir() else []:
            if c.is_dir():
                units.append((f"calibrations/{g.name}/{c.name}", "calibration", g.name,
                              content_files(c)))
    for name in METADATA_FILES:
        p = folder / name
        if p.is_file():
            units.append((name, "metadata", "", [p]))
    covered = {p for *_, files in units for p in files}
    stray = [p for p in content_files(folder) if p not in covered]
    items = []
    for item_id, role, group, files in units:
        it = old.get(item_id) or Item(id=item_id, role=role, group=group)
        it.role, it.group = role, group or it.group
        it.files = len(files)
        it.bytes = sum(p.stat().st_size for p in files)
        it.digest = tree_digest(folder, files)
        if describe is not None:
            extra = describe(item_id) or {}
            it.description = extra.get("description", it.description)
            it.origin = extra.get("origin", it.origin)
            it.group = extra.get("group", it.group)
            it.flags = list(extra.get("flags", it.flags))
        if flags is not None:
            it.flags = list(flags.get(item_id, []))
        items.append(it)
    if stray:
        it = old.get("(unassigned files)") or Item(id="(unassigned files)", role="other")
        it.files, it.bytes = len(stray), sum(p.stat().st_size for p in stray)
        it.digest = tree_digest(folder, stray)
        it.description = ", ".join(p.relative_to(folder).as_posix() for p in stray[:5])
        it.flags = ["files outside measurements/, calibrations/ and the metadata files"]
        items.append(it)
    meta = old.get(METADATA_ITEM) or Item(id=METADATA_ITEM, role="metadata")
    if flags is not None:
        meta.flags = list(flags.get(METADATA_ITEM, []))
    items.append(meta)
    for it in items:
        if not it.origin and rec.sources.get(it.id):
            it.origin = ", ".join(rec.sources[it.id])
        if it is not meta:
            it.errors = check_item(folder, it, rec)
    for gone in set(old) - {i.id for i in items}:
        rec._note("item removed", item=gone)
    rec.items = items
    refresh_metadata_item(rec)
    rec._note("scan", items=len(items))
    return rec


def refresh_metadata_item(rec: DepositRecord) -> Item:
    """Bring the :data:`METADATA_ITEM` in line with :attr:`DepositRecord.metadata`.

    Cheap (no file is read), so an editor calls it on every change; its
    approval holds only while the digest of the metadata is the approved one.
    """
    try:
        meta = rec.item(METADATA_ITEM)
    except KeyError:
        meta = Item(id=METADATA_ITEM, role="metadata")
        rec.items.append(meta)
    text = json.dumps(rec.metadata, sort_keys=True, ensure_ascii=False)
    meta.files, meta.bytes = 0, len(text.encode())
    meta.digest = hashlib.sha256(text.encode()).hexdigest()
    meta.description = "title, creators, description, license and keywords the deposition carries"
    meta.errors = check_item(rec.folder, meta, rec)
    return meta


def check_item(folder: Path, item: Item, rec: DepositRecord | None = None) -> list[str]:
    """Machine checks of one item.

    ``metadata.cif`` must conform to the dictionaries; the Zenodo metadata must
    be a complete deposition (:func:`mmfdb.deposit.compose.zenodo_problems`).
    """
    if item.id == METADATA_ITEM:
        from .compose import zenodo_problems

        return zenodo_problems(rec.metadata if rec is not None else {})
    if item.id != "metadata.cif":
        return []
    from mmfdb.schema.cif_validate import validate_cif

    problems = validate_cif((folder / "metadata.cif").read_text(encoding="utf-8"))
    return [str(p) for p in problems]


def blockers(rec: DepositRecord, for_publish: bool = False) -> list[str]:
    """Everything that stands between this record and an upload (empty = go).

    Every item approved (at its current content), a package that exists and
    still matches its recorded checksum. For publishing, additionally a remote
    draft whose files match the package and an explicit publish approval.
    """
    out = []
    if not rec.items:
        out.append("record has no items (scan it first)")
    for i in rec.items:
        if i.errors:
            out.append(f"{i.id}: {len(i.errors)} error(s), e.g. {i.errors[0]}")
        if i.status != "approved":
            stale = i.review.status == "approved"
            out.append(f"{i.id}: {'changed since approval' if stale else i.status}")
    pkg = rec.package
    zp = Path(pkg.get("path", "")) if pkg.get("path") else None
    if zp is None:
        out.append("no package built")
    elif not zp.is_file():
        out.append(f"package missing: {zp}")
    elif zp.stat().st_size != int(pkg.get("bytes", -1)):
        out.append("package changed since it was built")
    elif pkg.get("items_digest") != items_digest(rec):
        out.append("package predates the current item content (rebuild it)")
    if for_publish:
        files = {f.get("name"): f for f in rec.remote.get("files", [])}
        if not rec.remote.get("deposition_id"):
            out.append("not uploaded")
        elif zp is not None and files.get(zp.name, {}).get("md5") != pkg.get("md5"):
            out.append("remote file does not match the package")
        if rec.publish.status != "approved" or rec.publish.digest != pkg.get("md5"):
            out.append("publishing not approved for this package")
    return out


def items_digest(rec: DepositRecord) -> str:
    """One digest over all item digests: what a package was built from.

    The Zenodo metadata is not in the archive, so editing it does not ask for
    a new package -- only for its own review.
    """
    h = hashlib.sha256()
    for i in sorted((i for i in rec.items if i.id != METADATA_ITEM), key=lambda i: i.id):
        h.update(f"{i.id}\0{i.digest}\n".encode())
    return h.hexdigest()
