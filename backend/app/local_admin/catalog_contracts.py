"""仅解析当前应用的 OpenAPI，不调用业务接口。"""
from copy import deepcopy


def expand_schema(schema, spec, trail=()):
    ref = schema.get("$ref")
    if ref:
        if ref in trail:
            return {"$ref": ref, "description": "递归引用，展开到此层"}
        node = spec
        try:
            for key in ref.removeprefix("#/").split("/"):
                node = node[key.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            return {"$ref": ref, "description": "引用待更新"}
        return {**expand_schema(node, spec, (*trail, ref)), **{k:v for k,v in schema.items() if k!="$ref"}}
    return {key: [expand_schema(v, spec, trail) for v in value] if key in {"anyOf","oneOf","allOf"}
            else {k:expand_schema(v,spec,trail) for k,v in value.items()} if key=="properties"
            else expand_schema(value,spec,trail) if key in {"items","additionalProperties"} and isinstance(value,dict)
            else deepcopy(value) for key,value in schema.items()}


def describe_operation(openapi: dict, method: str, path: str) -> dict:
    item = openapi.get("paths", {}).get(path, {})
    operation = item.get(method.lower())
    if not operation:
        return {"available":False,"parameters":[],"requestBody":None,
                "issues":[{"code":"ROUTE_MISSING","message":f"目录待更新，当前没有 {method} {path}"}]}
    params = [expand_schema(p, openapi) for p in [*item.get("parameters",[]),*operation.get("parameters",[])]]
    if path=="/api/v1/search" and method.upper()=="GET" and not params:
        # 该路由通过 Request 手工解析；补充现有请求模型和当前选项的文档合同。
        from app.search.models import SearchRequest, SearchOptions
        from app.config import Settings
        options=SearchOptions.from_settings(Settings.from_env())
        properties=SearchRequest.model_json_schema(by_alias=True)["properties"]
        for key,schema in properties.items():
            schema=deepcopy(schema)
            if key=="q":
                schema.update(maxLength=options.query_max_length,description="证券代码或名称关键词；空字符串返回空闲状态")
            if key=="page":
                schema["maximum"]=9007199254740991
            if key=="pageSize":
                schema.update(default=options.default_page_size,maximum=options.max_page_size)
            params.append(dict(name=key,**{"in":"query"},required=False,schema=schema))
    body = operation.get("requestBody")
    if body:
        body = expand_schema(body, openapi)
        schema = body.get("content",{}).get("application/json",{}).get("schema",{})
        body = {"required":body.get("required",False),"schema":expand_schema(schema,openapi)}
    return {"available":True,"parameters":params,"requestBody":body,"issues":[]}
