"""
Optional publishing of readings to a Redis list (Day 2 lab: producer -> broker -> consumer).

Publishing only happens when REDIS_HOST is set, which is the case in the local
docker-compose.yml and not on the VM. The VM deployment writes straight to RDS and S3,
because with one machine and one consumer per reading a broker adds a process to run and
monitor without decoupling anything (see ADR-002 and ADR-004).
"""

import json
import os

QUEUE = "readings"


def enabled() -> bool:
    return bool(os.environ.get("REDIS_HOST"))


def publish(messages: list[dict]) -> int:
    """RPUSH each message as JSON onto the `readings` list. Returns the new list length."""
    if not messages or not enabled():
        return 0
    import redis

    client = redis.Redis(host=os.environ["REDIS_HOST"], port=int(os.environ.get("REDIS_PORT", "6379")),
                         socket_timeout=5)
    return client.rpush(QUEUE, *(json.dumps(m, default=str) for m in messages))
