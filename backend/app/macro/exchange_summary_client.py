"""安全业务原件 RPC；无默认重试、分页、供应商 msg 或请求密钥持久化。"""
from app.macro.exchange_summary_plan import validate_exchange_plan
from app.macro.exchange_summary_store import safe_business
from app.macro.extensions.client import ExtensionClient,decode_json
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.plan import canonical


class ExchangeSummaryClient:
    def __init__(self,token,*,transport=None):
        self._token=token
        self._client=ExtensionClient(token,transport=transport)

    async def fetch(self,plan,doc):
        validate_exchange_plan(plan)
        if doc not in plan['documents'] or type(self._token) is not str or not self._token:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        query=doc['query']
        body=await self._client._read(plan,'POST',plan['groupPolicy']['providerUrl'],payload={
            'api_name':query['api'],'token':self._token,'params':query['params'],'fields':','.join(query['fields'])})
        try:
            value=decode_json(body)
            if type(value) is not dict or type(value.get('code')) is not int:
                raise ValueError()
            if value['code']!=0:
                raise ExtensionError('EXTENSION_PERMISSION_ERROR' if value['code']==2002 else 'EXTENSION_PROVIDER_REJECTED')
            # 检查解码后的结构，防止 Unicode 转义躲过密钥回显检测。
            if self._token in canonical(value):raise ValueError()
            data=value['data']
            if type(data) is not dict or 'has_more' in data and data['has_more'] is not False:raise ValueError()
            return safe_business({'fields':data['fields'],'items':data['items']},plan['limits']['maxResponseBytes'])
        except ExtensionError:raise
        except (ValueError,TypeError,KeyError,UnicodeError,OverflowError,RecursionError):
            raise ExtensionError('EXTENSION_RESPONSE_INVALID') from None
