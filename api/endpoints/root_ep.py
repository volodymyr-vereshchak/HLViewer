from fastapi import APIRouter, status

from backend.main import update_hostlibs


class RootRouter:
    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            "/update_data/",
            update_hostlibs,
            methods=["POST"],
            status_code=status.HTTP_200_OK
        )

root_router = RootRouter().router
