"""新闻标准记录直接写入既有Raw/Core；无JSONL中转文件。"""
from datetime import datetime, timezone
import hashlib
import json
from sqlalchemy import select
from app.connectors.external_files.contracts import ContentImportRecord
from app.content.content_policy import permitted_text
from app.db.models import ContentDocument, ExternalFile, ExternalRecord, Security


def ingest_news(factory, pending):
    with factory.begin() as session:
        known = set(session.scalars(select(Security.ts_code)))
        records = []
        for entry in pending:
            item = entry['record']
            value = {k: v for k, v in item.items() if k not in ('contentHash', 'fetchedAt')}
            value.update(recordType='content', contentType='news', ingestionRevision=f"{item['fetchedAt']}:{entry['queueId']}")
            value['securities'] = [symbol for symbol in value['securities'] if symbol in known]
            record = ContentImportRecord.model_validate(value)
            clean = permitted_text(record.copyright_status, record.summary, record.body)
            record.summary, record.body = clean['summary'], clean['body']
            records.append(record)
        payloads = [row.model_dump(mode='json', by_alias=True) for row in records]
        material = json.dumps(payloads, sort_keys=True, ensure_ascii=False).encode()
        digest = hashlib.sha256(material).hexdigest()
        existing = session.scalar(select(ExternalFile).where(ExternalFile.file_hash == digest))
        if existing: return existing.valid_count
        batch = ExternalFile(path_name=f'news-batch:{digest}', file_hash=digest, schema_version='1.0',
            byte_size=len(material), row_count=len(records), valid_count=len(records), rejected_count=0,
            status='imported', error_summary=[], imported_at=datetime.now(timezone.utc))
        session.add(batch); session.flush()
        for index, record in enumerate(records, 1):
            session.add(ExternalRecord(external_file_id=batch.id, row_number=index, record_type='content', payload=payloads[index-1]))
            key = f'{record.source}:{record.external_id}'
            row = session.scalar(select(ContentDocument).where(ContentDocument.dedupe_key == key))
            values = dict(source=record.source, external_id=record.external_id, dedupe_key=key, content_type='news',
                title=record.title, summary=record.summary, body=record.body, published_at=record.published_at,
                source_url=str(record.source_url), securities=record.securities, copyright_status=record.copyright_status)
            if row is None: session.add(ContentDocument(**values))
            else:
                for field, value in values.items(): setattr(row, field, value)
        return len(records)
