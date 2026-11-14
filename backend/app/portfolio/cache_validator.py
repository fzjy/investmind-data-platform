from hashlib import sha256

from app.shared.contracts import DataMeta


def portfolio_etag(meta: DataMeta) -> str:
    # 发布版本绑定快照内容；鲜度和来源是响应的一部分，可能独立变化。
    # v1.1 对应剔除旧网站模板后的公共表示，字段合同变化须同时更新。
    material = "portfolio-v1.1:" + meta.model_dump_json(by_alias=True)
    return 'W/"' + sha256(material.encode()).hexdigest() + '"'


def matches_etag(condition: str, tag: str) -> bool:
    return any(item.strip() == "*" or item.strip().removeprefix("W/") == tag.removeprefix("W/")
               for item in condition.split(","))
