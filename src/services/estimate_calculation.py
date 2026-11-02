"""Current-price estimate arithmetic; source amounts already include quantity and indices."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itertools import product


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value).replace('\xa0', '').replace(' ', '').replace(',', '.'))
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def money(value):
    return None if value is None else float(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def component(amount, quantity, current_rate, correction=1, winter=1, applied=None):
    """Apply missing factors once, only with a demonstrable source-cost reconciliation.

    Amount is a position total, never a unit price. All four possible prior
    applications of the two factors are reconciled against the unit-rate trace.
    Ambiguity or a mismatch preserves the amount and requires human review.
    """
    amount, quantity, rate = map(number, (amount, quantity, current_rate))
    correction, winter = map(number, (correction, winter))
    if any(v is None or v <= 0 for v in (correction, winter)):
        return {'value':money(amount), 'status':'invalid_factor', 'applied':[]}
    if amount is None:
        if (rate == 0 and quantity is not None) or (quantity == 0 and rate is not None):
            amount = Decimal(0)
        else:
            return {'value':None, 'status':'missing_amount', 'applied':[]}
    factors = {'correction':correction, 'winter':winter}
    active = {k:v for k,v in factors.items() if v != 1}
    if not active or amount == 0:
        return {'value':money(amount), 'status':'unchanged', 'applied':[]}
    if applied is not None:
        included = set(applied)
    elif rate is not None and quantity is not None:
        keys = list(active)
        matches = []
        base = rate * quantity
        for flags in product((False, True), repeat=len(keys)):
            expected = base
            included = {key for key, flag in zip(keys, flags) if flag}
            for key in included:
                expected *= active[key]
            if abs(Decimal(str(money(expected))) - amount) <= Decimal('.02'):
                matches.append(included)
        if len(matches) != 1:
            return {'value':money(amount), 'status':'unverified_prior_factors', 'applied':[]}
        included = matches[0]
    else:
        return {'value':money(amount), 'status':'unverified_prior_factors', 'applied':[]}
    added = [key for key in active if key not in included]
    value = amount
    for key in added:
        value *= active[key]
    return {'value':money(value), 'status':'applied' if added else 'already_applied', 'applied':added}


def sum_complete(values):
    values = [number(value) for value in values]
    return None if any(value is None for value in values) else money(sum(values, Decimal(0)))


def sum_known(values):
    """Sum available amounts, keeping an entirely unknown amount blank."""
    known = [value for item in values if (value := number(item)) is not None]
    return money(sum(known, Decimal(0))) if known else None


def overhead(labor, drivers, percent):
    base = sum_complete((labor, drivers))
    rate = number(percent)
    return None if base is None or rate is None else money(number(base) * rate / 100)
