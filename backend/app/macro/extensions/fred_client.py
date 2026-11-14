"""仅FRED VIXCLS；不翻页，query凭据使用既有ContextVar日志脱敏。"""
import httpx

from app.connectors.macro.transport import _install_credential_log_filter
from app.macro.extensions.client import ExtensionClient, decode_json
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.normalize import normalize_extension_response

FRED_URL = 'https://api.stlouisfed.org/fred/series/observations'


class FredExtensionClient(ExtensionClient):
    source = 'fred'

    async def _request(self, plan):
        params = {**plan['query']['params'], 'api_key': self._token}
        log_filter = _install_credential_log_filter()
        url = httpx.Request('GET', FRED_URL, params=params).url
        with log_filter.request_context(url):
            body = await self._read(plan, 'GET', FRED_URL, params=params)
        try:
            value = decode_json(body)
            if type(value) is not dict:
                raise ValueError()
            for key in ('count', 'offset', 'limit', 'output_type'):
                if type(value.get(key)) is not int:
                    raise ValueError()
            for key in ('realtime_start', 'realtime_end', 'observation_start', 'observation_end',
                'units', 'output_type', 'order_by', 'sort_order', 'offset', 'limit'):
                if value[key] != plan['query']['params'][key]:
                    raise ValueError()
            if value.get('file_type', 'json') != 'json':
                raise ValueError()
            observations = value['observations']
            if (type(observations) is not list or value['count'] != len(observations)
                or not 0 <= value['count'] <= plan['limits']['maxResponseRows']):
                raise ValueError()
            fields = plan['query']['fields']
            if any(type(row) is not dict or set(row) != set(fields) for row in observations):
                raise ValueError()
            result = {'fields': list(fields), 'items': [[row[field] for field in fields] for row in observations]}
            normalize_extension_response(plan, result)
            return result
        except ExtensionError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ExtensionError('EXTENSION_RESPONSE_INVALID') from None
