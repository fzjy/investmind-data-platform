from app.local_admin.catalog_contracts import describe_operation


def test_nested_contract_and_deleted_route():
    spec = {"paths":{"/example":{"post":{"parameters":[{"name":"symbol","in":"query","required":True,"schema":{"type":"string"}}],
        "requestBody":{"required":True,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Body"}}}}}}},
        "components":{"schemas":{"Body":{"type":"object","required":["values"],"properties":{
            "values":{"type":"array","items":{"anyOf":[{"type":"number"},{"type":"null"}]}},
            "page":{"type":"integer","default":1,"minimum":1}}}}}}
    result = describe_operation(spec,"POST","/example")
    assert result["available"]
    assert result["parameters"][0]["required"]
    body=result["requestBody"]["schema"]
    assert body["required"]==["values"]
    assert body["properties"]["page"]["default"]==1
    assert body["properties"]["values"]["items"]["anyOf"][1]["type"]=="null"
    assert not describe_operation(spec,"GET","/gone")["available"]


def test_manual_search_query_contract_is_documented():
    from app.main import create_app
    result=describe_operation(create_app().openapi(),"GET","/api/v1/search")
    params={p["name"]:p for p in result["parameters"]}
    assert set(params)=={"q","page","pageSize"}
    assert params["q"]["schema"]["maxLength"]>0
    assert params["pageSize"]["schema"]["maximum"]>=params["pageSize"]["schema"]["default"]
