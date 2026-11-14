"""无供应商/数据库依赖的目标需求报告；库存不能升级为可信快照。"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from app.macro.enterprise_target_catalog import EnterpriseTargetCatalog
from app.stocks.api.universe import UniverseSelection


TARGET_EVIDENCE_APIS = (
    'index_weight', 'etf_basic', 'etf_index', 'etf_sh_cons', 'etf_sz_cons', 'fund_portfolio',
)


@dataclass(frozen=True, slots=True)
class TargetEvidenceInventory:
    legacy_constituent_rows: int
    raw_api_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        if (type(self.legacy_constituent_rows) is not int or self.legacy_constituent_rows < 0
            or not isinstance(self.raw_api_counts, Mapping)
            or set(self.raw_api_counts) != set(TARGET_EVIDENCE_APIS)
            or any(type(n) is not int or n < 0 for n in self.raw_api_counts.values())):
            raise ValueError('ENTERPRISE_TARGET_INVENTORY_INVALID')
        object.__setattr__(self, 'raw_api_counts', MappingProxyType(dict(self.raw_api_counts)))


def preview_enterprise_targets(
    selection: UniverseSelection,
    catalog: EnterpriseTargetCatalog,
    inventory: TargetEvidenceInventory,
    as_of: date,
) -> dict[str, object]:
    """报告来源依赖，不生成可执行HTTP参数、请求数或成分覆盖。"""
    return {
        'schemaVersion': 1,
        'asOf': as_of.isoformat(),
        'catalogHash': catalog.fingerprint,
        'catalogVersion': catalog.version,
        'pool': selection.to_payload(),
        'status': 'requirements_pending',
        'readOnly': True,
        'membershipReady': False,
        'accountPermissionProbed': False,
        'supplierHttp': 0,
        'notStrictPointInTime': True,
        'scope': 'configured-pool-intersection',
        'inventory': {
            'evidenceStatus': 'unverified_inventory_only',
            'legacyConstituentRows': inventory.legacy_constituent_rows,
            'rawApiCounts': dict(inventory.raw_api_counts),
        },
        'targets': [{
            'kind': target.kind,
            'code': target.code,
            'label': target.label,
            'sourceIndexNameHint': target.source_index_name_hint,
            'verifiedSourceIndexCode': None,
            'sourceConstituentCount': None,
            'intersectionCount': None,
            'status': 'missing_verified_membership',
            'sourceRequirements': ['index_basic', 'index_weight'] if target.kind == 'index' else ['etf_basic'],
            'requiresResolvedIndexMembership': target.kind == 'etf',
        } for target in catalog.targets],
    }
