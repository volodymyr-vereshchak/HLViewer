from datetime import datetime

from sqlalchemy import func
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
    ):
        try:
            stmt = insert(self.model).values(
                [instance for instance in list_of_dict_data]
            )
            stmt = stmt.on_conflict_do_nothing(
                index_elements=list_of_constraints,
            )
            await self.session.execute(stmt)
            await self.session.commit()
        except Exception as e:
            self.logger.error(
                f"Unexpected error occurred while bulk upsert: {e}", exc_info=True
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
            raise DatabaseIntegrityError("Create item integrity error!")
        except Exception as e:
            await self.session.rollback()
            self.logger.error(f"Unexpected error occurred: {e}", exc_info=True)
            raise
        return db_item

    async def delete_item(self, item_id: int):
        db_item = self.get_by_id(item_id)
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
