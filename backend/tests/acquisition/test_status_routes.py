from datetime import UTC, datetime
from pathlib import Path
import pytest
from app.acquisition.contracts import StatusQuery
from app.acquisition.service import StatusService


def test_unknown_evidence_does_not_hide_known_stock(factory):
    service = StatusService(factory,root=Path(__file__).resolve().parents[3],clock=lambda:datetime.now(UTC))
    query = StatusQuery(('stock-daily','stock-dividend'),subject_keys=('600519.SH',),limit=1)
    page = service.query(query)
    assert len(page.items) == 1 and page.next_cursor
    assert page.items[0].assessment.status == 'missing'
    from dataclasses import replace
    assert service.query(replace(query,cursor=page.next_cursor)).items[0].key.entry_id == 'stock-dividend'
    with pytest.raises(ValueError):
        service.query(replace(query,cursor=page.next_cursor,subject_keys=('000001.SZ',)))


def test_actual_http_contract_and_invalid_inputs(factory):
    import asyncio
    import httpx
    from fastapi import FastAPI
    from app.acquisition.routes import create_router
    app = FastAPI()
    app.include_router(create_router(StatusService(factory)))
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
            path = '/api/v1/data-status'
            response = await client.get(path,params={'entryId':'stock-daily','subjectKey':'600519.SH'})
            assert response.status_code==200 and response.headers['cache-control']=='no-store'
            assert response.json()['items'][0]['audit']['status']=='missing'
            for params,code in [({'entryId':'unknown'},404),({'entryId':'stock-daily','limit':101},422),
                                ({'entryId':'stock-daily','subjectKey':'abc'},422),
                                ({'entryId':'stock-daily','product':'bogus'},422)]:
                assert (await client.get(path,params=params)).status_code==code
    asyncio.run(check())
