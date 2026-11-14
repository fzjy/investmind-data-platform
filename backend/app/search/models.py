from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import Settings
from app.shared.contracts import DataMeta
from app.shared.errors import DataErrorCode


class SearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, hide_input_in_errors=True)


class SearchOptions(SearchModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    query_max_length: int = Field(default=64, ge=1, le=128, strict=True)
    default_page_size: int = Field(default=20, ge=1, le=200, strict=True)
    max_page_size: int = Field(default=50, ge=1, le=200, strict=True)

    @model_validator(mode="after")
    def consistent_page_sizes(self) -> Self:
        if self.default_page_size > self.max_page_size:
            raise ValueError("search default page size exceeds maximum")
        return self

    @classmethod
    def from_settings(cls, settings: Settings) -> SearchOptions:
        return cls(query_max_length=settings.search_query_max_length,
                   default_page_size=settings.search_default_page_size,
                   max_page_size=settings.search_max_page_size)


class SearchRequest(SearchModel):
    q: str = Field(default="", strict=True)
    page: int = Field(default=1, ge=1, strict=True)
    page_size: int | None = Field(default=None, ge=1, strict=True, alias="pageSize")


class SearchStockItem(SearchModel):
    symbol: str
    code: str
    name: str


class SearchError(SearchModel):
    code: DataErrorCode
    message: str


class SearchStocks(SearchModel):
    status: Literal["idle", "ready", "unavailable"]
    items: list[SearchStockItem] = Field(default_factory=list)
    total: int | None = Field(default=None, ge=0, strict=True)
    page: int = Field(ge=1, strict=True)
    page_size: int = Field(ge=1, alias="pageSize", strict=True)
    meta: DataMeta | None = None
    error: SearchError | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.status == "ready":
            valid = self.total is not None and self.meta is not None and self.error is None
            valid = valid and len(self.items) <= self.page_size and len(self.items) <= self.total
        else:
            valid = not self.items and self.total is None and self.meta is None
            valid = valid and ((self.error is None) == (self.status == "idle"))
        if not valid:
            raise ValueError("invalid search state")
        return self


class SearchData(SearchModel):
    query: str
    stocks: SearchStocks


class SearchMeta(SearchModel):
    request_id: str = Field(alias="requestId")


class SearchResponse(SearchModel):
    data: SearchData
    meta: SearchMeta
