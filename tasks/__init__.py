"""Functions behind the uv scripts in pyproject.toml (list: top of README.md).

Each one runs existing modules with `python -m` from the repository root. Extra command-line
arguments are passed on to a single module, for example `uv run edge-based-node --help`.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HAL = [("hal.packet",), ("hal.lora433",), ("hal.lora_pi", "--check"),
       ("hal.esp8266_passthrough", "--check"), ("hal.link",), ("tests.test_hal",)]
EDGE = [("edge.aqua_edge",), ("tests.test_pi_node",)]
GATEWAY = [("gateway.topology",), ("gateway.core",), ("tests.test_gateway_analytics",), ("tests.test_gateway_server",),
           ("tests.test_small_buildings",)]
COMMS = [("comms.common",), ("tests.test_comms",)]
QUALITY = [("detection.quality.quality_model",)]
CONSUMPTION = [("detection.consumption.test_integration",)]
LEAKAGE = [("detection.leakage.test_regression",), ("detection.leakage.test_offline_supply",),
           ("detection.leakage.test_supply_robustness",), ("detection.leakage.compare_leaks", "--check")]
RUNTIME = [("tests.test_unified_runtime",)]


def _call(module, *args):
    return subprocess.call([sys.executable, "-m", module, *args], cwd=ROOT)


def _one(module):
    sys.exit(_call(module, *sys.argv[1:]))


def _suite(checks):
    """Run every check, even after a failure, then list the ones that failed."""
    failed = []
    for check in checks:
        print(f"\n=== {' '.join(check)}", flush=True)
        if _call(*check):
            failed.append(" ".join(check))
    print(f"\n{len(checks) - len(failed)} of {len(checks)} checks passed" + "".join(f"\nFAILED: {f}" for f in failed))
    sys.exit(1 if failed else 0)


# tests
def test_all():
    _suite(HAL + EDGE + GATEWAY + COMMS + QUALITY + CONSUMPTION + LEAKAGE + RUNTIME)


def test_hal():
    _suite(HAL)


def test_edge():
    _suite(EDGE)


def test_gateway():
    _suite(GATEWAY)


def test_comms():
    _suite(COMMS)


def test_quality():
    _suite(QUALITY)


def test_consumption():
    _suite(CONSUMPTION)


def test_leakage():
    _suite(LEAKAGE)


def test_runtime():
    _suite(RUNTIME)


def test_parity():
    _one("detection.quality.test_temporal_parity")


def test_dashboard():
    _one("dashboard.test_dashboard")


# entry points
def edge_based_node():
    _one("edge.edge_based")


def edge_based_gateway():
    _one("gateway.edge_based")


def cloud_based_node():
    _one("edge.cloud_based")


def cloud_based_gateway():
    _one("gateway.cloud_based")


def pi_radio_test():
    sys.exit(_call("hal.lora_pi", "--radio-test"))


def gate_radio_test():
    sys.exit(_call("hal.esp8266_passthrough", "--radio-test", *sys.argv[1:]))


def dashboard_server():
    _one("dashboard.server")


# experiments
def exp_compare_leaks():
    _one("detection.leakage.compare_leaks")


def exp_validate_campaign():
    _one("detection.leakage.validate_campaign")


def exp_validate_swifeed():
    _one("detection.leakage.validate_swifeed")


def exp_validate_cache_county():
    _one("detection.leakage.validate_cache_county")


def exp_consumption_building_b():
    _one("detection.consumption.eval_building_b")


def exp_consumption_cache_county():
    _one("detection.consumption.eval_cache_county")


def exp_static_anomaly():
    _one("detection.quality.exp1_multivariate_anomaly")


def exp_temporal_anomaly():
    _one("detection.quality.exp2_temporal_anomaly")


def exp_train_quality():
    _one("detection.quality.train_quality")
