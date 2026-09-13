import pytest

from stackhelx import guardrails


def test_validate_identifier_valid():
    assert guardrails.validate_identifier("proj123") == "proj123"
    assert guardrails.validate_identifier("my-cool_project") == "my-cool_project"


def test_validate_identifier_invalid():
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_identifier("../evil")
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_identifier("proj/sub")
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_identifier("")
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_identifier("CON")
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_identifier("NUL")
    with pytest.raises(guardrails.GuardrailError):
        guardrails.validate_identifier("a" * 65)
