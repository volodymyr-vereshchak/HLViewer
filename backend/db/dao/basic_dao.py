from sqlalchemy.dialects.sqlite import insert
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
