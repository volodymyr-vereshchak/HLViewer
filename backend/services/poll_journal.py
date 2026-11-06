"""The log of a site's poll, kept as a plain file.

One file per site, rewritten by every session. It is both things at once: the
screen follows it line by line while the call runs, and it is still there
afterwards, which is when somebody asks what the last poll actually did.

There used to be a second copy of the same text in a `poll_log` table, for the
live screen. Two places writing one account of one phone call is one place too
many — and the database copy was deleted at the start of every session, so the
question asked five minutes later had no answer at all.

A history of every call is deliberately not kept: it would be a folder nobody
prunes and nobody reads, while the last call is looked at constantly. What did
happen is in `poll_attempt`, one row per attempt.

Nothing here may interrupt a poll. A full disk, a read-only folder, a path
somebody renamed — all of that is worth a line in the server log and nothing
more: the readings are the point of the call, the commentary is not.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

log = logging.getLogger(__name__)

#: Read back at most this much. A session is a few hundred lines; anything
#: larger is a runaway, and sending it to a browser helps nobody.
MAX_BYTES = 256 * 1024

#: The technical journal holds every frame of the call, so it is allowed to be
#: larger — but only by this much, and only for one call.
MAX_DEBUG_BYTES = 4 * 1024 * 1024


def journal_path(directory: str | Path, enterprise_id: Optional[int],
                 card_id: int) -> Path:
    """One file per site, named by what a person would look for.

    By enterprise where there is one — that is the name on the screen and the
    thing that survives a corrector being replaced. Cards that point straight
    at a DPD device or line keep their own id: rare, and still has to land
    somewhere.
    """
    folder = Path(directory)
    name = (f"enterprise-{enterprise_id}" if enterprise_id is not None
            else f"card-{card_id}")
    return folder / f"{name}.log"


def debug_path(path: Path) -> Path:
    """The technical journal beside the ordinary one.

    Kept exactly the same way — one file per site, rewritten by every call —
    because a fault on a line is the same fault on every call, and one call's
    worth of frames is what anybody actually reads. What it adds is the level
    below the operator's account: every frame sent and received, how long the
    corrector took to answer, what was discarded before it.
    """
    return path.with_suffix(path.suffix + ".debug")


def start(path: Path, title: str) -> None:
    """Begin a session's files, replacing whatever the last one left.

    Both of them: the operator's account and the technical one are two views
    of one call, and a technical journal left over from the previous call
    beside a fresh account of this one is worse than none.
    """
    for target in (path, debug_path(path)):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8") as handle:
                handle.write(_line("info", f"── {title}"))
            _stamp_mtime(target)
        except OSError as error:
            log.warning("Журнал опитування не почався (%s): %s", target, error)


def append(path: Path, lines: Iterable[dict]) -> None:
    """Add what the agent has just reported.

    Everything goes to the technical journal; the operator's one gets the
    account meant for a person. Splitting here rather than at the agent keeps
    one report of one call travelling over the line.
    """
    lines = list(lines)
    _write(debug_path(path), lines, MAX_DEBUG_BYTES)
    _write(path, [line for line in lines
                  if str(line.get("level", "info")) != "debug"], MAX_BYTES * 8)


def _write(path: Path, lines: Iterable[dict], cap: int) -> None:
    rows = [
        _line(str(line.get("level", "info")), str(line.get("message", "")))
        for line in lines
    ]
    if not rows:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A call that never stops talking must not fill the disk: past the cap
        # the file keeps what it has and says so once.
        if path.is_file() and path.stat().st_size > cap:
            return
        with path.open("a", encoding="utf-8") as handle:
            handle.writelines(rows)
        _stamp_mtime(path)
    except OSError as error:
        log.warning("Журнал опитування не дописався (%s): %s", path, error)


def finish(path: Path, status: str, error_text: Optional[str],
           rows: Optional[dict], duration_ms: Optional[int]) -> None:
    """Close the file with the outcome, so it ends with the answer."""
    counts = rows or {}
    level = "info" if status == "ok" else "error"
    tail = (f"── {'Готово' if status == 'ok' else 'Помилка'}: "
            f"годин {counts.get('hour', 0)}, діб {counts.get('day', 0)}")
    if duration_ms:
        tail += f", тривалість {duration_ms / 1000:.0f} с"
    text = _line(level, tail)
    if status != "ok" and error_text:
        text += _line(level, f"── {error_text}")
    for target in (path, debug_path(path)):
        try:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(text)
            _stamp_mtime(target)
        except OSError as error:
            log.warning("Журнал опитування не закрився (%s): %s", target, error)


def read(path: Path, limit: int = MAX_BYTES) -> Optional[str]:
    """The file as text, or None when this site has never been polled."""
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            if size > limit:
                handle.seek(size - limit)
                # The seek lands mid-line; that half line is not worth showing.
                handle.readline()
                return "…\n" + handle.read()
            return handle.read()
    except OSError as error:
        log.warning("Журнал опитування не прочитався (%s): %s", path, error)
        return None


#: "12.09.2026 17:15:40  INFO   Набираю…" — written for a person to read and
#: parsed back for the screen, which is why the year is in it: the browser
#: shows the time of each line and would otherwise be told 1900.
LINE_RE = re.compile(
    r"^(?P<ts>\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2})\s\s"
    r"(?P<level>[A-Z]+)\s+(?P<message>.*)$"
)


def session_of(path: Path) -> Optional[str]:
    """Which session the file holds: its first line, written by `start`.

    The lines are numbered by their place in the file, so a new session
    numbers them from one again. A screen that knows only "I have up to 40"
    would skip the first forty lines of the next call — this is what tells it
    the file has started over.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline().strip()
    except OSError:
        return None
    return first or None


def _stamp_mtime(path: Path) -> None:
    """Set the file's mtime from the application's clock.

    `started_before` compares the mtime with `polling_since`, which comes from
    `datetime.now()`. Linux stamps a write with the kernel's coarse clock, one
    tick (up to ~4 ms) behind it — a journal written a millisecond after the
    claim looked older than the claim, and the screen hid the new call.
    """
    now = time.time()
    try:
        os.utime(path, (now, now))
    except OSError:
        pass                                         # cosmetic, see module doc


def started_before(path: Path, moment: datetime) -> bool:
    """Was the file last written before `moment` — a previous session's."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime) < moment
    except OSError:
        return True


def read_lines(path: Path, after_seq: int = 0) -> List[dict]:
    """The session's lines, numbered, from `after_seq` on.

    Numbered by position in the file, so a screen refreshing every second asks
    only for what it does not already have — the same contract the database
    log had, and the reason it could be replaced without touching the browser.
    """
    text = read(path)
    if not text:
        return []

    out: List[dict] = []
    for index, raw in enumerate(text.splitlines(), start=1):
        if index <= after_seq or not raw.strip():
            continue
        match = LINE_RE.match(raw)
        if match is None:
            # A line from a version that wrote them differently, or one the
            # corrector itself put there. Shown as it is rather than dropped.
            out.append({"seq": index, "ts": None, "level": "info",
                        "message": raw.strip()})
            continue
        level = match.group("level")
        out.append({
            "seq": index,
            "ts": datetime.strptime(match.group("ts"), "%d.%m.%Y %H:%M:%S"),
            "level": "error" if level == "ERROR" else "info",
            "message": match.group("message"),
        })
    return out


def _line(level: str, message: str) -> str:
    # The agent logs answers exactly as they came off the wire, and a
    # corrector's identity block is padded with NULs. A control byte inside a
    # line would split or truncate it for every reader afterwards — and the
    # request carrying it is the report of a call that cannot be repeated.
    clean = "".join(" " if c < " " else c for c in message)[:1000]
    return f"{_stamp()}  {level.upper():<5} {clean}\n"


def _stamp() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")
