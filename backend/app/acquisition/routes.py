from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.acquisition.contracts import AuditContext, StatusQuery, QueryError, UnknownResource


def envelope(page):
    items = []
    for row in page.items:
        k, e = row.key, row.evidence
        items.append({'key':{'entryId':k.entry_id,'itemId':k.item_id,'subjectKind':k.subject_kind,
                             'subjectKey':k.subject_key,'scopeKind':k.scope_kind},
                      'name':row.name,'context':asdict(row.context),
                      'collection':{'status':e.status,'checkedAt':e.checked_at,'successAt':e.success_at,
                                    'errorCode':e.error_code,'reason':e.reason,'scope':e.scope,'references':e.references},
                      'availability':asdict(row.availability),'audit':asdict(row.assessment)})
    return {'schemaVersion':page.schema_version,'readAt':page.read_at,'items':items,'nextCursor':page.next_cursor}


def create_router(service, *, path='/api/v1/data-status'):
    router = APIRouter()
    @router.get(path, summary='按对象和数据条目读取获取状态')
    def status(entryId: Annotated[list[str] | None, Query()] = None,
               itemId: Annotated[list[str] | None, Query()] = None,
               subjectKey: Annotated[list[str] | None, Query()] = None, subjectKind: str | None = None,
               scopeKind: str = 'routine', view: str = 'stored', product: str | None = None,
               version: str | None = None, period: str | None = None, limit: str = '25', cursor: str | None = None):
        unique = lambda values: tuple(dict.fromkeys(values or ()))
        try:
            if max(map(len,(entryId or (),itemId or (),subjectKey or ()))) > 100:
                raise QueryError('筛选条件过多')
            if not limit.isascii() or not limit.isdigit() or len(limit)>3:
                raise QueryError('分页大小必须为 1 至 100 的整数')
            query = StatusQuery(unique(entryId),unique(itemId),subjectKind,unique(subjectKey),scopeKind,
                                AuditContext(view,product,version,period),int(limit),cursor)
            payload = envelope(service.query(query))
        except UnknownResource:
            raise HTTPException(404,'目录、产品或版本不存在') from None
        except QueryError as exc:
            raise HTTPException(422,str(exc)) from None
        except (SQLAlchemyError,ValueError,KeyError,TypeError,OSError):
            raise HTTPException(503,'状态数据库暂不可用，请检查连接及数据库迁移') from None
        return JSONResponse(jsonable_encoder(payload),headers={'Cache-Control':'no-store'})
    return router
