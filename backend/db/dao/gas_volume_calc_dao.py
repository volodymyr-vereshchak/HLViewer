from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import GasVolumeCalc, GasVolumeCalcCreate
from backend.db.models.gas_volume_calc_model import GasVolumeCalcUpdate


class GasVolumeCalcDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = GasVolumeCalc

    def get(self, address: int, lumg_id: int):
        session_db = self.get_session()
        statement = select(self.model).where(
            (self.model.address == address) & (self.model.lumg_id == lumg_id)
        )
        with session_db as session:
            result = session.exec(statement).first()
        return result

    def update_if_exists(
        self,
        address: int,
        lumg_id: int,
        type_id: int = 4,
        c_time: int = 7,
        name: str = None,
    ):
        result = self.get(address=address, lumg_id=lumg_id)
        if result:
            gvc = GasVolumeCalcUpdate(
                address=address,
                name=name,
                c_time=c_time,
                lumg_id=lumg_id,
                type_id=type_id,
            )
            result = self.update_by_id(result.id, gvc)
            self.logger.debug(
                f"Gas volume calc with this address: {address} was updated!"
            )
        return result

    def get_or_create(
        self,
        address: int,
        lumg_id: int,
        type_id: int = 4,
        c_time: int = 7,
        name: str = None,
    ):
        result = self.get(address=address, lumg_id=lumg_id)

        if not result:
            if not name:
                name = f"a{address}"
            gvc = GasVolumeCalcCreate(
                address=address,
                name=name,
                c_time=c_time,
                lumg_id=lumg_id,
                type_id=type_id,
            )
            result = self.create_flow_calc(gvc)
            self.logger.debug(
                f"No gas volume calc with this address: {address} Created new!"
            )

        return result

    def create_flow_calc(self, gvc: GasVolumeCalcCreate):
        gas_volume_calc = self.create_item(gvc)
        return gas_volume_calc

    def get_flow_by_lumg_id(self, lumg_id: int = None):
        statement = select(self.model)
        if lumg_id:
            statement = statement.where(self.model.lumg_id == lumg_id)

        with self.get_session() as session:
            return session.exec(statement).all()
