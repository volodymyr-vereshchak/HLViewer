from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd
import emoji

from backend.hl_engine.main import update_hostlibs as update_hostlibs_main
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.dao.line_dao import LineDao
from backend.db.engine import async_session_factory
from backend.db.models import HourlyArchiveList
from backend.settings import backend_settings
from backend.telegram_notifier.email_notifier import EmailNotifier
from backend.telegram_notifier.telegram_norifier import TelegramBot
from backend.services.virtual_lines_config import (
    get_active_virtual_lines,
    get_active_virtual_lines_db,
)


class HostlibUpdater:
    @staticmethod
    async def aggregate_virtual_lines(df: pd.DataFrame) -> pd.DataFrame:
        """
        Aggregate physical lines into virtual lines in DataFrame.

        For virtual lines:
        - Sum volumes from constituent physical lines
        - Average pressure and temperature
        - Use max density

        Tries DB first, falls back to JSON config.

        Args:
            df: DataFrame with hourly archive data (physical lines)

        Returns:
            DataFrame with aggregated data for virtual lines
        """
        async with async_session_factory() as session:
            virtual_lines = await get_active_virtual_lines_db(session)
        if not virtual_lines:
            virtual_lines = get_active_virtual_lines()  # JSON fallback

        result_rows = []
        for vline_id_str, vline_data in virtual_lines.items():
            vline_id = int(vline_id_str)
            physical_line_ids = vline_data["physical_line_ids"]

            # Filter data for physical lines in this virtual line
            df_physical = df[df['line_id'].isin(physical_line_ids)]

            if df_physical.empty:
                continue

            # Group by period and aggregate
            for period, group in df_physical.groupby('period'):
                aggregated = {
                    'period': period,
                    'line_id': vline_id,
                    'volume': group['volume'].sum(),
                    'w_volume_dp': group['w_volume_dp'].mean(),  # Average
                    'pressure': group['pressure'].mean(),  # Average
                    'temperature': group['temperature'].mean(),  # Average
                    'density': group['density'].max(),  # Max
                    'created_at': group['created_at'].max(),
                    'updated_at': group['updated_at'].max(),
                    'id': None  # Virtual line doesn't have DB id
                }
                result_rows.append(aggregated)

        return pd.DataFrame(result_rows)

    @staticmethod
    async def update_hostlibs(session):
        await update_hostlibs_main(session=session)

    @staticmethod
    async def send_telegram_message(message: str):
        bot = TelegramBot()
        await bot.send_updates(message)
        await bot.bot.session.close()

    @staticmethod
    def send_email_message(message: str):
        EmailNotifier().send_message(message=message)

    @staticmethod
    async def get_line_name(line_id: int) -> str:
        """Get line name for physical or virtual line (DB-backed)."""
        # Try virtual line first (DB)
        async with async_session_factory() as session:
            from backend.db.models.grmu_branch_model import VirtualLine
            vl = await session.get(VirtualLine, line_id)
            if vl:
                return vl.name

        # Physical line — get from database
        async with async_session_factory() as session:
            line = await LineDao(session=session).get_line_name_by_id(line_id)
            return line.name if line else f"Лінія {line_id}"

    @staticmethod
    async def create_message(df: pd.DataFrame, line_flags: dict) -> str:
        """
        Build Telegram notification message.

        Args:
            df: Combined DataFrame (physical + virtual lines)
            line_flags: {line_id: is_high_pressure} for all lines to include in report
        """
        lines = list(line_flags.keys())
        attention_text = emoji.emojize(":red_circle:")
        message = "Объем по ГРС за последние 24 часа:\n"
        df_lines = df[df.line_id.isin(lines)]
        volume_lines = df_lines.volume.sum()
        start_lines = df_lines.period.min()
        end_lines = df_lines.period.max() + timedelta(hours=1)
        message += f"{start_lines.strftime('%d-%m-%Y %H:%M')} - {end_lines.strftime('%d-%m-%Y %H:%M')}\n\n"
        message += "<b>ГРС</b> всего: {:,} м³;\n\n".format(
            round(volume_lines, 3)
        ).replace(",", " ")
        for line_id in lines:
            df_i = df[df.line_id == line_id]
            df_len = df_i.shape[0]

            # Get line name (works for both physical and virtual lines)
            line_name = await HostlibUpdater.get_line_name(line_id)

            volume = df_i.volume.sum()
            df_last = df_i.tail(1)
            p_out = df_last.pressure.sum()

            # Apply meter correction for physical lines (virtual lines return None from LineDao)
            async with async_session_factory() as session:
                line = await LineDao(session=session).get_line_name_by_id(line_id)
                if line and not line.meter:
                    p_out = p_out - df_last.w_volume_dp.sum() / 10_000

            p_out = (
                p_out.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if type(p_out) is Decimal
                else p_out
            )
            if df_len != 24:
                message += attention_text
            if not line_flags.get(line_id, False):
                message += "<b>{}</b>: {:,} м³; Pвых: {} кг/см²\n\n".format(
                    line_name, round(volume, 3), round(p_out, 3)
                ).replace(",", " ")
            else:
                message += "<b>{}</b>: {:,} м³; Pвх: {} кг/см²\n\n".format(
                    line_name, round(volume, 3), round(p_out, 3)
                ).replace(",", " ")

        return message

    @staticmethod
    async def create_email_message(df: pd.DataFrame, line_flags: dict) -> str:
        """
        Build HTML email notification message.

        Args:
            df: Combined DataFrame (physical + virtual lines)
            line_flags: {line_id: is_high_pressure} for all lines to include in report
        """
        lines = list(line_flags.keys())
        df_lines = df[df.line_id.isin(lines)]
        volume_lines = df_lines.volume.sum()
        start_lines = df_lines.period.min()
        end_lines = df_lines.period.max() + timedelta(hours=1)
        message = f"""
                <html>
                    <body>
                        <h1>Объем по ГРС за последние 24 часа</h1>
                        <p><b>Данные за период {start_lines.strftime('%d-%m-%Y %H:%M')} - {end_lines.strftime('%d-%m-%Y %H:%M')}:</b></p>
                        <table border="1">
                            <tr>
                                <th>Название ГРС</th>
                                <th>Объем, м³</th>
                                <th>Давление, кг/см² </th>
                            </tr>
                """
        formated_volume = "{:,}".format(volume_lines).replace(",", " ")
        message += f"""
                <tr>
                    <td><b>ГРС всего</b></td>
                    <td>{formated_volume}</td>
                    <td></td>
                </tr>
            """
        for line_id in lines:
            df_i = df[df.line_id == line_id]
            df_len = df_i.shape[0]

            # Get line name (works for both physical and virtual lines)
            line_name = await HostlibUpdater.get_line_name(line_id)

            volume = df_i.volume.sum()
            df_last = df_i.tail(1)
            p_out = df_last.pressure.sum()

            # Apply meter correction for physical lines (virtual lines return None from LineDao)
            async with async_session_factory() as session:
                line = await LineDao(session=session).get_line_name_by_id(line_id)
                if line and not line.meter:
                    p_out = p_out - df_last.w_volume_dp.sum() / 10_000

            p_out = (
                p_out.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if type(p_out) is not int
                else p_out
            )
            if df_len != 24:
                pass
            formated_volume = "{:,}".format(volume).replace(",", " ")
            if not line_flags.get(line_id, False):
                message += f"""
                                <tr>
                                    <td><b>{line_name}</b></td>
                                    <td>{formated_volume}</td>
                                    <td>{p_out}</td>
                                </tr>
                            """
            else:
                message += f"""
                                <tr>
                                    <td><b>{line_name}</b></td>
                                    <td>{formated_volume}</td>
                                    <td></td>
                                </tr>
                            """
        message += """
                </table>
            </body>
        </html>
        """
        return message

    async def update_and_send_notification(self):
        async with async_session_factory() as session:
            await self.update_hostlibs(session)
            end = await HourlyArchiveDao(session=session).get_last_period()
            start = end - timedelta(hours=23)
            result = await HourlyArchiveDao(session=session).get_range(
                from_date=start, to_date=end
            )
        extracted_data = [
            HourlyArchiveList(**vars(item)).model_dump() for item in result
        ]
        df_physical = pd.DataFrame(extracted_data).sort_values("period")

        # Build line_flags from DB (include_in_report / is_high_pressure)
        from sqlmodel import select
        from backend.db.models.line_model import Line
        from backend.db.models.grmu_branch_model import VirtualLine, VirtualLineMember

        async with async_session_factory() as session:
            phys_result = await session.execute(
                select(Line).where(Line.include_in_report == True)  # noqa: E712
            )
            phys_lines = phys_result.scalars().all()

            # Load virtual line → member physical line ID mappings
            vlm_result = await session.execute(
                select(VirtualLine.id, VirtualLineMember.line_id).join(
                    VirtualLineMember, VirtualLineMember.virtual_line_id == VirtualLine.id
                )
            )
            vlm_rows = vlm_result.all()

        line_flags = {l.id: l.is_high_pressure for l in phys_lines}

        # Derive is_high_pressure for virtual lines from their member physical lines
        vl_member_lines: dict[int, list[int]] = {}
        for vl_id, member_line_id in vlm_rows:
            vl_member_lines.setdefault(vl_id, []).append(member_line_id)
        for vl_id, member_ids in vl_member_lines.items():
            line_flags[vl_id] = any(line_flags.get(mid, False) for mid in member_ids)

        # Fallback to settings if DB has no flagged lines yet (pre-migration or empty DB)
        if not line_flags:
            lines_ids = backend_settings.get("LINES_IDS", [])
            high_p = set(backend_settings.get("HIGH_P_LINES_IDS", []))
            line_flags = {lid: (lid in high_p) for lid in lines_ids}

        # Aggregate virtual lines and combine with physical lines
        df_virtual = await self.aggregate_virtual_lines(df_physical)

        # Get physical lines that are NOT in virtual lines (use DB, fallback to JSON)
        async with async_session_factory() as session:
            virtual_lines = await get_active_virtual_lines_db(session)
        if not virtual_lines:
            virtual_lines = get_active_virtual_lines()

        physical_in_virtual = set()
        for vline_data in virtual_lines.values():
            physical_in_virtual.update(vline_data["physical_line_ids"])

        df_physical_filtered = df_physical[~df_physical['line_id'].isin(physical_in_virtual)]

        # Combine physical (not in virtual) + virtual lines
        df = pd.concat([df_physical_filtered, df_virtual], ignore_index=True).sort_values("period")

        message = await self.create_message(df, line_flags)
        await self.send_telegram_message(message)
        # message = await self.create_email_message(df, line_flags)
        # self.send_email_message(message)


if __name__ == "__main__":
    import asyncio

    asyncio.run(HostlibUpdater().update_and_send_notification())
