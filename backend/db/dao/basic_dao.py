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
        self.session = DbEngine().get_session()
        self.logger = logger_setup("backend")

    def bulk_upsert(self, list_of_instance: list, list_of_constraints: list[str], chunk_size: int = 900):
        chunks = [list_of_instance[i:i + chunk_size] for i in range(0, len(list_of_instance), chunk_size)]
        with self.session.begin():
            for chunk in chunks:
                stmt = insert(self.model).values([instance.model_dump() for instance in chunk])
                stmt = stmt.on_conflict_do_update(
                    index_elements=list_of_constraints,
                    set_={col: getattr(stmt.excluded, col) for col in self.model.model_fields if col != "id"}
                )
                self.session.execute(stmt)
            self.session.commit()

    def get_all(self):
        with self.session as session:
            return session.exec(select(self.model)).all()

    def get_range(self, from_date: datetime, to_date: datetime):
        statement = select(self.model).where(self.model.period >= from_date).where(self.model.period <= to_date)
        with self.session as session:
            return session.exec(statement).all()

    def get_by_id(self, item_id: int):
        with self.session as session:
            return session.get(self.model, item_id)

    def update_by_id(self, item_id: int, item):
        item_db = self.get_by_id(item_id)
        if item_db:
            item_data = item.model_dump(exclude_unset=True)
            item_db.sqlmodel_update(item_data)
            with self.session as session:
                session.add(item_db)
                session.commit()
                session.refresh(item_db)
        return item_db

    def create_item(self, item):
        db_item = self.model.model_validate(item)
        try:
            with self.session as session:
                session.add(db_item)
                session.commit()
                session.refresh(db_item)
        except IntegrityError as e:
            self.logger.exception(e)
            session.rollback()
            raise DatabaseIntegrityError("Create item integrity error!")
        return db_item

    def delete_item(self, item_id: int):
        db_item = self.get_by_id(item_id)
        if db_item:
            with self.session as session:
                session.delete(db_item)
                session.commit()
            return True
        return False
