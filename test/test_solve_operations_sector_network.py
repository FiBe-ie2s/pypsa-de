# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT
"""
Tests for scripts/solve_operations_sector_network.py: expanding a solved
coarse-resolution network to hourly resolution, overwriting time series from
an hourly donor network, fixing capacities and copperplating market zones.
"""

import numpy as np
import pandas as pd
import pypsa
import pytest

from scripts.solve_operations_sector_network import (
    apply_copperplate,
    build_block_map,
    fix_all_capacities,
    map_target_to_donor,
    overwrite_dynamic_from_donor,
    unfix_free_stores,
    upsample_to_dense,
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
        e_max_pu=pd.Series(
            np.linspace(0.5, 1.0, 48), index=DENSE
        ).resample("6h").min(),
    )
    n.add(
        "Store",
        "co2 atmosphere",
        bus="co2 atmosphere bus",
        e_nom_extendable=True,
        capital_cost=0.0,
        e_nom_max=np.inf,
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
            source.generators_t.p_max_pu["DE0 solar-2030"], expected,
            check_names=False,
        )


class TestMapTargetToDonor:
    def test_exact_and_suffix_matching(self):
        build_years = pd.Series(
            {"a solar-2030": 2030, "a solar": 0, "a pipe-2035": 0}
        )
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
