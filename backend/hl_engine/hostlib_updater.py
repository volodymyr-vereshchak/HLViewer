from datetime import timedelta

import pandas as pd

from backend.api.endpoints.root_ep import RootRouter
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.dao.line_dao import LineDao
from backend.db.engine import async_session_factory
from backend.db.models import HourlyArchiveList
from backend.settings import backend_settings
from backend.telegram_notifier.telegram_norifier import TelegramNotifier


class HostlibUpdater:
    @staticmethod
    async def update_hostlibs():
        root = RootRouter()
        await root.update_data()

    @staticmethod
    async def send_telegram_message(message: str):
        await TelegramNotifier().send_message(message)

    async def create_message(self, df: pd.DataFrame):
        lines = backend_settings.get("LINES_IDS")
        message = "Объем по ГРС за последние 24 часа:\n"
        df_lines = df[df.line_id.isin(lines)]
        volume_lines = df_lines.volume.sum()
        start_lines = df_lines.period.min()
        end_lines = df_lines.period.max()
        message += f"{start_lines.strftime('%d-%m-%Y %H:%M')} - {end_lines.strftime('%d-%m-%Y %H:%M')}\n"
        message += "ГРС всего: {:,} м³;\n".format(volume_lines).replace(",", " ")
        for line in lines:
            df_i = df[df.line_id == line]
            async with async_session_factory() as session:
                name_i = await LineDao(session=session).get_line_name_by_id(line)
            volume_i = df_i.volume.sum()
            message += "{}: {:,} м³;\n".format(name_i, volume_i).replace(",", " ")

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
