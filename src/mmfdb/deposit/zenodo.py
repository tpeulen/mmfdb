"""Zenodo: upload a reviewed package as a draft, compare, and (gated) publish.

Nothing here runs unless :func:`~mmfdb.deposit.record.blockers` is empty: every
item of the record approved at its current content and the package built from
exactly that content. Uploading creates or updates a **draft**; publishing is a
separate call that additionally needs a publish approval bound to the package's
MD5 and a remote file that matches it.

The token comes from the system keychain (:mod:`.credentials`, entered once in
the deposit tool) or ``ZENODO_TOKEN``, and is never written to a file. The HTTP
session is injectable, so the flow is testable without a network.

API notes (verified 2026-10): files are PUT to the deposition's
``links.bucket`` with ``Content-Type: application/octet-stream`` (anything else
is a 415); large PUTs occasionally return 502 and succeed when retried.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .credentials import load_token
from .record import DepositRecord, _now, blockers

API = "https://zenodo.org/api"
SANDBOX = "https://sandbox.zenodo.org/api"


class GateError(RuntimeError):
    """The record is not cleared for this step; the message lists why."""


class ZenodoClient:
    """The few deposition calls a record needs."""

    def __init__(self, token: str | None = None, base: str = API, session: Any = None,
                 retries: int = 3) -> None:
        self.base = base.rstrip("/")
        self.token, self.token_source = (token, "given") if token else load_token(self.base)
        if not self.token:
            raise RuntimeError(f"no Zenodo token for {self.base}: enter it in the deposit "
                               "tool (stored in the system keychain) or set ZENODO_TOKEN")
        if session is None:
            import requests

            session = requests.Session()
        self.s = session
        self.retries = retries

    def _h(self, extra: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self.token}", **(extra or {})}

    def _check(self, r: Any, what: str) -> Any:
        if r.status_code >= 400:
            raise RuntimeError(f"Zenodo {what}: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    def check_token(self) -> str:
        """``""`` when the token may create depositions, else what is wrong."""
        r = self.s.get(f"{self.base}/deposit/depositions", params={"size": 1},
                       headers=self._h())
        if r.status_code == 200:
            return ""
        if r.status_code in (401, 403):
            return (f"rejected (HTTP {r.status_code}): the token is wrong, expired, or lacks "
                    "the deposit:write scope")
        return f"HTTP {r.status_code}: {r.text[:200]}"

    def deposition(self, dep_id: int | str) -> dict:
        return self._check(self.s.get(f"{self.base}/deposit/depositions/{dep_id}",
                                      headers=self._h()), "get deposition")

    def create(self, metadata: dict) -> dict:
        return self._check(self.s.post(f"{self.base}/deposit/depositions", json={
            "metadata": metadata}, headers=self._h()), "create deposition")

    def update_metadata(self, dep_id: int | str, metadata: dict) -> dict:
        return self._check(self.s.put(f"{self.base}/deposit/depositions/{dep_id}", json={
            "metadata": metadata}, headers=self._h()), "update metadata")

    def files(self, dep_id: int | str) -> list[dict]:
        """Remote files as ``{name, md5, bytes, id}``."""
        out = []
        for f in self._check(self.s.get(f"{self.base}/deposit/depositions/{dep_id}/files",
                                        headers=self._h()), "list files"):
            md5 = str(f.get("checksum", "")).removeprefix("md5:")
            out.append({"name": f.get("filename") or f.get("key"), "md5": md5,
                        "bytes": int(f.get("filesize") or f.get("size") or 0),
                        "id": f.get("id")})
        return out

    def delete_file(self, dep_id: int | str, file_id: str) -> None:
        r = self.s.delete(f"{self.base}/deposit/depositions/{dep_id}/files/{file_id}",
                          headers=self._h())
        if r.status_code not in (204, 404):
            self._check(r, "delete file")

    def put_file(self, bucket: str, path: Path) -> dict:
        for attempt in range(self.retries):
            with open(path, "rb") as f:
                r = self.s.put(f"{bucket}/{path.name}", data=f,
                               headers=self._h({"Content-Type": "application/octet-stream"}))
            if r.status_code != 502 or attempt == self.retries - 1:
                return self._check(r, f"upload {path.name}")
            time.sleep(5 * (attempt + 1))
        raise AssertionError("unreachable")

    def new_version(self, dep_id: int | str) -> dict:
        """A new-version draft of the published *dep_id* (it starts with that version's files)."""
        d = self._check(self.s.post(
            f"{self.base}/deposit/depositions/{dep_id}/actions/newversion", headers=self._h()),
            "new version")
        draft = str(d.get("links", {}).get("latest_draft", "")).rstrip("/").rsplit("/", 1)[-1]
        if not draft:
            raise RuntimeError(f"Zenodo gave no draft for a new version of {dep_id}")
        return self.deposition(draft)

    def publish(self, dep_id: int | str) -> dict:
        return self._check(self.s.post(
            f"{self.base}/deposit/depositions/{dep_id}/actions/publish", headers=self._h()),
            "publish")


def refresh_remote(rec: DepositRecord, client: ZenodoClient) -> dict:
    """Read what the repository holds for this record into ``rec.remote``."""
    dep_id = rec.remote.get("deposition_id")
    if not dep_id:
        return rec.remote
    d = client.deposition(dep_id)
    rec.remote.update({"state": d.get("state"), "submitted": bool(d.get("submitted")),
                       "files": client.files(dep_id), "checked_at": _now(),
                       "html": d.get("links", {}).get("html", "")})
    return rec.remote


def compare(rec: DepositRecord) -> list[str]:
    """Differences between the package and the remote draft (empty = identical)."""
    pkg, files = rec.package, {f["name"]: f for f in rec.remote.get("files", [])}
    if not rec.remote.get("deposition_id"):
        return ["not uploaded"]
    name = Path(pkg.get("path", "")).name
    out = [f"remote has an extra file {n}" for n in files if n != name]
    if name not in files:
        out.append(f"remote lacks {name}")
    elif files[name]["md5"] != pkg.get("md5"):
        out.append(f"{name}: remote md5 {files[name]['md5']} != local {pkg.get('md5')}")
    return out


def metadata_digest(rec: DepositRecord) -> str:
    """sha256 of the record's Zenodo metadata, as the review binds it."""
    import hashlib
    import json

    text = json.dumps(rec.metadata, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def upload_history(rec: DepositRecord) -> list[dict]:
    """Every upload of the record, oldest first: ``rec.remote["uploads"]``, preceded by the
    uploads only the record's log knows (made before uploads were tracked)."""
    tracked = list(rec.remote.get("uploads", []))
    known = {(u["deposition_id"], u["md5"]) for u in tracked}
    older = [{"at": e["at"], "kind": "upload", "deposition_id": e.get("deposition"),
              "name": "", "bytes": 0, "md5": e.get("md5", ""),
              "verified": not e.get("differences")}
             for e in rec.log if e.get("event") == "upload" and "kind" not in e
             and (e.get("deposition"), e.get("md5")) not in known]
    return older + tracked


def upload_kind(rec: DepositRecord) -> str:
    """What :func:`upload` would do now.

    ``"new"`` (no deposition yet), ``"update"`` (replace the package or the
    metadata of the unpublished draft), ``"new version"`` (the record is
    published: a new version of it, under the same concept DOI) or
    ``"current"`` (the draft holds exactly this package and was sent exactly
    this metadata).
    """
    if not rec.remote.get("deposition_id"):
        return "new"
    if rec.remote.get("doi") and rec.remote.get("state") == "done":
        return "new version"
    last = (rec.remote.get("uploads") or [{}])[-1]
    same_meta = last.get("metadata_digest") == metadata_digest(rec)
    return "current" if not compare(rec) and same_meta else "update"


def upload(rec: DepositRecord, client: ZenodoClient, metadata: dict | None = None) -> dict:
    """Upload the reviewed package: a new draft, an update of the draft, or a new version.

    Refuses (:class:`GateError`) unless :func:`blockers` is empty. The draft
    ends up holding exactly the package -- an older package, or any other
    file, is removed from it -- and the MD5 is compared after the upload.
    Every upload is appended to ``rec.remote["uploads"]`` (when, which
    deposition, what kind, the package's name, size and MD5): what was
    uploaded is tracked, not inferred.
    """
    reasons = blockers(rec)
    if reasons:
        raise GateError("not cleared for upload:\n  " + "\n  ".join(reasons))
    if metadata is not None and metadata != rec.metadata:
        raise GateError("only the reviewed metadata is uploaded (edit it in the record, review it)")
    kind = upload_kind(rec)
    if kind == "current":
        raise GateError("the draft already holds this package and metadata: nothing to upload")
    meta = rec.metadata
    dep_id = rec.remote.get("deposition_id")
    if kind == "new version":
        previous = {k: rec.remote.get(k) for k in ("deposition_id", "doi", "published_at")}
        rec.remote.setdefault("published", []).append(previous)
        dep = client.new_version(dep_id)
        rec.remote.pop("doi", None)
        rec.remote.pop("published_at", None)
        dep = client.update_metadata(dep["id"], meta) if meta else dep
    elif kind == "update":
        dep = client.update_metadata(dep_id, meta) if meta else client.deposition(dep_id)
    else:
        dep = client.create(meta)
    dep_id = dep["id"]
    rec.remote["deposition_id"] = dep_id
    zp = Path(rec.package["path"])
    present = False
    for f in client.files(dep_id):
        if f["name"] == zp.name and f["md5"] == rec.package["md5"]:
            present = True  # a metadata-only update: the package is there already
        else:
            client.delete_file(dep_id, f["id"])
    if not present:
        client.put_file(dep["links"]["bucket"], zp)
    refresh_remote(rec, client)
    diff = compare(rec)
    rec.remote.setdefault("uploads", []).append({
        "at": _now(), "kind": kind, "deposition_id": dep_id, "name": zp.name,
        "bytes": rec.package.get("bytes", zp.stat().st_size), "md5": rec.package["md5"],
        "metadata_digest": metadata_digest(rec), "package_sent": not present,
        "verified": not diff})
    rec._note("upload", deposition=dep_id, kind=kind, md5=rec.package["md5"], differences=diff)
    if diff:
        raise RuntimeError(f"uploaded {zp.name} does not verify: {diff}")
    return rec.remote


def publish(rec: DepositRecord, client: ZenodoClient) -> dict:
    """Publish the draft -- irreversible, so gated on its own approval."""
    reasons = blockers(rec, for_publish=True)
    if reasons:
        raise GateError("not cleared for publishing:\n  " + "\n  ".join(reasons))
    d = client.publish(rec.remote["deposition_id"])
    rec.remote.update({"state": d.get("state"), "doi": d.get("doi", ""), "published_at": _now()})
    rec._note("publish", deposition=rec.remote["deposition_id"], doi=rec.remote.get("doi"))
    return rec.remote
