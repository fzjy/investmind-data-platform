from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from app.connectors.macro.sanitization import sanitize_macro_text


def test_long_machine_error_code_redaction_finishes_in_bounded_subprocess() -> None:
    # 取消嵌套前缀的线性匹配会使此真实调用超时，不使用墙钟微基准。
    backend = Path(__file__).resolve().parents[3]
    code = 'CHINAMONEY_USDCNY_PAGINATION_INVALID'
    result = subprocess.run([
        sys.executable, '-I', '-c',
        f'import sys; sys.path.insert(0, {str(backend)!r}); '
        'from app.connectors.macro.sanitization import sanitize_macro_text; '
        f'print(sanitize_macro_text({code!r}))',
    ], capture_output=True, text=True, timeout=3, check=False)
    assert result.returncode == 0 and result.stdout.strip() == code


@pytest.mark.parametrize('text,want', [
    ('token=secret', 'token=***'),
    ('privateAccessToken=secret', 'privateAccessToken=***'),
    ('DANG_FRED_API_KEY=secret', 'DANG_FRED_API_KEY=***'),
    ('foo=token=secret', 'foo=token=***'),
    ('{"privateAccessToken":"secret"}', '{"privateAccessToken":"***"}'),
    ('notatoken=visible', 'notatoken=visible'),
    ('https://site.test?foo=ok&api_key=secret', 'https://site.test?foo=ok&api_key=***'),
    ('Authorization: Bearer secret', 'Authorization: Bearer ***'),
])
def test_identifier_redaction_preserves_nested_assignments_and_key_boundaries(text, want) -> None:
    assert sanitize_macro_text(text) == want
