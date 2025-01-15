from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import GasVolumeCalc, GasVolumeCalcCreate


class GasVolumeCalcDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = GasVolumeCalc

    def get_flow_calc_by_address_and_line_or_create(self, address: int, line: int):
        session_db = self.get_session()
        statement = select(self.model).where(
            (self.model.address == address) & (self.model.line == line)
        )
        with session_db as session:
            result = session.exec(statement).first()

        if not result:
            result = self.create_default_flow_calc(address, line)
            self.logger.debug(
                f"No gas volume calc with this address: {address} line: {line}! Created new!"
            )

        return result

    def create_default_flow_calc(self, address: int, line: int):
        gvc = GasVolumeCalcCreate(
            address=address,
            line=line,
            meter=False,
            name=f"a{address}_l{line}",
            c_time=7,
            lumg_id=1,
            type_id=4,
        )
        gas_volume_calc = self.create_item(gvc)
        return gas_volume_calc
