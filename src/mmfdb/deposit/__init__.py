"""Deposition of reviewed records to a data repository (Zenodo).

Every record passes one fixed path, and a person signs off every step that
leaves the machine::

    scan(folder)            -> <folder>.deposit.json: items, digests, flags
    approve / reject items  -> each decision bound to the item's content digest
                               (the Zenodo metadata is one of them; compose.py
                               makes a record by hand: new -> add data -> metadata)
    build_package(rec, out) -> the single archive, bound to the item content
    upload(rec, client)     -> a DRAFT, refused while anything blocks; md5 checked
    approve_publish(...)    -> bound to the package md5
    publish(rec, client)    -> refused unless the draft matches and publish is approved

:func:`blockers` says what stands in the way at any point. The CLI is
``python -m mmfdb.deposit``; chisurf's *Zenodo deposit* tool is the GUI.
"""

from .package import build_package
from .record import (
    SCHEMA,
    DepositRecord,
    Item,
    Review,
    blockers,
    items_digest,
    origins_describer,
    refresh_metadata_item,
    scan,
)
from .zenodo import GateError, ZenodoClient, compare, publish, refresh_remote, upload

__all__ = [
    "SCHEMA",
    "DepositRecord",
    "GateError",
    "Item",
    "Review",
    "ZenodoClient",
    "blockers",
    "build_package",
    "compare",
    "items_digest",
    "refresh_metadata_item",
    "origins_describer",
    "publish",
    "refresh_remote",
    "scan",
    "upload",
]
