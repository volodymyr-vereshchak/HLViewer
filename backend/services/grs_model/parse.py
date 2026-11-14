"""Reading the 1С exports the model is fed from.

Three shapes, told apart by what is in the sheet rather than by the file's
name — operators save them under whatever name they like:

* **inflow** — «Фактичний баланс газу по ГРС (подобовий)»: one row per GRS,
  one column per day;
* **consumption** — «Фактичне споживання газу подобове» grouped by GRS and
  category: a GRS row, then its category rows (outline level 1) under it;
* **consumption by object** — the same report grouped by organisation,
  category (outline level 2) and object (level 3, the GRS in its own column).
  The 2025 export came this way; it is folded to GRS × category here.

The files 1С writes cannot be opened by openpyxl as they are: the shared
strings part is named `xl/SharedStrings.xml`, with a capital S, while the
relationships point at `sharedStrings.xml`. The archive is repaired in memory
before it is read.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime

import openpyxl

INFLOW = "inflow"
CONSUMPTION = "consumption"

#: The category the inflow rows are stored under, next to the consumption ones.
INFLOW_CATEGORY = "Надходження"
TOTAL = "Підсумок"

_DATE = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})(?:\s*р\.?)?\s*$")
#: How far down the header with the dates may sit.
_HEADER_ROWS = 25
#: A header row is the one with at least this many dates in it.
_MIN_DATES = 3


class ParseError(ValueError):
    """A message for the operator: what about the file could not be read."""


@dataclass
class ParsedFile:
    kind: str                                   # INFLOW | CONSUMPTION
    #: (day, GRS name as written, category, volume in m³)
    rows: list[tuple[date, str, str, float]] = field(default_factory=list)
    #: Every day the file has a column for — also the days with no figures:
    #: a GRS with nothing against a day the report covers consumed nothing.
    days: list[date] = field(default_factory=list)
    title: str = ""
    layout: str = ""                            # "grs" | "category" | "object"


def repair(content: bytes) -> bytes:
    """Rename `xl/SharedStrings.xml` to the name the relationships use."""
    try:
        source = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise ParseError("Файл не є книгою Excel (.xlsx)") from error
    names = source.namelist()
    wrong = [n for n in names if n.lower() == "xl/sharedstrings.xml" and n != "xl/sharedStrings.xml"]
    if not wrong:
        return content
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for name in names:
            data = source.read(name)
            if name in wrong:
                name = "xl/sharedStrings.xml"
            elif name.endswith(".rels") or name == "[Content_Types].xml":
                data = re.sub(rb"(?i)sharedstrings\.xml", b"sharedStrings.xml", data)
            target.writestr(name, data)
    return out.getvalue()


def _day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    found = _DATE.match(str(value)) if value is not None else None
    if not found:
        return None
    try:
        return date(int(found.group(3)), int(found.group(2)), int(found.group(1)))
    except ValueError:
        return None


def _header(sheet) -> tuple[int, dict[int, date]]:
    """The row with the dates, and the day of each of its columns."""
    best: tuple[int, dict[int, date]] = (0, {})
    for row in range(1, min(sheet.max_row, _HEADER_ROWS) + 1):
        days = {}
        for column in range(1, sheet.max_column + 1):
            day = _day(sheet.cell(row, column).value)
            if day is not None:
                days[column] = day
        if len(days) > len(best[1]):
            best = (row, days)
    if len(best[1]) < _MIN_DATES:
        raise ParseError(
            "У файлі не знайдено рядка з датами по днях — це не подобовий звіт 1С")
    return best


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse(content: bytes) -> ParsedFile:
    """One export → rows. Raises ParseError with a sentence for the operator."""
    try:
        book = openpyxl.load_workbook(io.BytesIO(repair(content)), data_only=True)
    except ParseError:
        raise
    except Exception as error:                  # noqa: BLE001 — any broken file
        raise ParseError(f"Не вдалося відкрити файл: {error}") from error
    sheet = book.active
    header_row, days = _header(sheet)
    first_day_column = min(days)
    title = " ".join(str(sheet.cell(r, 2).value or "").strip() for r in (1, 2)).strip()

    # Where the figures start: the first row under the header with a number
    # in a day column.
    start = None
    for row in range(header_row + 1, sheet.max_row + 1):
        if any(_number(sheet.cell(row, c).value) is not None for c in list(days)[:40]):
            start = row
            break
    if start is None:
        raise ParseError("У файлі немає жодного числа під датами")

    levels = {sheet.row_dimensions[r].outline_level for r in range(start, sheet.max_row + 1)}
    parsed = ParsedFile(kind=INFLOW, days=sorted(set(days.values())), title=title)

    def figures(row: int):
        for column, day in days.items():
            value = _number(sheet.cell(row, column).value)
            if value is not None:
                yield day, value

    if 3 in levels:
        # Organisation › branch › category (level 2) › object (level 3); the
        # object's GRS is in the column just before the first day.
        parsed.kind, parsed.layout = CONSUMPTION, "object"
        grs_column = first_day_column - 1
        category = None
        totals: dict[tuple[date, str, str], float] = {}
        for row in range(start, sheet.max_row + 1):
            level = sheet.row_dimensions[row].outline_level
            if level == 2:
                category = _text(sheet, row, range(2, first_day_column))
            elif level == 3 and category:
                grs = str(sheet.cell(row, grs_column).value or "").strip()
                if not grs:
                    continue
                for day, value in figures(row):
                    key = (day, grs, category)
                    totals[key] = totals.get(key, 0.0) + value
        parsed.rows = [(d, g, c, v) for (d, g, c), v in totals.items()]
    elif 1 in levels:
        # A GRS row (level 0) with its categories (level 1) under it. The GRS
        # row itself is their sum and is not stored.
        parsed.kind, parsed.layout = CONSUMPTION, "category"
        grs = None
        for row in range(start, sheet.max_row + 1):
            name = _text(sheet, row, range(2, first_day_column))
            if not name or name == TOTAL:
                continue
            if sheet.row_dimensions[row].outline_level == 0:
                grs = name
            elif grs:
                parsed.rows.extend((day, grs, name, value) for day, value in figures(row))
    else:
        parsed.layout = "grs"
        for row in range(start, sheet.max_row + 1):
            name = _text(sheet, row, range(2, first_day_column))
            if not name or name == TOTAL:
                continue
            parsed.rows.extend((day, name, INFLOW_CATEGORY, value) for day, value in figures(row))

    if not parsed.rows:
        raise ParseError("У файлі не знайдено жодного значення по ГРС")
    return parsed


def _text(sheet, row: int, columns) -> str:
    for column in columns:
        value = sheet.cell(row, column).value
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ── names ──────────────────────────────────────────────────────────────────

_RING = re.compile(r"^(ГРС[ -]?[123] Запоріжжя|Кільцева система)")
_RECEPTION = re.compile(r"^Приймання \((.+?)\)")
_REGION = re.compile(r"\s*\((?:Запорізька|Дніпропетровська|[А-ЯІЇЄҐ][а-яіїєґ']+ська) обл\.\)")

RING_NAME = "Кільце ГРС-1,2,3 Запоріжжя"


def grs_name(raw: str) -> str:
    """The name a GRS is kept under, whatever a report calls it.

    The two reports do not agree: the balance has one row for the ring of
    Zaporizhzhia («Кільцева система м.Запоріжжя») where the consumption names
    its three stations apart; one writes «(Запорізька обл.)» after a name and
    the other does not; a reception point carries the supplier after a dash.
    """
    name = " ".join(str(raw).split())
    if _RING.match(name):
        return RING_NAME
    found = _RECEPTION.match(name)
    if found:
        return f"Приймання ({found.group(1)})"
    return _REGION.sub("", name).strip()
