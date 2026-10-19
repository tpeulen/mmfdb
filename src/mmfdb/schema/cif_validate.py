"""Validate an mmCIF document against the bundled dictionaries.

A deposition's ``metadata.cif`` is only useful if a stock reader and the
archive's checker accept it, so this checks what the dictionaries declare --
not merely that each tag exists:

* every tag is defined, and its category is known;
* every **mandatory** item of each category used is present and not ``?``/``.``;
* values are of the item's **type** (int, float, code, date, ...) and within
  its **enumeration**;
* each category's **key** is unique;
* every child value **resolves** to a parent value present in the document
  (``_flr_experiment.instrument_id`` -> ``_flr_instrument.id``), and the parent
  category is present at all.

:func:`read_cif` is a small but complete reader for the subset a deposition
uses: one data block, ``loop_``s and single items, quoted values and
``;`` text fields. :func:`validate_cif` returns a list of :class:`Problem`
(empty when the document conforms).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .pdbx_metadata import MmcifDictionary

#: Value patterns for the dictionary type codes a deposition uses. A type not
#: listed is not checked beyond being present.
_TYPES = {
    "int": re.compile(r"^[+-]?\d+$"),
    "float": re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?(\(\d+\))?$"),
    "code": re.compile(r"^\S+$"),
    "ucode": re.compile(r"^\S+$"),
    "uline": re.compile(r"^[^\n]*$"),
    "line": re.compile(r"^[^\n]*$"),
    "yyyy-mm-dd": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "datetime": re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?)?$"),
    "boolean": re.compile(r"^(yes|no|y|n|true|false|0|1)$", re.IGNORECASE),
}
#: Types whose enumerations compare case-insensitively.
_CASELESS = {"ucode", "uline", "uchar1", "uchar3"}
MISSING = ("?", ".")


@dataclass(frozen=True)
class Problem:
    """One way the document departs from the dictionaries."""

    kind: str
    tag: str
    message: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.tag}: {self.message}"


def _tokens(text: str) -> Iterable[str | tuple[str]]:
    """CIF tokens; quoted values and text fields come as 1-tuples (never keywords)."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(";"):
            body = [line[1:]]
            i += 1
            while i < len(lines) and not lines[i].startswith(";"):
                body.append(lines[i])
                i += 1
            if i == len(lines):
                raise ValueError("unterminated ';' text field")
            yield ("\n".join(body).strip("\n"),)
            i += 1
            continue
        j = 0
        while j < len(line):
            c = line[j]
            if c.isspace():
                j += 1
            elif c == "#":
                break
            elif c in "'\"":
                k = j + 1
                while k < len(line) and not (line[k] == c and (k + 1 == len(line) or line[k + 1].isspace())):
                    k += 1
                if k >= len(line):
                    raise ValueError(f"unterminated quote on line {i + 1}")
                yield (line[j + 1:k],)
                j = k + 1
            else:
                k = j
                while k < len(line) and not line[k].isspace():
                    k += 1
                yield line[j:k]
                j = k
        i += 1


def read_cif(text: str) -> tuple[str, dict[str, dict]]:
    """Parse one data block: ``(block_name, {category: {"items": [...], "rows": [[...]]}})``."""
    block = ""
    cats: dict[str, dict] = {}
    toks = list(_tokens(text))
    i = 0

    def add(tag: str) -> tuple[str, str]:
        if "." not in tag:
            raise ValueError(f"tag without category: {tag}")
        cat, item = tag[1:].split(".", 1)
        return cat.lower(), item

    while i < len(toks):
        t = toks[i]
        if isinstance(t, str) and t.lower().startswith("data_"):
            if block:
                raise ValueError("more than one data block")
            block = t[5:]
            i += 1
        elif isinstance(t, str) and t.lower() == "loop_":
            i += 1
            tags = []
            while i < len(toks) and isinstance(toks[i], str) and toks[i].startswith("_"):
                tags.append(add(toks[i]))
                i += 1
            values = []
            while i < len(toks) and not (isinstance(toks[i], str) and (
                    toks[i].startswith("_") or toks[i].lower() in ("loop_",)
                    or toks[i].lower().startswith("data_"))):
                values.append(toks[i][0] if isinstance(toks[i], tuple) else toks[i])
                i += 1
            if not tags or len(values) % len(tags):
                raise ValueError(f"loop of {[f'_{c}.{a}' for c, a in tags]}: "
                                 f"{len(values)} values for {len(tags)} items")
            cat = tags[0][0]
            if any(c != cat for c, _ in tags):
                raise ValueError(f"loop mixes categories: {sorted({c for c, _ in tags})}")
            if cat in cats:
                raise ValueError(f"category {cat} given twice")
            n = len(tags)
            cats[cat] = {"items": [a for _, a in tags],
                         "rows": [values[k:k + n] for k in range(0, len(values), n)]}
        elif isinstance(t, str) and t.startswith("_"):
            cat, item = add(t)
            if i + 1 >= len(toks):
                raise ValueError(f"{t} has no value")
            v = toks[i + 1]
            v = v[0] if isinstance(v, tuple) else v
            entry = cats.setdefault(cat, {"items": [], "rows": [[]], "single": True})
            if not entry.get("single"):
                raise ValueError(f"category {cat} given as loop and as single items")
            entry["items"].append(item)
            entry["rows"][0].append(v)
            i += 2
        else:
            raise ValueError(f"unexpected token {t!r}")
    return block, cats


@dataclass
class DdlItem:
    name: str
    mandatory: bool = False
    type_code: str = ""
    enumerations: tuple = ()
    parents: tuple = ()


@dataclass
class DdlCategory:
    name: str
    keys: tuple = ()
    items: dict = None


class DdlDictionary:
    """The DDL2 dictionaries read save frame by save frame (not line by line).

    Handles what a line reader misses: items defined in a ``loop_`` of
    ``_item.name`` rows (an item and its children in one frame), composite
    category keys (``_category_key.name`` loops), enumeration loops with a
    detail column, and ``_item_linked`` rows wherever they appear. Later files
    extend earlier ones (the mmfdb extension refines IHM-FLR items).
    """

    #: Read in this order; a later file adds to or refines an earlier one.
    FILES = ("mmcif_pdbx_v50.dic", "mmcif_ihm_ext.dic", "mmcif_ihm_flr_ext.dic",
             "mmfdb_flr_ext.dic", "mmfdb_workflow_ext.dic")

    def __init__(self, paths) -> None:
        self.items: dict[str, DdlItem] = {}
        self.categories: dict[str, DdlCategory] = {}
        for p in paths:
            self._read(Path(p))
        for name, it in self.items.items():
            cat = name[1:].split(".", 1)[0]
            self.categories.setdefault(cat, DdlCategory(cat, (), {}))
            if self.categories[cat].items is None:
                self.categories[cat].items = {}
            self.categories[cat].items[name.split(".", 1)[1].lower()] = it

    @classmethod
    def bundled(cls) -> DdlDictionary:
        import pickle

        data = MmcifDictionary.DATA_DIR
        paths = [MmcifDictionary._resolve_dic(f) for f in cls.FILES]
        stamp = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in paths if p.exists())
        cache = Path.home() / ".cache" / "mmfdb" / "ddl_validate.pickle"
        try:
            got_stamp, obj = pickle.loads(cache.read_bytes())
            if got_stamp == stamp:
                return obj
        except (OSError, ValueError, EOFError, pickle.UnpicklingError, AttributeError):
            pass
        obj = cls([p for p in paths if p.exists()])
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(pickle.dumps((stamp, obj)))
        except OSError:
            pass
        _ = data
        return obj

    def _read(self, path: Path) -> None:
        import gzip

        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            toks = list(_tokens(f.read()))
        i, n = 0, len(toks)
        while i < n:
            t = toks[i]
            if isinstance(t, str) and t.lower().startswith("save_") and len(t) > 5:
                j = i + 1
                while j < n and not (isinstance(toks[j], str) and toks[j].lower().startswith("save_")):
                    j += 1
                self._frame(t[5:], toks[i + 1:j])
                # a frame ends at save_ (DDL2) or, tolerated, at the next frame
                i = j + 1 if j < n and toks[j].lower() == "save_" else j
            else:
                i += 1

    @staticmethod
    def _tables(toks) -> dict[str, list[str]]:
        """``{tag: [values...]}`` of one frame (singles become one-element lists)."""
        out: dict[str, list[str]] = {}
        i, n = 0, len(toks)
        while i < n:
            t = toks[i]
            if isinstance(t, str) and t.lower() == "loop_":
                i += 1
                tags = []
                while i < n and isinstance(toks[i], str) and toks[i].startswith("_"):
                    tags.append(toks[i].lower())
                    i += 1
                vals = []
                while i < n and not (isinstance(toks[i], str) and (toks[i].startswith("_") or toks[i].lower() == "loop_")):
                    vals.append(toks[i][0] if isinstance(toks[i], tuple) else toks[i])
                    i += 1
                if tags:
                    for k, tag in enumerate(tags):
                        out.setdefault(tag, []).extend(vals[k::len(tags)])
            elif isinstance(t, str) and t.startswith("_") and i + 1 < n:
                v = toks[i + 1]
                out.setdefault(t.lower(), []).append(v[0] if isinstance(v, tuple) else v)
                i += 2
            else:
                i += 1
        return out

    def _item(self, name: str) -> DdlItem:
        key = name.lower()
        if key not in self.items:
            self.items[key] = DdlItem(name)
        return self.items[key]

    def _frame(self, frame: str, toks) -> None:
        tb = self._tables(toks)
        for child, parent in zip(tb.get("_item_linked.child_name", []),
                                 tb.get("_item_linked.parent_name", [])):
            it = self._item(child)
            if parent.lower() not in (p.lower() for p in it.parents):
                it.parents = it.parents + (parent,)
        if not frame.startswith("_"):
            cat = (tb.get("_category.id") or [frame])[0].lower()
            c = self.categories.setdefault(cat, DdlCategory(cat, (), {}))
            keys = tuple(k.lower() for k in tb.get("_category_key.name", []))
            if keys:
                c.keys = keys
            return
        names = tb.get("_item.name", [])
        mands = tb.get("_item.mandatory_code", [])
        for k, nm in enumerate(names):
            it = self._item(nm)
            if k < len(mands):
                it.mandatory = mands[k].lower() == "yes"
        own = frame if not names else (next((x for x in names if x.lower() == frame.lower()), names[0]))
        it = self._item(own)
        if tb.get("_item_type.code"):
            it.type_code = tb["_item_type.code"][0]
            for nm in names:
                if not self._item(nm).type_code:
                    self._item(nm).type_code = it.type_code
        if tb.get("_item_enumeration.value"):
            it.enumerations = tuple(tb["_item_enumeration.value"])

    def get_category(self, name: str) -> DdlCategory | None:
        return self.categories.get(name.lower())


def validate_cif(text: str, dictionary: DdlDictionary | None = None) -> list[Problem]:
    """Every departure of *text* from the dictionaries (empty list = conformant)."""
    dic = dictionary or DdlDictionary.bundled()
    try:
        block, cats = read_cif(text)
    except ValueError as exc:
        return [Problem("syntax", "-", str(exc))]
    out: list[Problem] = []
    if not block:
        out.append(Problem("syntax", "-", "no data_ block"))
    values: dict[str, list[str]] = {}
    for cat, entry in cats.items():
        dcat = dic.get_category(cat)
        if dcat is None:
            out.append(Problem("unknown-category", f"_{cat}", "not defined in the dictionaries"))
            continue
        items = dcat.items or {}
        cols = [a.lower() for a in entry["items"]]
        for a in cols:
            if a not in items:
                out.append(Problem("unknown-item", f"_{cat}.{a}", "not defined in the dictionaries"))
        for a, it in items.items():
            if it.mandatory and a not in cols:
                out.append(Problem("missing-mandatory", f"_{cat}.{a}", "mandatory item absent"))
        for r, row in enumerate(entry["rows"]):
            for a, v in zip(cols, row):
                it = items.get(a)
                if it is None:
                    continue
                tag = f"_{cat}.{a}"
                values.setdefault(tag.lower(), []).append(v)
                if v in MISSING:
                    if it.mandatory:
                        out.append(Problem("missing-mandatory", tag, f"row {r + 1}: value {v!r}"))
                    continue
                pat = _TYPES.get(it.type_code)
                if pat is not None and not pat.match(v):
                    out.append(Problem("type", tag, f"row {r + 1}: {v!r} is not of type {it.type_code}"))
                enum = list(it.enumerations)
                if enum:
                    ok = (v.lower() in {e.lower() for e in enum}) if it.type_code in _CASELESS \
                        else v in enum
                    if not ok:
                        out.append(Problem("enumeration", tag,
                                           f"row {r + 1}: {v!r} not in {enum}"))
        kcols = [k.split(".", 1)[1] for k in dcat.keys if k.startswith(f"_{cat}.")]
        if kcols and all(k in cols for k in kcols):
            keys = [tuple(row[cols.index(k)] for k in kcols) for row in entry["rows"]]
            dup = sorted({k for k in keys if keys.count(k) > 1})
            if dup:
                out.append(Problem("duplicate-key", ", ".join(dcat.keys), f"repeated: {dup[:5]}"))
    # parent links: every child value must exist among the parent's values
    for cat, entry in cats.items():
        dcat = dic.get_category(cat)
        if dcat is None:
            continue
        items = dcat.items or {}
        for a in (x.lower() for x in entry["items"]):
            it = items.get(a)
            if it is None or not it.parents:
                continue
            child_vals = {v for v in values.get(f"_{cat}.{a}".lower(), []) if v not in MISSING}
            if not child_vals:
                continue
            # A child may name several possible parents; it resolves when the
            # values are found among those parents that are present.
            present = [p.lower() for p in it.parents if p[1:].split(".", 1)[0].lower() in cats]
            if not present:
                out.append(Problem("missing-parent", f"_{cat}.{a}",
                                   f"refers to {' or '.join(it.parents)}, none present"))
                continue
            have = set().union(*(values.get(p, []) for p in present))
            lost = sorted(child_vals - have)
            if lost:
                out.append(Problem("unresolved-reference", f"_{cat}.{a}",
                                   f"{lost[:6]} not found in {' / '.join(present)}"))
    return out


def validate_file(path: str | Path) -> list[Problem]:
    return validate_cif(Path(path).read_text(encoding="utf-8"))
