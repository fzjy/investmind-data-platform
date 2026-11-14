from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)


T = TypeVar("T")
DataSourceKind = Literal["fixture", "vendor", "database", "external"]


class DataMeta(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: DataSourceKind
    as_of: str = Field(alias="asOf")
    version: str
    stale: bool
    stale_reason: str | None = Field(default=None, alias="staleReason")

    @model_serializer(mode="wrap")
    def _omit_empty_optional_fields(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, object]:
        serialized = handler(self)
        if self.stale_reason is None:
            serialized.pop("stale_reason", None)
            serialized.pop("staleReason", None)
        return serialized


@dataclass(frozen=True)
class ProviderSnapshot(Generic[T]):
    raw: T
    meta: DataMeta


class DataResult(BaseModel, Generic[T]):
    data: T
    meta: DataMeta


def success_result(data: T, meta: DataMeta) -> DataResult[T]:
    return DataResult(data=data, meta=meta)
