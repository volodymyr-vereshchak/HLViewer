"""Часовий архів у компактній формі — один запит замість трьох.

Нічні витрати читають місяць годинних записів по кожній лінії філії, а
використовують з рядка три поля з восьми і дев'ять годин із двадцяти чотирьох.
У звичайному вигляді це коштувало 18 с і 69 МБ на місяць: ORM-об'єкт на кожен
рядок, потім `response_model`, потім `jsonable_encoder`, потім обхід
`_sanitize_nan` — дані обходяться чотири рази чистим Python.

Тут рядок — це трійка `[line_id, індекс штампа, volume]`, години відсіюються
в базі, а фізичні лінії, кільця й лінії ДПД приходять однією відповіддю, бо
звіт усе одно зливає їх в одну купу. Ті самі дані — 0.3 с і 2 МБ.

Штамп їде стінним часом ("YYYY-MM-DDTHH"), а не моментом. Це не дрібниця:
звіт зіставляє годину архіву з годиною промисловості, а та приходить наївним
ISO-рядком і читається як стінний час. Перетвори архів на epoch — і браузер в
іншому часовому поясі мовчки зістикував би не ті години, тобто відняв би не ту
промисловість. Стінний штамп робить звіт незалежним від поясу з обох боків, і
це рівно той ключ, яким уже склеюють дані (`enterprisePeriodKey`).

Штампів за місяць близько семисот, а рядків — сотні тисяч, тож штампи
передаються один раз списком, а рядок посилається на індекс.
"""

import asyncio
import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_allowed_line_ids, get_branch_filter
from backend.db.dao.dpd_line_dao import DpdLineArchiveDao
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.engine import get_session
from backend.db.models.dpd_line_model import DpdLine
from backend.services.virtual_lines_config import get_active_virtual_lines_db

router = APIRouter()


def _build_json(rows) -> str:
    """(line_id, period, volume) triples → the response body.

    Runs in an executor: a month over a branch is a few hundred thousand
    triples, and stringifying them on the event loop stalls every other
    request the worker is serving.
    """
    stamps: List[str] = []
    index: dict = {}
    out = []
    for line_id, period, volume in rows:
        idx = index.get(period)
        if idx is None:
            idx = len(stamps)
            index[period] = idx
            stamps.append(period.strftime("%Y-%m-%dT%H"))
        out.append((line_id, idx, volume))
    return json.dumps({"stamps": stamps, "rows": out}, separators=(",", ":"))


@router.get("/hourly_compact/", tags=["hourly"])
async def get_hourly_compact(
    from_date: datetime = Query(None, description="Початок періоду"),
    to_date: datetime = Query(None, description="Кінець періоду"),
    line_id: List[int] = Query(None, description="Лінії будь-якого виду: фізичні, кільця, ДПД"),
    hours: Optional[List[int]] = Query(
        None, description="Тільки ці години доби (0–23); без параметра — усі"
    ),
    allowed_line_ids: Optional[List[int]] = Depends(get_allowed_line_ids),
    allowed_branches: Optional[List[int]] = Depends(get_branch_filter),
    session: AsyncSession = Depends(get_session),
):
    if not from_date or not to_date:
        raise HTTPException(status_code=400, detail="Вкажіть початок і кінець періоду")
    if hours and any(h < 0 or h > 23 for h in hours):
        raise HTTPException(status_code=400, detail="Години мають бути в межах 0–23")
    if not line_id:
        return Response(content='{"stamps":[],"rows":[]}', media_type="application/json")

    # Кільце просять під його власним id, а читають архіви його учасників —
    # тому кожен учасник несе список кілець, у які має потрапити його об'єм.
    virtual_config = await get_active_virtual_lines_db(session)
    ring_of: dict[int, List[int]] = {}
    direct: set[int] = set()
    # Deduplicated: a repeated id would otherwise add its member's volume to
    # the same ring once per repetition.
    for lid in dict.fromkeys(line_id):
        members = virtual_config.get(str(lid), {}).get("physical_line_ids") if virtual_config else None
        if members:
            for member in members:
                ring_of.setdefault(member, []).append(lid)
        else:
            direct.add(lid)

    wanted = direct | set(ring_of)
    # Лінії ДПД живуть в іншій таблиці — розділяємо за наявністю в dpd_line.
    dpd_stmt = select(DpdLine.id).where(DpdLine.id.in_(wanted))
    if allowed_branches is not None:
        dpd_stmt = dpd_stmt.where(DpdLine.branch_id.in_(allowed_branches))
    dpd_ids = set((await session.execute(dpd_stmt)).scalars())
    physical_ids = wanted - dpd_ids
    # Ті самі права, що й у /hourly/ і /hourly_dpd/, причому й для учасників
    # кільця. Кільце не виходить за межі ЛУМГ, а ЛУМГ належить одній філії —
    # тож той, кому видно кільце, бачить і всіх його учасників, і це звуження
    # не може відрізати нічого в законного користувача.
    if allowed_line_ids is not None:
        physical_ids &= set(allowed_line_ids)

    rows: List[tuple] = []
    if physical_ids:
        rows.extend(await HourlyArchiveDao(session=session).load_volumes(
            from_date, to_date, sorted(physical_ids), hours
        ))
    if dpd_ids:
        rows.extend(await DpdLineArchiveDao(session).load_hourly_volumes(
            sorted(dpd_ids), from_date, to_date, hours
        ))

    if ring_of:
        rows = _fold_rings(rows, direct, ring_of)

    body = await asyncio.get_running_loop().run_in_executor(None, _build_json, rows)
    return Response(content=body, media_type="application/json")


def _fold_rings(rows, direct: set, ring_of: dict) -> List[tuple]:
    """Add a ring's total per period, keeping the members' own rows.

    A line can belong to several rings and still be requested in its own
    right; summing the volume is all the night report needs of a ring (the
    weighted pressure/temperature averages `/hourly_virtual/` computes have no
    reader here).
    """
    totals: dict = {}
    out = []
    for line_id, period, volume in rows:
        if line_id in direct:
            out.append((line_id, period, volume))
        for ring_id in ring_of.get(line_id, ()):
            key = (ring_id, period)
            totals[key] = totals.get(key, 0.0) + (volume or 0.0)
    out.extend((ring_id, period, volume) for (ring_id, period), volume in totals.items())
    return out
