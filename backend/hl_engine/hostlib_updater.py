from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd
import emoji

from backend.api.endpoints.root_ep import RootRouter
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.dao.line_dao import LineDao
from backend.db.engine import async_session_factory
from backend.db.models import HourlyArchiveList
from backend.settings import backend_settings
from backend.telegram_notifier.telegram_norifier import TelegramBot


class HostlibUpdater:
    @staticmethod
    async def update_hostlibs():
        root = RootRouter()
        await root.update_data()

    @staticmethod
    async def send_telegram_message(message: str):
        await TelegramBot().send_updates(message)

    @staticmethod
    async def create_message(df: pd.DataFrame):
        lines = backend_settings.get("LINES_IDS")
        high_p_lines = backend_settings.get("HIGH_P_LINES_IDS")
        attention_text = emoji.emojize(":red_circle:")
        message = "Объем по ГРС за последние 24 часа:\n"
        df_lines = df[df.line_id.isin(lines)]
        volume_lines = df_lines.volume.sum()
        start_lines = df_lines.period.min()
        end_lines = df_lines.period.max()
        message += f"{start_lines.strftime('%d-%m-%Y %H:%M')} - {end_lines.strftime('%d-%m-%Y %H:%M')}\n\n"
        message += "<b>ГРС</b> всего: {:,} м³;\n\n".format(volume_lines).replace(
            ",", " "
        )
        for line_id in lines:
            df_i = df[df.line_id == line_id]
            df_len = df_i.shape[0]
            async with async_session_factory() as session:
                line = await LineDao(session=session).get_line_name_by_id(line_id)
            volume = df_i.volume.sum()
            df_last = df_i.tail(1)
            p_out = df_last.pressure.sum()
            if not line.meter:
                p_out = p_out - df_last.w_volume_dp.sum() / 10_000
            p_out = p_out.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if df_len != 24:
                message += attention_text
            if line_id not in high_p_lines:
                message += "<b>{}</b>: {:,} м³; Pвых: {} кг/см²\n\n".format(
                    line.name, volume, p_out
                ).replace(",", " ")
            else:
                message += "<b>{}</b>: {:,} м³;\n\n".format(line.name, volume).replace(
                    ",", " "
                )

        return message

    async def update_and_send_notification(self):
        await self.update_hostlibs()
        async with async_session_factory() as session:
            end = await HourlyArchiveDao(session=session).get_last_period()
            start = end - timedelta(hours=23)
            result = await HourlyArchiveDao(session=session).get_range(
                from_date=start, to_date=end
            )
        extracted_data = [
            HourlyArchiveList(**vars(item)).model_dump() for item in result
        ]
        df = pd.DataFrame(extracted_data).sort_values("period")
        await self.send_telegram_message(await self.create_message(df))


if __name__ == "__main__":
    import asyncio

    asyncio.run(HostlibUpdater().update_and_send_notification())
