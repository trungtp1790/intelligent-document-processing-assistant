from __future__ import annotations

from app.login_rate_limit import (
    LOGIN_LIMIT,
    LOGIN_WINDOW_SECONDS,
    _key,
    clear_login_attempts,
    register_login_attempt,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def eval(self, _script: str, _key_count: int, key: str, ttl: int) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        self.ttls[key] = ttl
        return self.values[key]

    def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.ttls.pop(key, None)


def test_login_attempt_counter_is_shared_keyed_hashed_and_bounded() -> None:
    client = FakeRedis()
    key = _key("192.0.2.10", "Operator@Example.Local")

    assert "Operator" not in key and "Example" not in key
    assert _key("192.0.2.10", "operator@example.local") == key
    assert _key("192.0.2.11", "operator@example.local") != key
    attempts = [
        register_login_attempt("192.0.2.10", "operator@example.local", client=client)
        for _ in range(11)
    ]
    assert attempts == list(range(1, LOGIN_LIMIT + 2))
    assert client.ttls[key] == LOGIN_WINDOW_SECONDS

    clear_login_attempts("192.0.2.10", "OPERATOR@example.local", client=client)

    assert client.values.get(key, 0) == 0
