from datetime import date

from api_client.base_client import BaseClient


class DailyArchiveClient(BaseClient):
    def __init__(
        self,
        from_date: date = None,
        to_date: date = None,
        gas_volume_calc_id: int = None,
    ):
        super().__init__()
        self.endpoint = "day_archive/"
        self.params = {
            "from_date": from_date,
            "to_date": to_date,
            "gas_volume_calc_id": gas_volume_calc_id,
        }
