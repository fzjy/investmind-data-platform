"""已核对的文档目录；表结构与运行状态不参与静态内容加载。"""
from __future__ import annotations
from pathlib import Path
import re
from typing import Literal
from pydantic import BaseModel, Field, StrictBool
from app.local_admin.catalog_inventory import identities, read_json

class Judgment(BaseModel):
    value: StrictBool | None
    explanation: str
    evidence: list[str] = Field(default_factory=list)

class TableRef(BaseModel):
    schema_: str = Field(alias="schema")
    table: str
    name: str
    fieldPath: str = ""

class CatalogField(BaseModel):
    id: str
    name: str
    role: Literal["source","storage","response","input"]
    type: str = "待核验"
    unit: str = "待核验"
    nullable: str = "待核验"
    description: str
    sourceRefs: list[str] = Field(default_factory=list)
    tableRefs: list[TableRef] = Field(default_factory=list)
    derived: Judgment

class CatalogEntry(BaseModel):
    id: str
    name: str
    category: str
    description: str
    status: str
    scope: str
    sources: list[dict]
    frequencies: list[dict]
    tables: list[TableRef]
    history: dict[str, Judgment]
    derived: Judgment
    fields: list[CatalogField]
    calculations: list[dict]
    access: list[dict]
    evidence: list[str]

class Catalog(BaseModel):
    version: str
    categories: list[dict]
    entries: list[CatalogEntry]
    ownership: dict[str,list[str]]

def load_catalog(project_root: Path) -> Catalog:
    docs = (project_root/"docs").resolve()
    index = read_json(docs/"data-catalog.json")
    entries = []
    for file in index["files"]:
        target = (docs/file).resolve()
        if not target.is_relative_to(docs/"data-catalog") or target.suffix!=".json":
            raise ValueError("目录文件路径无效")
        entries.extend(read_json(target))
    return Catalog(version=index["version"], categories=index["categories"],
                   ownership=index["ownership"], entries=entries)

def validate_catalog(catalog: Catalog, *, project_root: Path, openapi: dict) -> list[dict]:
    issues = []
    def issue(entry,path,code,message):
        issues.append(dict(entryId=entry,path=path,code=code,message=message))
    ids = {e.id for e in catalog.entries}
    if len(ids)!=len(catalog.entries):
        issue("", "entries","DUPLICATE_ID","主说明标识重复")
    required = identities(project_root)
    required.update(f"operation:{m.upper()} {p}" for p,v in openapi.get("paths",{}).items()
                    if p.startswith("/api/v1/") for m in v if m in {"get","post","put","delete","patch"})
    for identity in sorted(required):
        owners = catalog.ownership.get(identity, [])
        if not owners or any(o not in ids for o in owners):
            issue("",identity,"MISSING_OWNER","来源或公式缺少有效数据条目归属")
    categories = {c["id"] for c in catalog.categories}
    from app.db.base import ALL_BASES
    from app.db import models
    definitions={t.fullname:t for base in ALL_BASES for t in base.metadata.tables.values()}
    from app.local_admin.catalog_contracts import describe_operation
    for entry in catalog.entries:
        if entry.category not in categories:
            issue(entry.id,"category","CATEGORY_MISSING","分类不存在")
        if set(entry.history)!={"records","revisions","query"}:
            issue(entry.id,"history","HISTORY_MISSING","历史三个判断项不完整")
        if not entry.sources:
            issue(entry.id,"sources","SOURCE_MISSING","缺少来源说明；尚无供给时须明确写出缺口")
        if not any(t.schema_ in {"core","raw"} for t in entry.tables):
            issue(entry.id,"tables","INPUT_TABLE_MISSING","仅列出结果表，缺少来源原件或核心输入表")
        if entry.derived.value is True and not entry.calculations:
            issue(entry.id,"calculations","CALCULATION_MISSING","已标为派生内容，但缺少计算口径")
        if entry.derived.value is not True and any(f.derived.value is True for f in entry.fields):
            issue(entry.id,"derived","DERIVATION_CONFLICT","字段含派生结果，但条目派生判断不一致")
        for n,f in enumerate(entry.fields):
            if not re.search(r"[\u4e00-\u9fff]", f.name):
                issue(entry.id,f"fields.{n}.name","LABEL_MISSING","缺少中文字段含义")
        refs=[*entry.tables,*(t for f in entry.fields for t in f.tableRefs)]
        for ref in refs:
            name=ref.schema_+"."+ref.table
            target=definitions.get(name)
            if target is None:
                issue(entry.id,"tables","TABLE_MISSING",name+" 未登记于存储模型")
            elif ref.fieldPath and ref.fieldPath.split(".")[0] not in target.c:
                issue(entry.id,"tables","TABLE_FIELD_MISSING",name+"."+ref.fieldPath+" 的根列不存在")
        for n,c in enumerate(entry.calculations):
            for f in c.get("inputs",[]):
                if not f.get("name") or not re.search(r"[\u4e00-\u9fff]",f["name"]):
                    issue(entry.id,f"calculations.{n}","LABEL_MISSING","公式输入缺少中文含义")
        for evidence in entry.evidence:
            if not (project_root/evidence.split("#")[0]).is_file():
                issue(entry.id,"evidence","EVIDENCE_MISSING",evidence)
        for access in entry.access:
            if access.get("path") and not describe_operation(openapi,access["method"],access["path"])["available"]:
                issue(entry.id,"access","ROUTE_MISSING",access["path"])
    return issues
