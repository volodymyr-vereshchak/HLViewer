"""CRUD for EditType (edit-event reference codes). Built by the shared
type-router factory — see type_router_factory.py."""

from backend.api.endpoints.type_router_factory import make_type_router
from backend.db.dao.edit_type_dao import EditTypeDao
from backend.db.models.edit_type_model import (
    EditType,
    EditTypeCreate,
    EditTypeList,
    EditTypeUpdate,
)

edit_type_router = make_type_router(
    prefix="/edit-types",
    tag="edit_type",
    model=EditType,
    dao_cls=EditTypeDao,
    create_model_cls=EditTypeCreate,
    update_model_cls=EditTypeUpdate,
    list_model_cls=EditTypeList,
    type_id_field="edit_type_id",
    name_field="edit_name",
)
