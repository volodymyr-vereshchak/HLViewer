from contextlib import contextmanager
from datetime import datetime

from sqlalchemy.dialects.sqlite import insert
from sqlmodel import select
from sqlalchemy.exc import IntegrityError

from backend.db.engine import DbEngine
from utils.logger import logger_setup
from backend.db.dao.custom_exceptions import DatabaseIntegrityError


class BasicDao:
    def __init__(self):
        self.model = None
        self.logger = logger_setup("backend")

    @staticmethod
    @contextmanager
    def get_session():
        session = DbEngine().get_session()
        try:
            yield session
        except Exception as exc:
            session.rollback()
            raise exc
        finally:
            session.close()

    def bulk_upsert(
        self,
        list_of_instance: list,
        list_of_constraints: list[str],
        chunk_size: int = 900,
    ):
        unique_instances = {
            tuple(getattr(inst, key) for key in list_of_constraints): inst
            for inst in list_of_instance
        }
        filtered_instances = list(unique_instances.values())

        chunks = [
            filtered_instances[i : i + chunk_size]
            for i in range(0, len(filtered_instances), chunk_size)
        ]
        with self.get_session() as session:
            try:
                for chunk in chunks:
                    stmt = insert(self.model).values(
                        [instance.model_dump() for instance in chunk]
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=list_of_constraints,
                        set_={
                            col: getattr(stmt.excluded, col)
                            for col in self.model.model_fields
                            if col != "id"
                        },
                    )
                    session.execute(stmt)
                session.commit()
            except Exception as e:
                session.rollback()
                self.logger.error(
                    f"Unexpected error occurred while bulk upsert: {e}", exc_info=True
                )
                raise

    def get_all(self):
        with self.get_session() as session:
            return session.exec(select(self.model)).all()

    def get_range(self, from_date: datetime, to_date: datetime):
        statement = (
            select(self.model)
            .where(self.model.period >= from_date)
            .where(self.model.period <= to_date)
        )
        with self.get_session() as session:
            return session.exec(statement).all()

    def get_by_id(self, item_id: int):
        with self.get_session() as session:
            return session.get(self.model, item_id)

    def update_by_id(self, item_id: int, item):
        item_db = self.get_by_id(item_id)
        if item_db:
            item_data = item.model_dump(exclude_unset=True)
            item_db.sqlmodel_update(item_data)
            with self.get_session() as session:
                try:
                    session.add(item_db)
                    session.commit()
                    session.refresh(item_db)
                except Exception as e:
                    session.rollback()
                    self.logger.error(
                        f"Unexpected error occurred while update: {e}", exc_info=True
                    )
                    raise
        return item_db

    def create_item(self, item):
        db_item = self.model.model_validate(item)
        with self.get_session() as session:
            try:
                session.add(db_item)
                session.commit()
                session.refresh(db_item)
            except IntegrityError as e:
                self.logger.exception(e)
                session.rollback()
                raise DatabaseIntegrityError("Create item integrity error!")
            except Exception as e:
                session.rollback()
                self.logger.error(f"Unexpected error occurred: {e}", exc_info=True)
                raise
        return db_item

    def delete_item(self, item_id: int):
        db_item = self.get_by_id(item_id)
        if db_item:
            with self.get_session() as session:
                session.delete(db_item)
                session.commit()
            return True
        return False

    def get_by_gas_volume_type_id(self, gas_volume_type_id: int):
        statement = select(self.model).where(
            self.model.gas_volume_calc_type_id == gas_volume_type_id
        )
        with self.get_session() as session:
            return session.exec(statement).all()
