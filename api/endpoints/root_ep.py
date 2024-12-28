from fastapi import APIRouter, status

from backend.main import update_hostlibs


class RootRouter:
    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            path="/update_data/",
            endpoint=update_hostlibs,
            tags=["root"],
            methods=["POST"],
            status_code=status.HTTP_201_CREATED
        )

root_router = RootRouter().router
