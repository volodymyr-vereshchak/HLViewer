from sqlalchemy.dialects.sqlite import insert
from sqlmodel import select

from backend.db.engine import DbEngine


class BasicDao:
    def __init__(self):
        self.model = None
        self.session = DbEngine().get_session()

    def bulk_upsert(self, list_of_instance: list, list_of_constraints: list[str]):
        stmt = insert(self.model).values([instance.model_dump() for instance in list_of_instance])
        stmt = stmt.on_conflict_do_update(
            index_elements=list_of_constraints,
            set_={col: getattr(stmt.excluded, col) for col in self.model.model_fields if col != "id"}
        )
        with self.session as session:
            session.exec(stmt)
            session.commit()

    def get_all(self):
        with self.session as session:
            return session.exec(select(self.model)).all()

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
        with self.session as session:
            session.add(db_item)
            session.commit()
            session.refresh(db_item)
        return db_item
