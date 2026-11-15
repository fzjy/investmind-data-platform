"""有限人工核准的公司公告裁决：精确原件/记录/官方文件摘要绑定。"""
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlparse
from app.ingestion.repositories import content_hash

MANIFEST=Path(__file__).resolve().parents[3]/'docs/research/evidence/portfolio'/'portfolio-event-resolutions.json'

def resolve_announced_supersession(rows,source_hash,as_of,manifest=MANIFEST):
    rules=json.loads(manifest.read_text(encoding='utf-8'))
    result=list(rows);proofs={}
    fingerprint=lambda row:content_hash({k:v for k,v in row.items() if k!='sourceId'})
    for rule in rules:
        if rule['sourceHash']!=source_hash:continue
        old=[r for r in result if fingerprint(r)==rule['cancelHash']]
        new=[r for r in result if fingerprint(r)==rule['replacementHash']]
        if len(old)!=1 or len(new)!=1:continue
        cancelled,replacement=old[0],new[0]
        if rule['scope']!='proposal-only' or cancelled.get('div_proc') not in ('停止实施','取消实施') or replacement.get('div_proc') not in ('实施','实施完成') or cancelled.get('record_date') or cancelled.get('ex_date') or (cancelled.get('ts_code'),cancelled.get('end_date'))!=(replacement.get('ts_code'),replacement.get('end_date')):
            raise ValueError('官方裁决范围无效')
        docs=rule['documents']
        if not docs:raise ValueError('官方裁决原件缺失')
        if any(date.fromisoformat(d['publishedAt'])>as_of for d in docs):continue
        for doc in docs:
            path=(manifest.parent/doc['file']).resolve()
            url=urlparse(doc['url'])
            if url.scheme!='https' or url.hostname not in ('www.hkexnews.hk','www1.hkexnews.hk') or not path.is_relative_to(manifest.parent.resolve()) or sha256(path.read_bytes()).hexdigest()!=doc['sha256']:
                raise ValueError('官方裁决原件摘要不匹配')
        result=[r for r in result if r is not cancelled]
        proofs[replacement['sourceId']]=[{k:d[k] for k in ('url','sha256','publishedAt')} for d in docs]
    return result,proofs
