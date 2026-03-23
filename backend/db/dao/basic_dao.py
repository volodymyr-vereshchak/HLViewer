from datetime import datetime

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, desc
from sqlalchemy.exc import IntegrityError

from utils.logger import logger_setup
from backend.db.dao.custom_exceptions import DatabaseIntegrityError


class BasicDao:
    def __init__(self, session: AsyncSession):
        self.model = None
        self.logger = logger_setup("backend")
        self.session = session

    async def bulk_upsert(
        self,
        list_of_dict_data: list,
        list_of_constraints: list[str],
        commit: bool = True,
    ):
        try:
            stmt = insert(self.model).values(
                [instance for instance in list_of_dict_data]
            )
            stmt = stmt.on_conflict_do_nothing(
                index_elements=list_of_constraints,
            )
            await self.session.execute(stmt)
            if commit:
                await self.session.commit()
        except Exception as e:
            self.logger.error(
                f"Unexpected error occurred while bulk upsert: {e}", exc_info=True
            )
            await self.session.rollback()
            raise

    async def bulk_upsert_via_copy(
        self,
        all_records: list,
        list_of_constraints: list[str],
    ):
        """Fast bulk upsert: asyncpg COPY into temp table (no constraint checks),
        then single INSERT...WHERE NOT EXISTS (hash join, O(n) vs O(n log m) per-row)."""
        if not all_records:
            return
        try:
            # Map Python field names → actual DB column names (handles mixed case like A0su)
            field_to_col = {col.key: col.name for col in self.model.__table__.columns}
            data_keys = list(all_records[0].keys())
            data_col_names = [field_to_col.get(k, k) for k in data_keys]

            # Columns NOT in data records but required (created_at, updated_at with Python defaults)
            auto_now_cols = [
                col.name for col in self.model.__table__.columns
                if col.key not in data_keys
                and not col.nullable
                and col.name not in ("id",)
                and col.default is not None
            ]

            main_table = self.model.__tablename__
            temp_table = f"_tmp_{main_table}"

            # Create temp table with positional aliases c0, c1, c2, ...
            # This avoids all case-sensitivity issues (D20 vs d20, A0su, etc.)
            # and is always safe for asyncpg COPY column list.
            temp_aliases = [f"c{i}" for i in range(len(data_col_names))]
            aliases = ", ".join(
                f'"{orig}" AS c{i}' for i, orig in enumerate(data_col_names)
            )
            await self.session.execute(text(
                f"CREATE TEMP TABLE {temp_table} AS "
                f"SELECT {aliases} FROM {main_table} WHERE FALSE"
            ))

            # Get raw asyncpg connection (shares the session's transaction)
            sa_conn = await self.session.connection()
            raw = await sa_conn.get_raw_connection()
            asyncpg_conn = raw.driver_connection

            # COPY all records — pure binary append, zero constraint overhead
            records = [tuple(r[k] for k in data_keys) for r in all_records]
            await asyncpg_conn.copy_records_to_table(
                temp_table, records=records, columns=temp_aliases
            )

            # INSERT ... ON CONFLICT DO NOTHING — atomic, safe under concurrent inserts
            insert_cols = ", ".join(f'"{n}"' for n in data_col_names)
            select_cols = ", ".join(f"t.c{i}" for i in range(len(data_col_names)))

            if auto_now_cols:
                auto_insert = ", " + ", ".join(f'"{n}"' for n in auto_now_cols)
                auto_select = ", " + ", ".join("NOW()" for _ in auto_now_cols)
            else:
                auto_insert = auto_select = ""

            conflict_cols = ", ".join(f'"{c}"' for c in list_of_constraints)

            await self.session.execute(text(
                f"INSERT INTO {main_table} ({insert_cols}{auto_insert}) "
                f"SELECT {select_cols}{auto_select} FROM {temp_table} t "
                f"ON CONFLICT ({conflict_cols}) DO NOTHING"
            ))

            await self.session.execute(text(f"DROP TABLE {temp_table}"))
            await self.session.commit()
        except Exception as e:
            self.logger.error(
                f"Unexpected error in bulk_upsert_via_copy: {e}", exc_info=True
            )
            await self.session.rollback()
            raise

    async def bulk_upsert_with_update(
        self,
        list_of_dict_data: list,
        list_of_constraints: list[str],
    ):
        try:
            stmt = insert(self.model).values(list_of_dict_data)
            update_fields = {
                key: stmt.excluded[key] for key in list_of_dict_data[0].keys()
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=list_of_constraints, set_=update_fields
            )
            await self.session.execute(stmt)
            await self.session.commit()
        except Exception as e:
            self.logger.error(
                f"Unexpected error occurred while bulk upsert: {e}", exc_info=True
            )
            await self.session.rollback()
            raise

    async def get_all(self):
        result = await self.session.execute(select(self.model))
        return result.scalars().all()

    async def get_last_period(self):
        result = await self.session.execute(select(func.max(self.model.period)))
        return result.scalar()

    async def get_range(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
    ):
        statement = select(self.model)
        if from_date:
            statement = statement.where(self.model.period >= from_date)
        if to_date:
            statement = statement.where(self.model.period <= to_date)
        if line_id:
            statement = statement.where(self.model.line_id.in_(line_id))

        result = await self.session.execute(statement)
        return result.scalars().all()

    async def get_last_for_to_date(
        self,
        to_date: datetime = None,
        line_id: list[int] = None,
    ):
        statement = select(self.model).order_by(desc(self.model.period))
        if to_date:
            statement = statement.where(self.model.period <= to_date)
        if line_id:
            statement = statement.where(self.model.line_id.in_(line_id))

        statement = statement.limit(1)

        result = await self.session.execute(statement)
        return result.scalars().first()

    async def get_by_id(self, item_id: int):
        result = await self.session.get(self.model, item_id)
        return result

    async def update_by_id(self, item_id: int, item):
        item_db = await self.get_by_id(item_id)
        if item_db:
            item_data = item.model_dump(exclude_unset=True)
            for key, value in item_data.items():
                setattr(item_db, key, value)

            try:
                self.session.add(item_db)
                await self.session.commit()
                await self.session.refresh(item_db)
            except Exception as e:
                await self.session.rollback()
                self.logger.error(
                    f"Unexpected error occurred while updating: {e}", exc_info=True
                )
                raise
        return item_db

    async def create_item(self, item):
        db_item = self.model.model_validate(item)
        try:
            self.session.add(db_item)
            await self.session.commit()
            await self.session.refresh(db_item)
        except IntegrityError as e:
            self.logger.exception(e)
            await self.session.rollback()
            raise DatabaseIntegrityError(str(e.orig)) from e
        except Exception as e:
            await self.session.rollback()
            self.logger.error(f"Unexpected error occurred: {e}", exc_info=True)
            raise
        return db_item

    async def delete_item(self, item_id: int):
        db_item = await self.get_by_id(item_id)
        if db_item:
            await self.session.delete(db_item)
            await self.session.commit()
            return True
        return False

    async def get_by_gas_volume_type_id(self, gas_volume_type_id: int):
        statement = select(self.model).where(
            self.model.gas_volume_calc_type_id == gas_volume_type_id
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def get_data_counts_by_hour(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
    ):
        statement = select(
            func.date_trunc("hour", self.model.period).label("hour_group"),
            func.count().label("record_count"),
        )

        if from_date:
            statement = statement.where(self.model.period >= from_date)
        if to_date:
            statement = statement.where(self.model.period <= to_date)
        if line_id:
            statement = statement.where(self.model.line_id.in_(line_id))

        statement = statement.group_by("hour_group").order_by("hour_group")

        results = await self.session.execute(statement)
        results = results.all()

        return [
            {
                "hour_group": row.hour_group,
                "record_count": row.record_count,
            }
            for row in results
        ]
