"""``python -m mmfdb.deposit`` -- review and upload records from a terminal."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from . import blockers, build_package, compare, refresh_remote, scan
from . import publish as _publish
from . import upload as _upload
from .record import DepositRecord
from .zenodo import ZenodoClient


def _load(folder: str) -> DepositRecord:
    return DepositRecord.load(Path(folder))


@click.group()
def cli() -> None:
    """Review, package and upload deposition records."""


@cli.command("scan")
@click.argument("folder", type=click.Path(exists=True, file_okay=False))
def scan_cmd(folder: str) -> None:
    """Create or refresh <folder>.deposit.json (reviews are kept)."""
    rec = scan(folder)
    click.echo(f"{rec.save()}: {len(rec.items)} items {rec.counts()}")


@cli.command()
@click.argument("folder")
def status(folder: str) -> None:
    """Items, their review state and what blocks the upload."""
    rec = _load(folder)
    for i in rec.items:
        flag = f"  ! {'; '.join(i.flags)}" if i.flags else ""
        click.echo(f"{i.status:9} {i.role:12} {i.id} ({i.files} files, {i.bytes / 1e6:.1f} MB){flag}")
    b = blockers(rec)
    click.echo("upload: " + ("cleared" if not b else f"blocked ({len(b)})"))
    for line in b:
        click.echo(f"  - {line}")


@cli.command()
@click.argument("folder")
@click.argument("item_ids", nargs=-1, required=True)
@click.option("--reject", is_flag=True, help="Reject instead of approve.")
@click.option("--note", default="", help="Why.")
def review(folder: str, item_ids: tuple[str, ...], reject: bool, note: str) -> None:
    """Approve (or --reject) items by id."""
    rec = _load(folder)
    for item_id in item_ids:
        rec.set_review(item_id, "rejected" if reject else "approved", note)
    rec.save()
    click.echo(rec.counts())


@cli.command()
@click.argument("folder")
@click.argument("out_dir")
def package(folder: str, out_dir: str) -> None:
    """Build the upload archive from the current content."""
    rec = _load(folder)
    p = build_package(rec, out_dir)
    rec.save()
    click.echo(f"{p}: {rec.package['bytes']:,} B md5 {rec.package['md5']}")


@cli.command()
@click.argument("folder")
@click.option("--sandbox", is_flag=True)
def upload(folder: str, sandbox: bool) -> None:
    """Upload the package into a Zenodo DRAFT (refused while anything blocks)."""
    rec = _load(folder)
    client = ZenodoClient(base="https://sandbox.zenodo.org/api" if sandbox else
                          "https://zenodo.org/api")
    try:
        remote = _upload(rec, client)
    finally:
        rec.save()
    click.echo(f"draft {remote['deposition_id']}: {compare(rec) or 'matches the package'}")


@cli.command()
@click.argument("folder")
def check(folder: str) -> None:
    """Compare the package with what Zenodo holds."""
    rec = _load(folder)
    refresh_remote(rec, ZenodoClient())
    rec.save()
    click.echo(compare(rec) or "remote matches the package")


@cli.command("approve-publish")
@click.argument("folder")
@click.option("--note", default="")
def approve_publish(folder: str, note: str) -> None:
    """Approve publishing exactly the current package."""
    rec = _load(folder)
    rec.approve_publish(note)
    rec.save()
    click.echo(f"publishing approved for md5 {rec.package['md5']}")


@cli.command()
@click.argument("folder")
@click.confirmation_option(prompt="Publishing is irreversible. Publish?")
def publish(folder: str) -> None:
    """Publish the draft (refused unless it matches and publishing is approved)."""
    rec = _load(folder)
    try:
        remote = _publish(rec, ZenodoClient())
    finally:
        rec.save()
    click.echo(f"published: {remote.get('doi')}")


if __name__ == "__main__":
    sys.exit(cli())
