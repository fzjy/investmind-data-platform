import pytest
from pydantic import ValidationError

from app.connectors.external_files.contracts import MacroImportRecord


def test_macro_requires_release_and_revision() -> None:
    with pytest.raises(ValidationError):
        MacroImportRecord.model_validate({"schemaVersion": "1.0", "seriesId": "CPI"})
