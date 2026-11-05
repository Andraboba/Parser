"""Централизованный лимит запросов: все контейнеры выходят в сеть через один IP (раздел 5 плана)."""
from collections.abc import Mapping

DECREASE_FACTOR = 0.5
RECOVERY_STEP = 0.05
FLOOR_FRACTION = 0.05


def next_limit(current: float, base: float, throttled: int) -> float:
    """Аддитивный рост и мультипликативное снижение: при 429 и 5xx лимит падает вдвое,
    без них за каждый проход возвращается 5 % базового значения (ТЗ, п. 9)."""
    floor = base * FLOOR_FRACTION
    if throttled > 0:
        return max(floor, min(current, base) * DECREASE_FACTOR)
    return min(base, max(floor, current + base * RECOVERY_STEP))


def fair_shares(
    total: float, weights: Mapping[int, float], caps: Mapping[int, float | None],
) -> dict[int, float]:
    """Делит total пропорционально весам. Узел с личным лимитом не забирает больше него,
    остаток уходит другим. Сумма долей никогда не превышает total."""
    shares = dict.fromkeys(weights, 0.0)
    remaining = total
    active = {k: w for k, w in weights.items() if w > 0}
    while active and remaining > 1e-9:
        weight_sum = sum(active.values())
        capped = {
            k for k, w in active.items()
            if (cap := caps.get(k)) is not None and shares[k] + remaining * w / weight_sum >= cap
        }
        if not capped:
            for k, w in active.items():
                shares[k] += remaining * w / weight_sum
            remaining = 0.0
            break
        for k in capped:
            cap = caps[k]
            assert cap is not None
            remaining -= max(0.0, cap - shares[k])
            shares[k] = cap
            del active[k]
    return shares
