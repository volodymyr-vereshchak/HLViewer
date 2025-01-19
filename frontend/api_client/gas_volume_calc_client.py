from api_client.base_client import BaseClient


class GasVolumeCalcClient(BaseClient):
    def __init__(self):
        super().__init__()
        self.endpoint = "gas_volume_calcs/"


if __name__ == "__main__":
    status, gas_volume_calcs = GasVolumeCalcClient().api_request()
    pass
