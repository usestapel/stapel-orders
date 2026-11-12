"""Django system checks for stapel-orders.

- E001  ``PURCHASABLE_TYPES`` is empty: a shop with nothing to sell.
- E003  a ``PURCHASABLE_TYPES`` entry is not ``{"resolve": "<function>"}``.
- W001  a registered resolver is not reachable on this transport.
- E002 / W002  ``PAYMENT="mock"`` outside / inside the prototype posture stage.
- E004  ``PAYMENT`` names no known provider.
- E005  the money bounds no longer fit the columns (total could overflow).
- E006  an ``ADJUSTERS`` entry is not ``{"quote": "<function>", "order": int}``.
- E007  a ``CHECKOUT_HOOKS`` entry lacks ``reserve``/``commit``/``release``.
- W003  a registered adjuster or hook Function is not reachable here
  (quote shows an error, checkout answers 503 — never an error at boot,
  so a host can boot before a companion's provider is up).
"""
from django.core import checks
from django.core.exceptions import ImproperlyConfigured


@checks.register(checks.Tags.compatibility)
def check_purchasable_types(app_configs, **kwargs):
    from .conf import orders_settings
    from .registry import get_purchasable_types

    raw = orders_settings.PURCHASABLE_TYPES
    if raw is not None and not isinstance(raw, dict):
        return [checks.Error(
            "STAPEL_ORDERS['PURCHASABLE_TYPES'] must be a dict of "
            "{type_name: {'resolve': '<comm function>'} | None}.",
            id="stapel_orders.E003",
        )]
    types = get_purchasable_types()
    if not types:
        return [checks.Error(
            "STAPEL_ORDERS['PURCHASABLE_TYPES'] is empty: the cart cannot "
            "accept anything.",
            hint="Register a type, e.g. {'listing': {'resolve': "
                 "'listings.purchasables'}} (the stapel-shop preset does this), "
                 "or register_purchasable_type() from your AppConfig.ready().",
            id="stapel_orders.E001",
        )]
    findings = []
    from stapel_core.comm import function_unreachable_reason

    for name, entry in sorted(types.items()):
        if not isinstance(entry, dict) or not entry.get("resolve"):
            findings.append(checks.Error(
                f"STAPEL_ORDERS['PURCHASABLE_TYPES'][{name!r}] must be "
                "{'resolve': '<comm function name>'}.",
                id="stapel_orders.E003",
            ))
            continue
        reason = function_unreachable_reason(str(entry["resolve"]))
        if reason:
            findings.append(checks.Warning(
                f"Purchasable type {name!r}: resolver {entry['resolve']!r} is "
                f"not reachable here ({reason}). Adds and checkouts of this "
                "type will answer 503.",
                id="stapel_orders.W001",
            ))
    return findings


@checks.register(checks.Tags.compatibility)
def check_payment(app_configs, **kwargs):
    from stapel_core.django.presets import stage_finding

    from .conf import orders_settings

    try:
        provider = orders_settings.PAYMENT
    except ImproperlyConfigured as exc:
        return [checks.Error(str(exc), id="stapel_orders.E004")]
    if getattr(provider, "name", "") != "mock":
        return []
    finding = checks.Error(
        "STAPEL_ORDERS['PAYMENT'] is 'mock': every order is marked paid "
        "without any money moving.",
        hint="Use PAYMENT='none' (staff confirm by hand) or a real provider; "
             "the mock is for a prototype posture only.",
        id="stapel_orders.E002",
    )
    return [stage_finding(finding, warning_id="stapel_orders.W002")]


@checks.register(checks.Tags.compatibility)
def check_money_bounds(app_configs, **kwargs):
    from . import money
    from .services.cart import limits

    from .conf import orders_settings

    max_quantity, max_lines = limits()
    max_adjustments = int(orders_settings.MAX_ADJUSTMENTS)
    if max_quantity < 1 or max_lines < 1 or max_adjustments < 0:
        return [checks.Error(
            "STAPEL_ORDERS['MAX_LINE_QUANTITY'] and ['MAX_LINES_PER_CART'] "
            "must be positive and ['MAX_ADJUSTMENTS'] not negative.",
            id="stapel_orders.E005",
        )]
    if not money.columns_hold(max_quantity, max_lines, max_adjustments):
        return [checks.Error(
            f"MAX_LINE_QUANTITY={max_quantity} x MAX_LINES_PER_CART={max_lines} "
            f"x unit price < 10^{money.UNIT_PRICE_INTEGER_DIGITS} plus "
            f"MAX_ADJUSTMENTS={max_adjustments} x 10^{money.ADJUSTMENT_INTEGER_DIGITS} "
            f"can exceed the line ({money.LINE_TOTAL_MAX_DIGITS} digits) or order "
            f"({money.ORDER_TOTAL_MAX_DIGITS} digits) total column.",
            hint="Lower one of the limits; the columns are the proof's ceiling.",
            id="stapel_orders.E005",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_checkout_seams(app_configs, **kwargs):
    from stapel_core.comm import function_unreachable_reason

    from . import seams
    from .conf import orders_settings

    findings = []
    for key, error_id in (("ADJUSTERS", "stapel_orders.E006"),
                          ("CHECKOUT_HOOKS", "stapel_orders.E007")):
        raw = getattr(orders_settings, key)
        if raw is not None and not isinstance(raw, dict):
            findings.append(checks.Error(
                f"STAPEL_ORDERS[{key!r}] must be a dict of {{name: entry | None}}.",
                id=error_id,
            ))
    if findings:
        return findings
    seam_sets = (
        ("ADJUSTERS", "stapel_orders.E006", seams.get_adjusters(),
         seams.adjuster_entry_problem, ("quote",)),
        ("CHECKOUT_HOOKS", "stapel_orders.E007", seams.get_checkout_hooks(),
         seams.hook_entry_problem, seams.HOOK_OPERATIONS),
    )
    for key, error_id, entries, problem_of, operations in seam_sets:
        for name, entry in entries:
            problem = problem_of(entry)
            if problem:
                findings.append(checks.Error(
                    f"STAPEL_ORDERS[{key!r}][{name!r}] {problem}.",
                    id=error_id,
                ))
                continue
            for operation in operations:
                reason = function_unreachable_reason(str(entry[operation]))
                if reason:
                    findings.append(checks.Warning(
                        f"{key} {name!r}: {operation} Function {entry[operation]!r} is "
                        f"not reachable here ({reason}). Quotes will show "
                        "error.503 and checkouts will be refused until it is.",
                        id="stapel_orders.W003",
                    ))
    return findings
