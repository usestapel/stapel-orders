"""stapel-orders capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli


def main(argv=None):
    from stapel_orders._codegen import _configure

    _configure()
    from stapel_orders.conf import DEFAULTS
    from stapel_orders.urls import GATE_REGISTRY

    # Two CTO-facing axes, both behavioural (no endpoint is unmounted):
    # CHECKOUT_REQUIRES_ACCOUNT (guest checkout) and PAYMENT (none | mock |
    # a registered provider). PURCHASABLE_TYPES is the extension seam.
    axes = {"CHECKOUT_REQUIRES_ACCOUNT", "PAYMENT"}
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/orders/api/v1",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda k: k in axes,
        axis_group=axis_group_rules(
            exact={
                "CHECKOUT_REQUIRES_ACCOUNT": "orders.checkout",
                "PAYMENT": "orders.payment",
            }
        ),
        prog="stapel-orders-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
