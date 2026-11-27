"""Functions behind the uv scripts in pyproject.toml (list: top of README.md).

Each one runs existing modules with `python -m` from the repository root. Extra command-line
arguments are passed on to a single module, for example `uv run pi-node --check`.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HAL = [("hal.packet",), ("hal.lora433",), ("hal.pi_hal", "--check")]
EDGE = [("edge.aqua_edge",)]
GATEWAY = [("gateway.topology",), ("gateway.core",), ("tests.test_gateway_analytics",)]
QUALITY = [("tinyml.quality.quality_model",)]
CONSUMPTION = [("tinyml.consumption.test_integration",)]
LEAKAGE = [("tinyml.leakage.test_regression",), ("tinyml.leakage.test_offline_supply",),
           ("tinyml.leakage.test_supply_robustness",), ("tinyml.leakage.compare_leaks", "--check")]
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
    _suite(HAL + EDGE + GATEWAY + QUALITY + CONSUMPTION + LEAKAGE + RUNTIME)


def test_hal():
    _suite(HAL)


def test_edge():
    _suite(EDGE)


def test_gateway():
    _suite(GATEWAY)


def test_quality():
    _suite(QUALITY)


def test_consumption():
    _suite(CONSUMPTION)


def test_leakage():
    _suite(LEAKAGE)


def test_runtime():
    _suite(RUNTIME)


def test_parity():
    _one("tinyml.quality.test_temporal_parity")


# entry points
def edge_based_node():
    _one("edge.edge_based")


def edge_based_gateway():
    _one("gateway.edge_based")


def cloud_based_node():
    _one("edge.cloud_based")


def cloud_based_gateway():
    _one("gateway.cloud_based")


def pi_node():
    _one("hal.pi_hal")


# experiments
def exp_compare_leaks():
    _one("tinyml.leakage.compare_leaks")


def exp_validate_campaign():
    _one("tinyml.leakage.validate_campaign")


def exp_validate_swifeed():
    _one("tinyml.leakage.validate_swifeed")


def exp_validate_cache_county():
    _one("tinyml.leakage.validate_cache_county")


def exp_consumption_building_b():
    _one("tinyml.consumption.eval_building_b")


def exp_consumption_cache_county():
    _one("tinyml.consumption.eval_cache_county")


def exp_static_anomaly():
    _one("tinyml.quality.exp1_multivariate_anomaly")


def exp_temporal_anomaly():
    _one("tinyml.quality.exp2_temporal_anomaly")


def exp_train_quality():
    _one("tinyml.quality.train_quality")
