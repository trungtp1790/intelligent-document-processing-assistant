from types import SimpleNamespace

from app.services.extraction import _provider_usage_summary


def test_provider_usage_summary_totals_calls_and_available_token_counts() -> None:
    calls = [
        SimpleNamespace(input_tokens=71, output_tokens=18),
        SimpleNamespace(input_tokens=33, output_tokens=9),
        SimpleNamespace(input_tokens=None, output_tokens=None),
    ]

    assert _provider_usage_summary(calls) == {
        "model_calls": 3,
        "input_tokens": 104,
        "output_tokens": 27,
    }


def test_provider_usage_summary_keeps_unknown_tokens_null() -> None:
    calls = [SimpleNamespace(input_tokens=None, output_tokens=None)]

    assert _provider_usage_summary(calls) == {
        "model_calls": 1,
        "input_tokens": None,
        "output_tokens": None,
    }
