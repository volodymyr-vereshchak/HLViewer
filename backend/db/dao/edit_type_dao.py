from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import EditType


class EditTypeDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = EditType


if __name__ == "__main__":
    from backend.db.models import EditTypeCreate

    edit_type_dao = EditTypeDao()
    res = edit_type_dao.get_by_gas_volume_type_id(1)
    edit_dict = {instance.edit_type_id: instance.id for instance in res}
    item = EditTypeCreate(
        edit_type_id=42, gas_volume_calc_type_id=4, edit_name="Unknown"
    )
    edit_type_dao.create_item(item)
    pass
