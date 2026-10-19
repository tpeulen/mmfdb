"""The deposition path: scan -> review -> package -> draft upload -> publish.

What matters is the gate: nothing reaches the repository that a person has
not approved at its current content, and publishing needs an approval of the
exact package that sits in the draft.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mmfdb.deposit import (
    DepositRecord,
    GateError,
    ZenodoClient,
    blockers,
    build_package,
    compare,
    publish,
    scan,
    upload,
)


@pytest.fixture()
def record(tmp_path: Path) -> Path:
    root = tmp_path / "records" / "t4l-demo"
    for rel, data in {
        "measurements/2014-08-12_7.5M-urea/Split-0000.ht3": b"photons" * 100,
        "measurements/2014-08-12_7.5M-urea/2014-08-12_7.5M-urea.mmfdb.pto": b"pto",
        "calibrations/2014-08-12_pq-hydraharp/h2o-scatter-irf/Split_irf-0000.ht3": b"irf",
        "calibrations/2014-08-12_pq-hydraharp/rho110/Split_rho110-0000.ht3": b"rh",
        "README.md": b"# demo\n",
        "metadata.cif": b"data_demo\n",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    DepositRecord(folder=root, metadata=dict(META)).save()
    return root


#: A complete deposition: the Zenodo metadata is a reviewed, checked item too.
META = {"title": "T4L demo", "upload_type": "dataset", "description": "Demo record.",
        "creators": [{"name": "Peulen, Thomas-Otavio", "orcid": "0000-0002-1825-0097"}],
        "keywords": ["smFRET"], "access_right": "open", "license": "cc-by-4.0"}


def _approve_all(rec: DepositRecord) -> None:
    for i in rec.items:
        rec.approve(i.id, by="tester")


def test_scan_finds_measurement_calibration_and_metadata_items(record: Path):
    rec = scan(record)
    roles = {i.id: (i.role, i.group) for i in rec.items}
    assert roles["measurements/2014-08-12_7.5M-urea"] == ("measurement", "")
    assert roles["calibrations/2014-08-12_pq-hydraharp/h2o-scatter-irf"] == (
        "calibration", "2014-08-12_pq-hydraharp")
    assert roles["README.md"][0] == "metadata"
    assert rec.counts() == {"pending": len(rec.items), "approved": 0, "rejected": 0}
    # the review lives beside the folder, so it is not packed with the data
    assert rec.save() == record.parent / "t4l-demo.deposit.json"


def test_stray_files_become_an_item_of_their_own(record: Path):
    (record / "notes.txt").write_text("x")
    rec = scan(record)
    assert rec.item("(unassigned files)").files == 1


def test_an_approval_is_void_once_the_content_changes(record: Path):
    rec = scan(record)
    rec.approve("calibrations/2014-08-12_pq-hydraharp/rho110", by="tester")
    rec.save()
    (record / "calibrations/2014-08-12_pq-hydraharp/rho110/extra.ht3").write_bytes(b"new")
    rec = scan(record)
    it = rec.item("calibrations/2014-08-12_pq-hydraharp/rho110")
    assert it.review.status == "approved" and it.status == "pending"
    assert any("changed since approval" in b for b in blockers(rec))


def test_flags_and_origins_come_from_the_describer(record: Path):
    def describe(item_id):
        if item_id.endswith("h2o-scatter-irf"):
            return {"origin": "server:/irf", "flags": ["role inferred from the analysis Info"]}
        return {}

    rec = scan(record, describe=describe)
    it = rec.item("calibrations/2014-08-12_pq-hydraharp/h2o-scatter-irf")
    assert it.origin == "server:/irf" and it.flags


class FakeZenodo:
    """Just enough of the deposition API, with the bucket in memory."""

    class R:
        def __init__(self, code, body=None):
            self.status_code = code
            self.content = b"" if body is None else b"1"
            self._b = body
            self.text = json.dumps(body)

        def json(self):
            return self._b

    def __init__(self):
        self.files, self.published, self.calls = {}, False, []

    def _dep(self):
        return {"id": 7, "state": "done" if self.published else "unsubmitted",
                "submitted": self.published, "doi": "10.5281/zenodo.7" if self.published else "",
                "links": {"bucket": "https://z/bucket/7", "html": "https://z/deposit/7"}}

    def post(self, url, json=None, headers=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/actions/publish"):
            self.published = True
        return self.R(201, self._dep())

    def put(self, url, data=None, json=None, headers=None):
        self.calls.append(("PUT", url))
        if "/bucket/" in url:
            assert headers["Content-Type"] == "application/octet-stream"
            body = data.read()
            self.files[url.rsplit("/", 1)[1]] = body
            return self.R(201, {"key": url.rsplit("/", 1)[1]})
        return self.R(200, self._dep())

    def get(self, url, headers=None, params=None):
        if url.endswith("/files"):
            return self.R(200, [{"filename": n, "checksum": hashlib.md5(b).hexdigest(),
                                 "filesize": len(b), "id": n} for n, b in self.files.items()])
        return self.R(200, self._dep())

    def delete(self, url, headers=None):
        self.files.pop(url.rsplit("/", 1)[1], None)
        return self.R(204)


def test_upload_is_refused_until_every_item_is_approved(record: Path, tmp_path: Path):
    rec = scan(record)
    build_package(rec, tmp_path / "zips")
    fake = FakeZenodo()
    with pytest.raises(GateError, match="pending"):
        upload(rec, ZenodoClient(token="t", session=fake))
    assert fake.calls == []  # nothing left the machine


def test_full_path_to_a_published_record(record: Path, tmp_path: Path):
    rec = scan(record)
    _approve_all(rec)
    zp = build_package(rec, tmp_path / "zips")
    assert zp.name == "t4l-demo.zip" and blockers(rec) == []
    fake = FakeZenodo()
    client = ZenodoClient(token="t", session=fake)
    upload(rec, client)
    assert fake.calls[0][2]["metadata"]["title"] == "T4L demo"
    assert compare(rec) == []
    assert fake.files["t4l-demo.zip"] == zp.read_bytes()
    with pytest.raises(GateError, match="publishing not approved"):
        publish(rec, client)
    rec.approve_publish(by="tester")
    publish(rec, client)
    assert rec.remote["doi"] == "10.5281/zenodo.7"


def test_a_rebuilt_package_needs_a_new_publish_approval(record: Path, tmp_path: Path):
    rec = scan(record)
    _approve_all(rec)
    build_package(rec, tmp_path / "zips")
    upload(rec, ZenodoClient(token="t", session=FakeZenodo()))
    rec.approve_publish(by="tester")
    (record / "README.md").write_text("# changed\n")
    rec = scan(record)
    assert any("README.md" in b for b in blockers(rec, for_publish=True))


def test_a_package_built_before_an_item_changed_blocks(record: Path, tmp_path: Path):
    rec = scan(record)
    _approve_all(rec)
    build_package(rec, tmp_path / "zips")
    rec.save()
    (record / "metadata.cif").write_text("data_changed\n")
    rec = scan(record)
    rec.approve("metadata.cif", by="tester")
    assert "package predates the current item content (rebuild it)" in blockers(rec)


def test_the_token_comes_from_the_keychain_before_the_environment(monkeypatch):
    from mmfdb.deposit import credentials

    vault = {}
    monkeypatch.setattr(credentials._store, "store_session_token",
                        lambda h, p, u, t: vault.__setitem__((h, u), t) or True)
    monkeypatch.setattr(credentials._store, "load_session_token",
                        lambda h, p, u: vault.get((h, u)))
    monkeypatch.setattr(credentials._store, "delete_session_token",
                        lambda h, p, u: vault.pop((h, u), None) is not None)
    monkeypatch.setenv("ZENODO_TOKEN", "from-env")
    base = "https://sandbox.zenodo.org/api"
    assert credentials.load_token(base) == ("from-env", "environment")
    assert credentials.store_token(base, " stored \n")
    assert credentials.load_token(base) == ("stored", "keychain")
    # one token per host: production does not see the sandbox token
    assert credentials.load_token("https://zenodo.org/api") == ("from-env", "environment")
    assert ZenodoClient(base=base, session=FakeZenodo()).token == "stored"
    credentials.delete_token(base)
    monkeypatch.delenv("ZENODO_TOKEN")
    with pytest.raises(RuntimeError, match="no Zenodo token"):
        ZenodoClient(base=base, session=FakeZenodo())


def test_browsing_the_record_in_a_file_manager_changes_nothing(record: Path, tmp_path: Path):
    rec = scan(record)
    _approve_all(rec)
    rec.save()
    for junk in (".DS_Store", "measurements/.DS_Store",
                 "calibrations/2014-08-12_pq-hydraharp/rho110/._Split_rho110-0000.ht3"):
        (record / junk).write_bytes(b"finder")
    rec = scan(record)
    assert all(i.status == "approved" for i in rec.items)
    assert "(unassigned files)" not in [i.id for i in rec.items]
    import zipfile

    names = zipfile.ZipFile(build_package(rec, tmp_path / "zips")).namelist()
    assert not [n for n in names if ".DS_Store" in n or "/._" in n]


def test_a_nonconformant_metadata_cif_blocks_even_when_approved(record: Path, tmp_path: Path):
    (record / "metadata.cif").write_text("data_x\n_flr_instrument.id one\n")
    rec = scan(record)
    _approve_all(rec)
    build_package(rec, tmp_path / "zips")
    errs = rec.item("metadata.cif").errors
    assert errs and "type" in errs[0]
    assert any("metadata.cif" in b and "error" in b for b in blockers(rec))


def test_only_the_reviewed_metadata_is_uploaded(record: Path, tmp_path: Path):
    rec = scan(record)
    _approve_all(rec)
    build_package(rec, tmp_path / "zips")
    with pytest.raises(GateError, match="reviewed metadata"):
        upload(rec, ZenodoClient(token="t", session=FakeZenodo()), metadata={"title": "other"})


def test_editing_the_metadata_voids_its_approval_not_the_package(record: Path, tmp_path: Path):
    from mmfdb.deposit.record import METADATA_ITEM

    rec = scan(record)
    _approve_all(rec)
    build_package(rec, tmp_path / "zips")
    rec.save()
    meta = {**rec.metadata, "title": "T4L demo, corrected"}
    rec = scan(record, metadata=meta)
    assert blockers(rec) == [f"{METADATA_ITEM}: changed since approval"]


def test_incomplete_metadata_blocks_whatever_the_review_says(record: Path, tmp_path: Path):
    from mmfdb.deposit.record import METADATA_ITEM

    rec = scan(record, metadata={"title": "x", "creators": [{"name": "Thomas Peulen"}]})
    _approve_all(rec)
    build_package(rec, tmp_path / "zips")
    errors = rec.item(METADATA_ITEM).errors
    assert "description is empty" in errors
    assert any("'Family, Given'" in e for e in errors)
    assert blockers(rec) and blockers(rec)[0].startswith(METADATA_ITEM)


def test_a_record_made_by_hand(tmp_path: Path):
    """new -> add data -> metadata -> scan: the path without a build script."""
    from mmfdb.deposit.compose import add_data, new_record, zenodo_problems
    from mmfdb.deposit.record import METADATA_ITEM

    src = tmp_path / "src"
    (src / "run1").mkdir(parents=True)
    (src / "run1" / "a.ht3").write_bytes(b"photons")
    (src / "run1" / ".DS_Store").write_bytes(b"x")
    (src / "irf.ht3").write_bytes(b"irf")
    root = tmp_path / "records"
    root.mkdir()
    with pytest.raises(ValueError, match="record name"):
        new_record(root, "My Record")
    rec = new_record(root, "demo-by-hand", title="Demo")
    assert zenodo_problems(rec.metadata)  # the template is not yet a deposition

    r = add_data(rec, [src / "run1"], "measurement", "2014-08-12_0M-urea")
    assert r == {"item": "measurements/2014-08-12_0M-urea", "linked": 1, "copied": 0,
                 "present": 0}
    placed = rec.folder / "measurements/2014-08-12_0M-urea/run1/a.ht3"
    assert placed.stat().st_ino == (src / "run1" / "a.ht3").stat().st_ino  # no second copy
    assert not (placed.parent / ".DS_Store").exists()
    with pytest.raises(ValueError, match="group"):
        add_data(rec, [src / "irf.ht3"], "calibration", "irf")
    add_data(rec, [src / "irf.ht3"], "calibration", "irf", group="2014-08-12_hydraharp")
    assert add_data(rec, [src / "irf.ht3"], "calibration", "irf",
                    group="2014-08-12_hydraharp")["present"] == 1  # adding twice is harmless
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "irf.ht3").write_bytes(b"different")
    with pytest.raises(FileExistsError):
        add_data(rec, [tmp_path / "other" / "irf.ht3"], "calibration", "irf",
                 group="2014-08-12_hydraharp")

    rec.metadata.update(META)
    rec = scan(rec.folder, metadata=rec.metadata)
    ids = {i.id: i for i in rec.items}
    assert set(ids) == {"measurements/2014-08-12_0M-urea",
                        "calibrations/2014-08-12_hydraharp/irf", METADATA_ITEM}
    assert ids["measurements/2014-08-12_0M-urea"].origin == str(src / "run1")
    assert ids[METADATA_ITEM].errors == []


def test_a_measurement_names_its_calibrations_from_its_container(record: Path):
    """The container's _mmfdb_calibration links say which calibrations a measurement uses."""
    tttrlib = pytest.importorskip("tttrlib")
    meas = record / "measurements/2014-08-12_7.5M-urea"
    pto = meas / "2014-08-12_7.5M-urea.mmfdb.pto"
    pto.unlink()
    f = tttrlib.PtoFile()
    assert f.create(str(pto), "m")
    tag = tttrlib.PtoTag()
    tag.name, tag.type, tag.target = "_mmfdb_calibration.0.path", tttrlib.PtoType_Text, 0
    tag.text = "../../calibrations/2014-08-12_pq-hydraharp/h2o-scatter-irf"
    f.add_tag(tag)
    assert f.commit()
    f.close()
    rec = scan(record)
    assert rec.item("measurements/2014-08-12_7.5M-urea").uses == [
        "calibrations/2014-08-12_pq-hydraharp/h2o-scatter-irf"]
    rec.save()
    again = DepositRecord.load(record)
    assert again.item("measurements/2014-08-12_7.5M-urea").uses == [
        "calibrations/2014-08-12_pq-hydraharp/h2o-scatter-irf"]


def test_without_links_the_describer_says_what_a_measurement_uses(record: Path):
    uses = ["calibrations/2014-08-12_pq-hydraharp/rho110"]
    rec = scan(record, describe=lambda i: {"uses": uses} if i.startswith("measurements/") else {})
    assert rec.item("measurements/2014-08-12_7.5M-urea").uses == uses
