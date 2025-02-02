from datetime import datetime

from backend.db.dao.edit_type_dao import EditTypeDao
from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.line_dao import LineDao
from backend.db.models import EditArchiveCreate, EditTypeCreate
from backend.hl_engine.data_classes.edit_dataclass import EditStruct
from backend.hl_engine.hl_engine import Hostlib
from utils.files_utils import find_files_by_mask, read_archive_file


class EditEngine(Hostlib):

    def __init__(
        self, path: str = "./", chunk_size: int = 900, lumg_id: int = 1
    ) -> None:
        super().__init__(path, chunk_size)
        self.edit_mask = "S*R*U.*"
        self.edit_struct = EditStruct
        self.create_class = EditArchiveCreate
        self.lumg_id = lumg_id

    def read(self):
        files = find_files_by_mask(self.path, self.edit_mask)
        archive_dict_list = []
        gas_volume_dao = GasVolumeCalcDao()
        edit_type_dao = EditTypeDao()
        line_dao = LineDao()
        for file in files:
            flow_params = self.get_params_from_file_name(file)
            gas_volume_calc = (
                gas_volume_dao.get_flow_calc_by_address_and_lumg_or_create(
                    address=flow_params["address"], lumg_id=self.lumg_id
                )
            )
            gas_volume_calc_id = gas_volume_calc.id
            gas_volume_calc_type_id = gas_volume_calc.type_id

            gas_volume_line = line_dao.get_line_by_gas_id_and_line_or_create(
                gas_volume_calc_id, flow_params["line"]
            )

            line_id = gas_volume_line.id

            edit_list = edit_type_dao.get_by_gas_volume_type_id(gas_volume_calc_type_id)
            edit_dict = {instance.edit_type_id: instance.id for instance in edit_list}

            read_archive_gen = read_archive_file(file, self.edit_struct)
            while True:
                try:
                    file_dict = next(read_archive_gen)
                    datetime_period = datetime(
                        file_dict["year"] + 2000,
                        file_dict["month"],
                        file_dict["day"],
                        file_dict["hour"],
                        file_dict["minutes"],
                        file_dict["seconds"],
                    )

                    if file_dict.get("edit_type_id") is not None:
                        try:
                            file_dict["edit_id"] = edit_dict[file_dict["edit_type_id"]]
                        except KeyError:
                            new_edit = EditTypeCreate(
                                edit_type_id=file_dict["edit_type_id"],
                                gas_volume_calc_type_id=gas_volume_calc_type_id,
                                edit_name=f"Неизвестный код {file_dict['edit_type_id']}",
                            )
                            new_item = edit_type_dao.create_item(new_edit)
                            file_dict["edit_id"] = new_item.id
                            edit_dict[file_dict["edit_type_id"]] = new_item.id
                            self.logger.debug(
                                f"No edit type with this id: {file_dict['edit_type_id']}! Created new!"
                            )

                    file_dict["period"] = datetime_period
                    file_dict["line_id"] = line_id
                    archive_dict = {
                        key: value
                        for key, value in file_dict.items()
                        if key in self.create_class.model_fields
                    }
                    archive_dict_list.append(archive_dict)
                    if len(archive_dict_list) == self.chunk_size:
                        yield archive_dict_list
                        archive_dict_list = []

                except StopIteration:
                    break

                except ValueError as e:
                    self.logger.debug(e)

        if archive_dict_list:
            yield archive_dict_list
