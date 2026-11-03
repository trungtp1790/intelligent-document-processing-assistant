from __future__ import annotations

from functools import lru_cache
from hashlib import sha256

from redis import Redis

from app.config import settings

LOGIN_LIMIT = 10
LOGIN_WINDOW_SECONDS = 300
_INCREMENT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


@lru_cache(maxsize=1)
def _redis_client() -> Redis:
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )


def _key(remote: str, email: str) -> str:
    identity = f"{remote.strip()}:{email.strip().casefold()}".encode()
    return f"idp:login-limit:v1:{sha256(identity).hexdigest()}"


def register_login_attempt(remote: str, email: str, *, client: Redis | None = None) -> int:
    counter = client or _redis_client()
    return int(counter.eval(_INCREMENT_SCRIPT, 1, _key(remote, email), LOGIN_WINDOW_SECONDS))


def clear_login_attempts(remote: str, email: str, *, client: Redis | None = None) -> None:
    counter = client or _redis_client()
    counter.delete(_key(remote, email))


def check_login_rate_limiter() -> None:
    _redis_client().ping()
