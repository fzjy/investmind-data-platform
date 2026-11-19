"""静态边界只检查真实源文件；不导入业务应用，不请求供应商。"""
import ast
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'backend/app'
DOMAINS = {'market', 'macro', 'stocks', 'portfolio', 'search', 'content'}


def domain_violations(source: str, owner: str) -> list[str]:
    errors = []
    for node in ast.walk(ast.parse(source)):
        targets = []
        if isinstance(node, ast.ImportFrom) and node.module:
            targets = [(node.module, [n.name for n in node.names])]
        elif isinstance(node, ast.Import):
            targets = [(n.name, []) for n in node.names]
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ''
            if name in {'import_module', '__import__'}:
                targets = [(node.args[0].value, [])]
        for target, names in targets:
            parts = target.split('.')
            if len(parts) > 1 and parts[0] == 'app' and parts[1] in DOMAINS and parts[1] != owner:
                if len(parts) < 3 or parts[2] != 'api' or any(n.startswith('_') or n == '*' for n in names):
                    errors.append(f'{node.lineno}: {target}')
    return errors


def test_cross_domain_calls_use_public_capabilities():
    failures = []
    for path in APP.rglob('*.py'):
        owner = path.relative_to(APP).parts[0]
        if owner in DOMAINS:
            failures.extend(f'{path.relative_to(APP)}:{v}' for v in domain_violations(path.read_text(encoding='utf-8'), owner))
    assert not failures, '\n'.join(failures)


def test_public_datasets_load_without_investmind_builders_or_publishers():
    script = """
import sys
from fastapi import FastAPI
from app.content import datasets_routes as content
from app.macro import datasets_routes as macro
from app.market import datasets_routes as market
from app.stocks import datasets_routes as stocks
app = FastAPI()
for domain in (content, macro, market, stocks):
    app.include_router(domain.create_router(None))
assert '/api/v1/datasets/stock-dividends' in app.openapi()['paths']
loaded = [m for m in sys.modules if m.startswith('app.') and
          ('.marts.' in m or m.endswith(('.database_provider', '.publication')))]
assert not loaded, loaded
"""
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT / 'backend', capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_boundary_catches_private_and_dynamic_imports():
    assert domain_violations('from app.stocks.financial_supply import load_points', 'macro')
    assert domain_violations('import app.stocks.financial_supply as supply', 'macro')
    assert domain_violations('importlib.import_module("app.stocks.financial_supply")', 'macro')
    assert domain_violations('from app.stocks.api.financial import _internal', 'macro')
    assert not domain_violations('from app.stocks.api.financial import CoreSnapshot', 'macro')
    assert not domain_violations('from app.stocks.financial_supply import load_points', 'stocks')


def test_lock_and_fingerprint_imports_do_not_load_business_builders():
    script = '''
import sys
from app.jobs.locks import ProcessLocalJobLock
from app.shared.fingerprints import canonical_source_hash
assert not any(m.startswith(('app.stocks.', 'app.market.', 'app.macro.', 'app.portfolio.')) for m in sys.modules)
from datetime import datetime, timezone
assert canonical_source_hash(datetime(2026,1,1,tzinfo=timezone.utc), 'a', 'b')
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT / 'backend', capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_market_coverage_rules_do_not_load_publication_or_runtime():
    script = '''
import sys
from app.market.etf.coverage import etf_coverage_regressed
from app.market.commodities.coverage import commodity_coverage_regressed
assert not etf_coverage_regressed({}, {})
assert not commodity_coverage_regressed({}, {})
assert 'app.marts.publication' not in sys.modules
assert not any(m.endswith('.runtime') for m in sys.modules if m.startswith('app.'))
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT / 'backend', capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_no_eager_python_module_cycles():
    files = {'.'.join(p.relative_to(ROOT / 'backend').with_suffix('').parts).removesuffix('.__init__'): p for p in APP.rglob('*.py')}
    graph = {}
    for name, path in files.items():
        edges = set()
        # 仅直接顶层导入。函数内延迟调用与TYPE_CHECKING不是初始化依赖。
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module in files:
                    edges.add(node.module)
                edges.update(node.module + '.' + item.name for item in node.names if node.module + '.' + item.name in files)
            elif isinstance(node, ast.Import):
                edges.update(item.name for item in node.names if item.name in files)
        graph[name] = edges - {name}
    done, stack = set(), []

    def visit(name):
        assert name not in stack, ' -> '.join([*stack, name])
        if name in done:
            return
        stack.append(name)
        for target in graph[name]:
            visit(target)
        stack.pop()
        done.add(name)

    for name in graph:
        visit(name)


def test_manual_research_and_test_helpers_are_outside_runtime():
    assert not (APP / 'portfolio/dividend_resolution.py').exists()
    for path in APP.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            values = []
            if isinstance(node, ast.ImportFrom) and node.module:
                values.append(node.module)
            elif isinstance(node, ast.Import):
                values.extend(a.name for a in node.names)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                values.append(node.value.replace('/', '.').replace('\\', '.'))
            assert not any(v.startswith(('backend.tests.', 'tests.support.', 'tests.fixtures.')) for v in values), str(path)
