# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT
"""
Tests for scripts/solve_operations_sector_network.py: expanding a solved
coarse-resolution network to hourly resolution, overwriting time series from
an hourly donor network, fixing capacities and copperplating market zones.
"""

import logging

import numpy as np
import pandas as pd
import pypsa
import pytest

from scripts.solve_operations_sector_network import (
    apply_co2_price,
    apply_copperplate,
    build_block_map,
    capture_final_state_of_charge,
    fix_all_capacities,
    map_target_to_donor,
    overwrite_dynamic_from_donor,
    prorate_operational_limits,
    seed_initial_state_of_charge,
    unfix_free_stores,
    upsample_to_dense,
    watch_rolling_horizon_windows,
)

DENSE = pd.date_range("2013-01-01", periods=48, freq="h")
COARSE = pd.date_range("2013-01-01", periods=8, freq="6h")


def solar_profile():
    hours = np.arange(48)
    return pd.Series(
        np.clip(np.sin((hours % 24 - 6) / 12 * np.pi), 0.0, None), index=DENSE
    )


def load_profile():
    hours = np.arange(48)
    return pd.Series(100.0 + 10.0 * np.cos(hours / 24 * 2 * np.pi), index=DENSE)


def add_buses(n):
    n.add(
        "Bus",
        ["DE0", "DE1", "FR0"],
        carrier="AC",
        country=["DE", "DE", "FR"],
        v_nom=380.0,
    )
    n.add("Bus", "DE0 low voltage", carrier="low voltage", country="DE")
    n.add("Bus", "DE0 H2", carrier="H2", country="DE")
    n.add("Bus", "DE0 EV battery", carrier="EV battery", country="DE")
    n.add("Bus", "co2 atmosphere bus", carrier="co2", country="")


def make_donor():
    n = pypsa.Network()
    n.set_snapshots(DENSE)
    add_buses(n)
    n.add("Generator", "DE0 solar", bus="DE0", p_nom=1.0, p_max_pu=solar_profile())
    n.add("Generator", "DE1 gas", bus="DE1", p_nom=1.0)
    n.add("Generator", "DE0 pipe", bus="DE0", p_nom=1.0, p_max_pu=solar_profile())
    n.add("Load", "DE0 load", bus="DE0", p_set=load_profile())
    n.add(
        "Store",
        "DE0 battery",
        bus="DE0",
        e_nom=1.0,
        e_max_pu=pd.Series(np.linspace(0.5, 1.0, 48), index=DENSE),
    )
    return n


def make_source():
    """Solved coarse network: block means of the donor plus modifications."""
    n = pypsa.Network()
    n.set_snapshots(COARSE)
    n.snapshot_weightings.loc[:, :] = 6.0
    add_buses(n)

    solar_block = solar_profile().resample("6h").mean()
    load_block = load_profile().resample("6h").mean()

    # two build-year vintages of the donor's "DE0 solar"
    n.add(
        "Generator",
        "DE0 solar-2030",
        bus="DE0",
        build_year=2030,
        p_nom_extendable=True,
        p_max_pu=solar_block,
    )
    n.add(
        "Generator",
        "DE0 solar-2035",
        bus="DE0",
        build_year=2035,
        p_nom_extendable=True,
        p_max_pu=solar_block,
    )
    n.add("Generator", "DE1 gas", bus="DE1", p_nom_extendable=True, marginal_cost=50.0)
    # no donor counterpart: must stay block-constant
    n.add(
        "Generator",
        "DE0 gauge",
        bus="DE0",
        p_nom=1.0,
        p_max_pu=solar_block * 0.5,
    )
    # name ends in "-2035" but build_year is 0: suffix must NOT be stripped
    n.add(
        "Generator",
        "DE0 pipe-2035",
        bus="DE0",
        p_nom_extendable=True,
        p_max_pu=solar_block,
    )
    # demand modified after aggregation in the source run (scaling factor 1.3)
    n.add("Load", "DE0 load", bus="DE0", p_set=load_block * 1.3)

    n.add(
        "Store",
        "DE0 battery",
        bus="DE0",
        e_nom_extendable=True,
        capital_cost=10.0,
        e_max_pu=pd.Series(np.linspace(0.5, 1.0, 48), index=DENSE).resample("6h").min(),
    )
    n.add(
        "Store",
        "co2 atmosphere",
        bus="co2 atmosphere bus",
        e_nom_extendable=True,
        capital_cost=0.0,
        e_nom_max=np.inf,
    )
    # Exogenous EV battery: zero capital cost and infinite e_nom_max like the
    # co2 store, but on an ENERGY bus. Must NEVER be released, or it becomes
    # free unlimited grid storage (regression guard for the EV-battery bug).
    n.add(
        "Store",
        "DE0 EV battery",
        bus="DE0 EV battery",
        e_nom=100.0,
        e_nom_extendable=False,
        capital_cost=0.0,
        e_nom_max=np.inf,
    )
    n.add(
        "Link",
        "DE0 BEV charger",
        bus0="DE0 low voltage",
        bus1="DE0 EV battery",
        carrier="BEV charger",
        p_nom=50.0,
    )

    n.add("Line", "l-de", bus0="DE0", bus1="DE1", x=0.1, r=0.01, s_nom_extendable=True)
    n.add("Line", "l-fr", bus0="DE0", bus1="FR0", x=0.1, r=0.01, s_nom=100.0)
    n.add(
        "Link",
        "DE0 distribution",
        bus0="DE0",
        bus1="DE0 low voltage",
        p_nom_extendable=True,
    )
    n.add(
        "Link",
        "DE0 electrolysis",
        bus0="DE0",
        bus1="DE0 H2",
        p_nom_extendable=True,
        efficiency=0.7,
    )

    # optimised capacities as a solver would have left them
    n.generators.loc["DE0 solar-2030", "p_nom_opt"] = 10.0
    n.generators.loc["DE0 solar-2035", "p_nom_opt"] = 20.0
    n.generators.loc["DE1 gas", "p_nom_opt"] = 500.0
    n.generators.loc["DE0 pipe-2035", "p_nom_opt"] = 7.0
    n.stores.loc["DE0 battery", "e_nom_opt"] = 50.0
    n.stores.loc["co2 atmosphere", "e_nom_opt"] = 123.0
    n.lines.loc["l-de", "s_nom_opt"] = 80.0
    n.links.loc["DE0 distribution", "p_nom_opt"] = 300.0
    n.links.loc["DE0 electrolysis", "p_nom_opt"] = 5.0
    return n


@pytest.fixture
def donor():
    return make_donor()


@pytest.fixture
def source():
    return make_source()


class TestBuildBlockMap:
    def test_uniform_blocks(self):
        bm = build_block_map(COARSE, DENSE)
        assert len(bm) == 48
        assert bm.loc[pd.Timestamp("2013-01-01 00:00")] == pd.Timestamp(
            "2013-01-01 00:00"
        )
        assert bm.loc[pd.Timestamp("2013-01-01 05:00")] == pd.Timestamp(
            "2013-01-01 00:00"
        )
        assert bm.loc[pd.Timestamp("2013-01-02 23:00")] == pd.Timestamp(
            "2013-01-02 18:00"
        )

    def test_mixed_resolution(self):
        mixed = pd.DatetimeIndex(
            list(pd.date_range("2013-01-01", periods=24, freq="h"))
            + list(pd.date_range("2013-01-02", periods=4, freq="6h"))
        )
        bm = build_block_map(mixed, DENSE)
        # inside the hourly window snapshots map to themselves
        assert bm.loc[pd.Timestamp("2013-01-01 05:00")] == pd.Timestamp(
            "2013-01-01 05:00"
        )
        # in the coarse part they map to the block start
        assert bm.loc[pd.Timestamp("2013-01-02 05:00")] == pd.Timestamp(
            "2013-01-02 00:00"
        )
        assert bm.loc[pd.Timestamp("2013-01-02 23:00")] == pd.Timestamp(
            "2013-01-02 18:00"
        )

    def test_dense_before_source_raises(self):
        with pytest.raises(ValueError, match="before the first"):
            build_block_map(COARSE[1:], DENSE)


class TestUpsample:
    def test_snapshots_weightings_and_ffill(self, source):
        expected = source.generators_t.p_max_pu["DE0 solar-2030"].reindex(
            DENSE, method="ffill"
        )
        upsample_to_dense(source, DENSE)
        assert len(source.snapshots) == 48
        assert (source.snapshot_weightings == 1.0).all().all()
        pd.testing.assert_series_equal(
            source.generators_t.p_max_pu["DE0 solar-2030"],
            expected,
            check_names=False,
        )


class TestMapTargetToDonor:
    def test_exact_and_suffix_matching(self):
        build_years = pd.Series({"a solar-2030": 2030, "a solar": 0, "a pipe-2035": 0})
        mapping = map_target_to_donor(
            pd.Index(["a solar-2030", "a solar", "a pipe-2035"]),
            build_years,
            pd.Index(["a solar", "a pipe"]),
        )
        assert mapping == {"a solar-2030": "a solar", "a solar": "a solar"}

    def test_suffix_requires_matching_build_year(self):
        mapping = map_target_to_donor(
            pd.Index(["a solar-2030"]),
            pd.Series({"a solar-2030": 2025}),
            pd.Index(["a solar"]),
        )
        assert mapping == {}


class TestOverwriteFromDonor:
    @pytest.fixture
    def pipeline(self, source, donor):
        block_map = upsample_to_dense(source, donor.snapshots)
        summary = overwrite_dynamic_from_donor(source, donor, block_map)
        return source, donor, summary

    def test_vintages_reproduce_donor_exactly(self, pipeline):
        source, donor, _ = pipeline
        for name in ("DE0 solar-2030", "DE0 solar-2035"):
            np.testing.assert_allclose(
                source.generators_t.p_max_pu[name].values,
                donor.generators_t.p_max_pu["DE0 solar"].values,
                rtol=1e-9,
                atol=1e-12,
            )

    def test_modified_load_keeps_scaling(self, pipeline):
        source, donor, _ = pipeline
        np.testing.assert_allclose(
            source.loads_t.p_set["DE0 load"].values,
            1.3 * donor.loads_t.p_set["DE0 load"].values,
            rtol=1e-9,
        )

    def test_unmatched_column_stays_block_constant(self, pipeline):
        source, _, _ = pipeline
        gauge = source.generators_t.p_max_pu["DE0 gauge"]
        # block-constant: identical within each 6h block
        assert (gauge.groupby(pd.Grouper(freq="6h")).nunique() == 1).all()

    def test_build_year_zero_suffix_not_stripped(self, pipeline):
        source, donor, _ = pipeline
        pipe = source.generators_t.p_max_pu["DE0 pipe-2035"]
        assert (pipe.groupby(pd.Grouper(freq="6h")).nunique() == 1).all()
        assert not np.allclose(
            pipe.values, donor.generators_t.p_max_pu["DE0 pipe"].values
        )

    def test_minmax_attrs_copied_plainly(self, pipeline):
        source, donor, _ = pipeline
        pd.testing.assert_series_equal(
            source.stores_t.e_max_pu["DE0 battery"],
            donor.stores_t.e_max_pu["DE0 battery"],
            check_names=False,
        )


class TestFixCapacities:
    def test_fix_and_unfix_free_stores(self, source):
        fix_all_capacities(source)

        assert not source.generators.p_nom_extendable.any()
        assert not source.links.p_nom_extendable.any()
        assert not source.stores.e_nom_extendable.any()
        assert not source.lines.s_nom_extendable.any()
        assert source.generators.loc["DE0 solar-2030", "p_nom"] == 10.0
        assert source.generators.loc["DE1 gas", "p_nom"] == 500.0
        assert source.stores.loc["DE0 battery", "e_nom"] == 50.0
        assert source.lines.loc["l-de", "s_nom"] == 80.0
        assert source.links.loc["DE0 distribution", "p_nom"] == 300.0

        freed = unfix_free_stores(source)
        assert list(freed) == ["co2 atmosphere"]
        assert source.stores.loc["co2 atmosphere", "e_nom_extendable"]
        assert not source.stores.loc["DE0 battery", "e_nom_extendable"]
        # EV battery is zero-cost with infinite e_nom_max but on an energy bus:
        # it must stay fixed, otherwise it becomes free unlimited grid storage.
        assert "DE0 EV battery" not in list(freed)
        assert not source.stores.loc["DE0 EV battery", "e_nom_extendable"]
        assert source.stores.loc["DE0 EV battery", "e_nom"] == 100.0


class TestCopperplate:
    def test_intra_zone_removed_others_kept(self, source):
        groups = apply_copperplate(source, [["DE"]])
        assert groups == [["DE0", "DE1"]]

        assert "l-de" not in source.lines.index
        assert "l-fr" in source.lines.index
        assert "DE0 distribution" in source.links.index
        assert "DE0 electrolysis" in source.links.index

        copper = source.links.index[source.links.carrier == "copper"]
        assert len(copper) == 1
        link = source.links.loc[copper[0]]
        assert np.isinf(link.p_nom)
        assert link.p_min_pu == -1
        assert {link.bus0, link.bus1} == {"DE0", "DE1"}
        assert "copper" in source.carriers.index

    def test_too_small_group_is_skipped(self, source):
        assert apply_copperplate(source, [["FR"]]) == []
        assert "l-fr" in source.lines.index


class TestFullPipelineSolve:
    def test_dispatch_gives_uniform_zone_price(self, source, donor):
        pytest.importorskip("highspy")

        block_map = upsample_to_dense(source, donor.snapshots)
        overwrite_dynamic_from_donor(source, donor, block_map)
        fix_all_capacities(source)
        unfix_free_stores(source)
        apply_copperplate(source, [["DE"]])

        status, condition = source.optimize(solver_name="highs")
        assert status == "ok"

        prices = source.buses_t.marginal_price
        # copperplated zone: uniform German price despite the load sitting at
        # DE0 and the dispatchable capacity at DE1 (their line was removed)
        np.testing.assert_allclose(
            prices["DE0"].values, prices["DE1"].values, atol=1e-6
        )
        # demand must be fully served by DE1 gas through the copper link
        served = source.generators_t.p.sum(axis=1)
        np.testing.assert_allclose(
            served.values,
            source.loads_t.p_set["DE0 load"].values,
            rtol=1e-6,
        )


@pytest.fixture
def constrained():
    """Hourly network carrying the three GlobalConstraint types of a real run."""
    n = pypsa.Network()
    n.set_snapshots(DENSE)  # 48 hourly snapshots
    add_buses(n)
    n.add(
        "GlobalConstraint",
        "unsustainable biomass limit",
        type="operational_limit",
        carrier_attribute="unsustainable solid biomass",
        sense="==",
        constant=2400.0,
    )
    n.add(
        "GlobalConstraint",
        "biomass limit",
        type="operational_limit",
        carrier_attribute="solid biomass",
        sense="<=",
        constant=4800.0,
    )
    n.add(
        "GlobalConstraint",
        "CO2Limit",
        type="co2_atmosphere",
        carrier_attribute="co2_emissions",
        sense="<=",
        constant=960.0,
    )
    # ignored by PyPSA (empty type), must not be touched either
    n.add("GlobalConstraint", "capacity_minimum-DE", sense=">=", constant=12.0)
    n.add("Store", "co2 atmosphere", bus="co2 atmosphere bus", e_nom_extendable=True)
    return n


class TestProrateOperationalLimits:
    def test_scales_only_operational_limits(self, constrained):
        original = prorate_operational_limits(constrained, horizon=12)

        constants = constrained.global_constraints.constant
        # 12 h window out of 48 snapshots -> quarter of the annual budget
        assert constants["unsustainable biomass limit"] == pytest.approx(600.0)
        assert constants["biomass limit"] == pytest.approx(1200.0)
        # stock-type and untyped constraints stay untouched
        assert constants["CO2Limit"] == pytest.approx(960.0)
        assert constants["capacity_minimum-DE"] == pytest.approx(12.0)

        assert set(original.index) == {"unsustainable biomass limit", "biomass limit"}
        assert original["unsustainable biomass limit"] == pytest.approx(2400.0)

    def test_annual_total_is_restored_independently_of_overlap(self, constrained):
        """
        Every hour carries one window's worth of the budget, so the yearly
        total comes back to the original constant for any overlap.
        """
        horizon = 12
        prorate_operational_limits(constrained, horizon=horizon)
        per_window = constrained.global_constraints.constant[
            "unsustainable biomass limit"
        ]
        hourly_rate = per_window / horizon
        assert hourly_rate * len(constrained.snapshots) == pytest.approx(2400.0)

    def test_window_covering_whole_year_is_not_scaled(self, constrained):
        prorate_operational_limits(constrained, horizon=48)
        assert constrained.global_constraints.constant[
            "unsustainable biomass limit"
        ] == pytest.approx(2400.0)

        prorate_operational_limits(constrained, horizon=100)
        assert constrained.global_constraints.constant[
            "unsustainable biomass limit"
        ] == pytest.approx(2400.0)

    def test_no_operational_limits_is_a_noop(self):
        n = pypsa.Network()
        n.set_snapshots(DENSE)
        assert prorate_operational_limits(n, horizon=12).empty


class TestApplyCo2Price:
    def test_prices_the_atmosphere_store(self, constrained):
        apply_co2_price(constrained, 123.6)
        # negative sign: emitting (filling the store) costs co2_price per tonne
        assert constrained.stores.at[
            "co2 atmosphere", "marginal_cost"
        ] == pytest.approx(-123.6)

    def test_removes_the_co2_budget_but_keeps_operational_limits(self, constrained):
        apply_co2_price(constrained, 123.6)
        gc = constrained.global_constraints
        assert (gc.type == "co2_atmosphere").sum() == 0
        assert set(gc.index[gc.type == "operational_limit"]) == {
            "unsustainable biomass limit",
            "biomass limit",
        }

    def test_raises_without_an_atmosphere_store(self):
        n = pypsa.Network()
        n.set_snapshots(DENSE)
        n.add("Bus", "DE0", carrier="AC")
        with pytest.raises(ValueError, match="atmospheric CO2 store"):
            apply_co2_price(n, 123.6)


class TestWatchRollingHorizonWindows:
    def test_passes_when_all_windows_solve(self):
        with watch_rolling_horizon_windows() as watcher:
            logging.getLogger("pypsa.optimization.abstract").warning("all good")
        assert watcher.failures == []

    def test_raises_on_a_failed_window(self):
        with pytest.raises(RuntimeError, match="failed to solve"):
            with watch_rolling_horizon_windows():
                logging.getLogger("pypsa.optimization.abstract").warning(
                    "Optimization failed with status %s and condition %s",
                    "warning",
                    "infeasible",
                )

    def test_handler_is_detached_afterwards(self):
        before = len(logging.getLogger("pypsa").handlers)
        with watch_rolling_horizon_windows():
            pass
        assert len(logging.getLogger("pypsa").handlers) == before


SNS_RH = pd.date_range("2013-01-01", periods=24, freq="h")
RH_HORIZON = 6  # -> 4 windows over the 24 h "year"


def make_rolling_horizon_network():
    """
    Minimal dispatch network with an annual '==' operational_limit.

    Mirrors the real defect: 'unsustainable biomass limit' forces a fixed annual
    quantity, and cheap biomass would otherwise displace the expensive gas unit.
    """
    n = pypsa.Network()
    n.set_snapshots(SNS_RH)
    n.add("Bus", "DE0", carrier="AC", country="DE")
    n.add("Carrier", ["unsustainable solid biomass", "gas"])
    n.add(
        "Generator",
        "DE0 biomass",
        bus="DE0",
        carrier="unsustainable solid biomass",
        p_nom=200.0,
        marginal_cost=1.0,
    )
    n.add(
        "Generator",
        "DE0 gas",
        bus="DE0",
        carrier="gas",
        p_nom=200.0,
        marginal_cost=50.0,
    )
    n.add("Load", "DE0 load", bus="DE0", p_set=100.0)
    n.add(
        "GlobalConstraint",
        "unsustainable biomass limit",
        type="operational_limit",
        carrier_attribute="unsustainable solid biomass",
        sense="==",
        constant=480.0,  # 20 MWh/h over the 24 h year
    )
    return n


def solve_rolling_horizon(n):
    n.optimize.optimize_with_rolling_horizon(
        horizon=RH_HORIZON, overlap=0, solver_name="highs"
    )
    return n.generators_t.p["DE0 biomass"].mul(n.snapshot_weightings.objective).sum()


class TestRollingHorizonEndToEnd:
    """Reproduce the annual-budget-per-window defect against real PyPSA."""

    def test_unscaled_annual_limit_is_enforced_once_per_window(self):
        pytest.importorskip("highspy")
        n = make_rolling_horizon_network()

        total = solve_rolling_horizon(n)

        # the bug: 480 MWh forced in each of the 4 windows
        n_windows = len(SNS_RH) / RH_HORIZON
        assert total == pytest.approx(480.0 * n_windows, rel=1e-4)

    def test_prorating_restores_the_annual_total(self):
        pytest.importorskip("highspy")
        n = make_rolling_horizon_network()

        prorate_operational_limits(n, horizon=RH_HORIZON)
        total = solve_rolling_horizon(n)

        assert total == pytest.approx(480.0, rel=1e-4)

    def test_prorating_matches_the_single_pass_solution(self):
        pytest.importorskip("highspy")
        single = make_rolling_horizon_network()
        single.optimize(solver_name="highs")
        expected = (
            single.generators_t.p["DE0 biomass"]
            .mul(single.snapshot_weightings.objective)
            .sum()
        )

        rh = make_rolling_horizon_network()
        prorate_operational_limits(rh, horizon=RH_HORIZON)
        assert solve_rolling_horizon(rh) == pytest.approx(expected, rel=1e-4)


def make_solved_storage_source():
    """Source network with solved, cyclic storage levels at its last snapshot."""
    n = pypsa.Network()
    n.set_snapshots(COARSE)
    add_buses(n)
    n.add("Store", "DE0 H2", bus="DE0 H2", e_nom=100.0, e_cyclic=True)
    n.add("Store", "DE0 battery", bus="DE0", e_nom=10.0, e_cyclic=True)
    n.add("Store", "co2 atmosphere", bus="co2 atmosphere bus", e_nom_extendable=True)
    n.add("StorageUnit", "DE0 PHS", bus="DE0", p_nom=5.0, cyclic_state_of_charge=True)

    n.stores_t.e = pd.DataFrame(
        {
            "DE0 H2": np.linspace(10.0, 93.0, len(COARSE)),
            "DE0 battery": np.linspace(1.0, 4.0, len(COARSE)),
            "co2 atmosphere": np.linspace(0.0, 950.0, len(COARSE)),
        },
        index=COARSE,
    )
    n.storage_units_t.state_of_charge = pd.DataFrame(
        {"DE0 PHS": np.linspace(0.0, 3.5, len(COARSE))}, index=COARSE
    )
    return n


class TestSeedInitialStateOfCharge:
    def test_captures_the_last_snapshot(self):
        levels = capture_final_state_of_charge(make_solved_storage_source())
        assert levels["Store"]["DE0 H2"] == pytest.approx(93.0)
        assert levels["StorageUnit"]["DE0 PHS"] == pytest.approx(3.5)

    def test_energy_stores_are_seeded(self):
        source = make_solved_storage_source()
        levels = capture_final_state_of_charge(source)

        target = make_solved_storage_source()
        target.stores["e_initial"] = 0.0  # as prepare_network leaves it
        target.storage_units["state_of_charge_initial"] = 0.0
        seed_initial_state_of_charge(target, levels)

        assert target.stores.at["DE0 H2", "e_initial"] == pytest.approx(93.0)
        assert target.stores.at["DE0 battery", "e_initial"] == pytest.approx(4.0)
        assert target.storage_units.at[
            "DE0 PHS", "state_of_charge_initial"
        ] == pytest.approx(3.5)

    def test_accounting_stores_stay_at_zero(self):
        """
        The co2 atmosphere level IS the dispatch year's emission balance;
        inheriting the source year's total would exhaust the budget instantly.
        """
        source = make_solved_storage_source()
        levels = capture_final_state_of_charge(source)

        target = make_solved_storage_source()
        target.stores["e_initial"] = 0.0
        summary = seed_initial_state_of_charge(target, levels)

        assert target.stores.at["co2 atmosphere", "e_initial"] == 0.0
        store_row = summary.set_index("component").loc["Store"]
        assert store_row["accounting_kept_at_zero"] == 1
        assert store_row["seeded"] == 2

    def test_unmatched_components_stay_empty(self):
        levels = capture_final_state_of_charge(make_solved_storage_source())
        target = make_solved_storage_source()
        target.stores["e_initial"] = 0.0
        target.add("Store", "DE0 new", bus="DE0", e_nom=1.0)

        summary = seed_initial_state_of_charge(target, levels)

        assert target.stores.at["DE0 new", "e_initial"] == 0.0
        assert summary.set_index("component").loc["Store", "unmatched"] == 1

    def test_source_without_solved_levels_is_a_noop(self):
        n = pypsa.Network()
        n.set_snapshots(COARSE)
        add_buses(n)
        n.add("Store", "DE0 H2", bus="DE0 H2", e_nom=100.0)
        assert capture_final_state_of_charge(n) == {}
        assert seed_initial_state_of_charge(n, {}).empty
        assert n.stores.at["DE0 H2", "e_initial"] == 0.0


class TestPrepareNetworkCyclicFlags:
    """
    Guard for the `state_of_charge_cyclic` typo: the assignment must reach
    the real PyPSA attribute, not create a stray column.
    """

    def test_rolling_horizon_disables_storage_unit_cyclicity(self):
        from scripts.solve_network import prepare_network

        n = pypsa.Network()
        n.set_snapshots(COARSE)
        add_buses(n)
        n.add(
            "StorageUnit",
            "DE0 PHS",
            bus="DE0",
            p_nom=5.0,
            cyclic_state_of_charge=True,
            state_of_charge_initial=2.0,
        )
        n.add("Store", "DE0 H2", bus="DE0 H2", e_nom=1.0, e_cyclic=True, e_initial=0.5)
        # add_land_use_constraint() needs a non-empty generators frame;
        # extendable so it is not treated as existing capacity to subtract
        n.add(
            "Generator",
            "DE0 solar",
            bus="DE0",
            carrier="solar",
            p_nom_extendable=True,
        )

        prepare_network(
            n,
            solve_opts={},
            foresight="myopic",
            planning_horizons="2035",
            co2_sequestration_potential={},
            rolling_horizon=True,
        )

        assert not n.storage_units.cyclic_state_of_charge.any()
        assert (n.storage_units.state_of_charge_initial == 0).all()
        assert not n.stores.e_cyclic.any()
        assert (n.stores.e_initial == 0).all()

    def test_single_pass_leaves_cyclicity_untouched(self):
        from scripts.solve_network import prepare_network

        n = pypsa.Network()
        n.set_snapshots(COARSE)
        add_buses(n)
        n.add(
            "StorageUnit",
            "DE0 PHS",
            bus="DE0",
            p_nom=5.0,
            cyclic_state_of_charge=True,
            state_of_charge_initial=2.0,
        )
        n.add("Store", "DE0 H2", bus="DE0 H2", e_nom=1.0, e_cyclic=True, e_initial=0.5)
        # add_land_use_constraint() needs a non-empty generators frame;
        # extendable so it is not treated as existing capacity to subtract
        n.add(
            "Generator",
            "DE0 solar",
            bus="DE0",
            carrier="solar",
            p_nom_extendable=True,
        )

        prepare_network(
            n,
            solve_opts={},
            foresight="myopic",
            planning_horizons="2035",
            co2_sequestration_potential={},
            rolling_horizon=False,
        )

        assert n.storage_units.cyclic_state_of_charge.all()
        assert n.storage_units.at["DE0 PHS", "state_of_charge_initial"] == 2.0
        assert n.stores.e_cyclic.all()
        assert n.stores.at["DE0 H2", "e_initial"] == 0.5
