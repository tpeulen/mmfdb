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


def upload(rec: DepositRecord, client: ZenodoClient, metadata: dict | None = None) -> dict:
    """Upload the reviewed package into a draft (created when there is none).

    Refuses (:class:`GateError`) unless :func:`blockers` is empty. A draft that
    already holds other files keeps nothing stale: files of the same name are
    replaced, other files are reported by :func:`compare` and left for the
    reviewer to judge. The MD5 is compared after upload.
    """
    reasons = blockers(rec)
    if reasons:
        raise GateError("not cleared for upload:\n  " + "\n  ".join(reasons))
    if metadata is not None and metadata != rec.metadata:
        raise GateError("only the reviewed metadata is uploaded (edit it in the record, review it)")
    meta = rec.metadata
    dep_id = rec.remote.get("deposition_id")
    dep = client.update_metadata(dep_id, meta) if dep_id and meta else (
        client.deposition(dep_id) if dep_id else client.create(meta))
    dep_id = dep["id"]
    rec.remote["deposition_id"] = dep_id
    zp = Path(rec.package["path"])
    for f in client.files(dep_id):
        if f["name"] == zp.name and f["md5"] != rec.package["md5"]:
            client.delete_file(dep_id, f["id"])
    client.put_file(dep["links"]["bucket"], zp)
    refresh_remote(rec, client)
    diff = compare(rec)
    rec._note("upload", deposition=dep_id, md5=rec.package["md5"], differences=diff)
    if any(zp.name in d for d in diff):
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
