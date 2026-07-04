"""CRUD for SysType (sys-event reference codes). Built by the shared
type-router factory — see type_router_factory.py."""

from backend.api.endpoints.type_router_factory import make_type_router
from backend.db.dao.sys_type_dao import SysTypeDao
from backend.db.models.sys_type_model import (
    SysType,
    SysTypeCreate,
    SysTypeList,
    SysTypeUpdate,
)

sys_type_router = make_type_router(
    prefix="/sys-types",
    tag="sys_type",
    model=SysType,
    dao_cls=SysTypeDao,
    create_model_cls=SysTypeCreate,
    update_model_cls=SysTypeUpdate,
    list_model_cls=SysTypeList,
    type_id_field="sys_type_id",
    name_field="sys_name",
)
