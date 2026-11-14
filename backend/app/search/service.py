from sqlalchemy.exc import SQLAlchemyError

from app.search.matching import match_pages, match_stocks, normalize_query
from app.search.models import SearchData, SearchError, SearchMeta, SearchOptions, SearchPages, SearchRequest, SearchResponse, SearchStocks
from app.shared.errors import DataServiceError
from app.stocks.api.quotes import StocksProvider


class SearchService:
    def __init__(self, provider: StocksProvider, options: SearchOptions) -> None:
        self._provider = provider
        self._options = options

    def query(self, request: SearchRequest, *, request_id: str) -> SearchResponse:
        query = normalize_query(request.q, self._options)
        page_size = request.page_size if request.page_size is not None else self._options.default_page_size
        if page_size > self._options.max_page_size:
            raise DataServiceError("INVALID_REQUEST", "搜索页大小超出上限")
        page_items = match_pages(query)
        if not query:
            stocks = SearchStocks(status="idle", page=request.page, pageSize=page_size)
        else:
            try:
                snapshot = self._provider.read_screener_universe()
                items = match_stocks(snapshot.raw, query)
            except SQLAlchemyError:
                stocks = SearchStocks(status="unavailable", page=request.page, pageSize=page_size,
                    error=SearchError(code="DATA_SOURCE_UNAVAILABLE", message="股票数据暂不可用"))
            except DataServiceError as error:
                if error.code not in {"DATA_NOT_READY", "DATA_NOT_FOUND", "INVALID_SOURCE_PAYLOAD",
                                      "DATA_SOURCE_UNAVAILABLE", "DATA_SOURCE_TIMEOUT"}:
                    raise
                stocks = SearchStocks(status="unavailable", page=request.page, pageSize=page_size,
                    error=SearchError(code=error.code, message="股票搜索数据暂不可用"))
            else:
                start = (request.page - 1) * page_size
                stocks = SearchStocks(status="ready", page=request.page, pageSize=page_size,
                    items=items[start:start + page_size], total=len(items), meta=snapshot.meta)
        return SearchResponse(data=SearchData(query=query, stocks=stocks,
            pages=SearchPages(items=page_items, total=len(page_items))), meta=SearchMeta(requestId=request_id))
