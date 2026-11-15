"""企业范围目标的严格目录；归档名称仅为提示，不证明供应商映射。"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from app.config_paths import CONFIG_ROOT
from app.macro.target_evidence_policy import TargetEvidencePolicy, load_target_evidence_policy
from app.macro.official_member_policy import OfficialMemberPolicy, load_official_policy
from app.macro.catalog_member_policy import CatalogMemberPolicy,load_catalog_member_policy


DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE = (
    CONFIG_ROOT / "macro/enterprise-targets.json"
)


@dataclass(frozen=True, slots=True)
class EnterpriseTarget:
    kind: str
    code: str
    label: str
    source_index_name_hint: str | None


@dataclass(frozen=True, slots=True)
class EnterpriseTargetCatalog:
    version: str
    max_targets: int
    targets: tuple[EnterpriseTarget, ...]
    fingerprint: str
    evidence_policy: TargetEvidencePolicy
    official_policy: OfficialMemberPolicy | None = None
    catalog_member_policy: CatalogMemberPolicy | None = None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('ENTERPRISE_TARGET_CATALOG_INVALID')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('ENTERPRISE_TARGET_CATALOG_INVALID')


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def load_enterprise_target_catalog(path: Path | None = None) -> EnterpriseTargetCatalog:
    """不接任意CLI路径，不读取密钥，不截断超限目标。"""
    source = DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE if path is None else path
    value = json.loads(source.read_text(encoding='utf-8'),
        object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    keys = {'schemaVersion', 'catalogVersion', 'mode', 'maxTargetsPerPreview', 'targets', 'evidencePolicy'}
    if not isinstance(value, dict):
        raise ValueError('ENTERPRISE_TARGET_CATALOG_INVALID')
    version = value.get('schemaVersion')
    if type(version) is not int or version != 2 or not keys<=set(value)<=keys | {'officialEvidence','catalogEvidence'}:
        raise ValueError('ENTERPRISE_TARGET_CATALOG_INVALID')
    limit = value['maxTargetsPerPreview']
    if (value['mode'] != 'tracking-index-proxy'
        or not _nonempty_string(value['catalogVersion'])
        or type(limit) is not int or limit <= 0
        or not isinstance(value['targets'], list) or not 1 <= len(value['targets']) <= limit):
        raise ValueError('ENTERPRISE_TARGET_CATALOG_INVALID')
    targets = []
    seen = set()
    for item in value['targets']:
        if not isinstance(item, dict) or set(item) != {'kind', 'code', 'label', 'sourceIndexNameHint'}:
            raise ValueError('ENTERPRISE_TARGET_CATALOG_INVALID')
        kind, code, hint = item['kind'], item['code'], item['sourceIndexNameHint']
        if (kind not in ('index', 'etf') or not isinstance(code, str)
            or re.fullmatch(r'[0-9]{6}\.(SH|SZ)', code) is None
            or not _nonempty_string(item['label'])
            or (kind == 'index' and hint is not None)
            or (kind == 'etf' and not _nonempty_string(hint)) or code in seen):
            raise ValueError('ENTERPRISE_TARGET_CATALOG_INVALID')
        seen.add(code)
        targets.append(EnterpriseTarget(kind, code, item['label'], hint))
    # 新官方策略独立冻结，不让既有失败根因配置增加命名空间而失效。
    legacy_value={key:value[key] for key in keys}
    fingerprint = sha256(json.dumps(legacy_value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    policy = load_target_evidence_policy(value['evidencePolicy'], tuple(t.code for t in targets))
    official=load_official_policy(value['officialEvidence'],tuple(targets)) if 'officialEvidence' in value else None
    completion=load_catalog_member_policy(value['catalogEvidence'],tuple(targets),official) if 'catalogEvidence' in value else None
    return EnterpriseTargetCatalog(value['catalogVersion'], limit, tuple(targets), fingerprint, policy,official,completion)
