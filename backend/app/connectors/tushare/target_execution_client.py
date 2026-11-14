"""成分专用单次有界流；不复用带重试的通用客户端。"""
import json
import httpx

from app.connectors.tushare.client import DEFAULT_ENDPOINT
from app.connectors.tushare.contracts import TushareResponse
from app.macro.target_evidence_normalize import validated_policy,validate_outer


class TargetExecutionError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


def _reject_json(_value):
    raise TargetExecutionError('TARGET_RESPONSE_INVALID')


def _unique(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise TargetExecutionError('TARGET_RESPONSE_INVALID')
        value[key]=item
    return value


class TargetExecutionClient:
    def __init__(self,token,policy,*,transport=None):
        self.policy=validated_policy(policy)
        if not isinstance(token,str) or not token:raise TargetExecutionError('TARGET_AUTH_ERROR')
        self._token=token
        self._client=httpx.Client(transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
            timeout=self.policy.request_timeout_seconds,follow_redirects=False,trust_env=False)

    def __enter__(self):return self
    def __exit__(self,*_):self._client.close()

    def call(self,request,before_send):
        if not callable(before_send):raise TargetExecutionError('TARGET_EXECUTION_INVALID')
        payload={'api_name':request.api_name,'token':self._token,'params':dict(request.params),'fields':','.join(request.fields)}
        # 回调异常直接传播；尚未进入stream，不得为未预留请求发送。
        before_send()
        try:
            with self._client.stream('POST',DEFAULT_ENDPOINT,json=payload,headers={'Accept-Encoding':'identity'}) as response:
                status=response.status_code
                if 300<=status<400:raise TargetExecutionError('TARGET_REDIRECT_REFUSED')
                if status in (401,403):raise TargetExecutionError('TARGET_AUTH_ERROR')
                if status==429:raise TargetExecutionError('TARGET_RATE_LIMITED')
                if status>=500:raise TargetExecutionError('TARGET_SERVER_ERROR')
                if status>=400:raise TargetExecutionError('TARGET_HTTP_ERROR')
                if response.headers.get('content-encoding','identity').strip().lower() not in ('','identity'):
                    raise TargetExecutionError('TARGET_ENCODING_UNSUPPORTED')
                length=response.headers.get('content-length')
                if length is not None:
                    if not length.isdecimal():raise TargetExecutionError('TARGET_RESPONSE_INVALID')
                    if len(length)>20 or int(length)>self.policy.max_response_bytes:raise TargetExecutionError('TARGET_RESPONSE_TOO_LARGE')
                body=bytearray()
                for chunk in response.iter_raw(chunk_size=min(8192,self.policy.max_response_bytes+1)):
                    if len(body)+len(chunk)>self.policy.max_response_bytes:raise TargetExecutionError('TARGET_RESPONSE_TOO_LARGE')
                    body.extend(chunk)
            return self._decode(bytes(body),request)
        except httpx.RequestError:
            raise TargetExecutionError('TARGET_NETWORK_ERROR') from None

    def _decode(self,body,request):
        try:
            value=json.loads(body.decode('utf-8'),parse_constant=_reject_json,object_pairs_hook=_unique)
            if not isinstance(value,dict) or type(value.get('code')) is not int:
                raise TargetExecutionError('TARGET_RESPONSE_INVALID')
            if value['code']!=0:
                message=value.get('msg','')
                message=message.replace(self._token,'').lower() if isinstance(message,str) else ''
                if any(marker in message for marker in ('token','令牌')):raise TargetExecutionError('TARGET_AUTH_ERROR')
                if any(marker in message for marker in ('权限','积分','permission')):raise TargetExecutionError('TARGET_PERMISSION_ERROR')
                if any(marker in message for marker in ('限流','频率','每分钟','每小时','访问次数','rate limit','too many')):
                    raise TargetExecutionError('TARGET_RATE_LIMITED')
                raise TargetExecutionError('TARGET_RESPONSE_INVALID')
            data=value['data']
            if (not isinstance(data,dict) or not isinstance(data.get('fields'),list)
                or not isinstance(data.get('items'),list) or len(data['items'])>self.policy.max_response_rows
                or not all(isinstance(row,list) for row in data['items'])):
                raise TargetExecutionError('TARGET_RESPONSE_INVALID')
            response=TushareResponse.model_validate({'fields':tuple(data['fields']),'items':tuple(tuple(row) for row in data['items'])},strict=True)
            validate_outer(request,response,self.policy)
            return response
        except TargetExecutionError:raise
        except (ValueError,UnicodeError,KeyError,TypeError,OverflowError,RecursionError):
            raise TargetExecutionError('TARGET_RESPONSE_INVALID') from None
