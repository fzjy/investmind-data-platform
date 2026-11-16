import json
from functools import lru_cache
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from app.acquisition.contracts import Binding


@lru_cache(maxsize=4)
def load_bindings(root: Path) -> tuple[Binding,...]:
    path = root/'backend/app/acquisition/bindings.json'
    rows = json.loads((path if path.is_file() else Path(__file__).with_name('bindings.json')).read_text(encoding='utf-8'))
    return tuple(Binding(**{**row, 'dependencies': tuple(row['dependencies']),
                            'writers': tuple(row['writers'])}) for row in rows)


def validate_bindings(root: Path) -> list[dict]:
    entries = {row['id'] for path in (root/'docs/data-catalog').glob('*.json')
               for row in json.loads(path.read_text(encoding='utf-8'))}
    bindings = load_bindings(root)
    issues = [{'entryId': e, 'code':'ENTRY_MISMATCH'} for e in entries ^ {b.entry_id for b in bindings}]
    seen = set()
    graph = TopologicalSorter()
    for binding in bindings:
        key = binding.entry_id, binding.item_id
        if key in seen or not binding.writers and not binding.unavailable_reason:
            issues.append({'entryId': key[0], 'itemId':key[1], 'code':'INVALID_BINDING'})
        if binding.source_adapter not in {'stocks','markets','macro','members','derived','content','imports'}:
            issues.append({'entryId':key[0],'itemId':key[1],'code':'UNKNOWN_SOURCE_ADAPTER'})
        if binding.subject_kind not in {'series','security','product','instrument','industry','exchange','source'}:
            issues.append({'entryId':key[0],'itemId':key[1],'code':'UNKNOWN_SUBJECT_KIND'})
        seen.add(key)
        for dep in binding.dependencies:
            if dep not in entries:
                issues.append({'entryId':key[0], 'dependency':dep, 'code':'UNKNOWN_DEPENDENCY'})
        graph.add(key[0], *binding.dependencies)
    try:
        graph.prepare()
    except CycleError as error:
        issues.append({'code':'DEPENDENCY_CYCLE', 'entries':error.args[1]})
    return issues
