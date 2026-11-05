from redis.asyncio import Redis


def make_redis(redis_url: str, timeout: float) -> Redis:
    return Redis.from_url(redis_url, socket_connect_timeout=timeout, socket_timeout=timeout)


async def ping(client: Redis) -> None:
    await client.ping()
