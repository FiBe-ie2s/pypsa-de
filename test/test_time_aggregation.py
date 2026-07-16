# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT
"""
Tests for the "custom" temporal resolution in scripts/time_aggregation.py:
native hourly resolution inside user-defined windows, coarser base
resolution (e.g. 6h) for the rest of the year.
"""

import pandas as pd
import pytest

from scripts.time_aggregation import build_custom_snapshot_weightings

# Default map used in config/config.test02_custom_map.yaml:
# four mid-quarter weeks (Monday starts) of weather year 2013.
CUSTOM = {
    "base_resolution": "6h",
    "window_days": 7,
    "hourly_windows": ["2013-02-11", "2013-05-13", "2013-08-12", "2013-11-11"],
}


@pytest.fixture
def sw():
    """Native snapshot weightings for weather year 2013 (8760 hourly rows)."""
    idx = pd.date_range("2013-01-01", "2014-01-01", freq="h", inclusive="left")
    idx.name = "snapshot"
    return pd.DataFrame(1.0, index=idx, columns=["objective", "stores", "generators"])


def test_counts_weights_and_sum(sw):
    out = build_custom_snapshot_weightings(sw, CUSTOM)
    # 4 x 168 hourly + (8760 - 672) / 6 coarse = 672 + 1348 = 2020 rows
    assert len(out) == 2020
    # Total modelled time must be conserved
    assert out.objective.sum() == 8760
    assert (out.objective == 1).sum() == 672
    assert (out.objective == 6).sum() == 1348
    # All weighting columns must be aggregated identically
    assert out.objective.equals(out.stores)
    assert out.objective.equals(out.generators)


def test_window_rows_are_hourly_and_boundaries_align(sw):
    out = build_custom_snapshot_weightings(sw, CUSTOM)
    # Inside the first window: 168 hourly rows with weight 1
    window = out.loc["2013-02-11":"2013-02-17 23:00"]
    assert len(window) == 168
    assert (window.objective == 1).all()
    # Coarse rows must sit on the 6h grid (hours 0, 6, 12, 18)
    coarse = out[out.objective == 6]
    assert (coarse.index.hour % 6 == 0).all()
    assert (coarse.index.minute == 0).all()
    # Clean boundary: last coarse block before the window starts at 18:00,
    # first row of the window is exactly midnight
    assert pd.Timestamp("2013-02-10 18:00") in out.index
    assert pd.Timestamp("2013-02-11 00:00") in out.index
    assert out.loc["2013-02-10 18:00", "objective"] == 6
    assert out.loc["2013-02-11 00:00", "objective"] == 1


def test_index_is_sorted_and_unique(sw):
    out = build_custom_snapshot_weightings(sw, CUSTOM)
    assert out.index.is_monotonic_increasing
    assert out.index.is_unique


def test_missing_leap_day_handled():
    # Simulate drop_leap_day behaviour: 2016 is a leap year, Feb 29 removed.
    idx = pd.date_range("2016-01-01", "2017-01-01", freq="h", inclusive="left")
    idx = idx[~((idx.month == 2) & (idx.day == 29))]
    sw16 = pd.DataFrame(1.0, index=idx, columns=["objective", "stores", "generators"])
    # Window [Feb 24, Mar 2) spanning the (missing) Feb 29
    cfg = dict(CUSTOM, hourly_windows=["2016-02-24"])
    out = build_custom_snapshot_weightings(sw16, cfg)
    # Only existing snapshots are grouped, so the total is still conserved
    assert out.objective.sum() == len(idx)
    # The window loses its Feb 29 hours (window has 168 - 24 = 144 hourly rows)
    assert (out.objective == 1).sum() == 144


@pytest.mark.parametrize(
    "bad",
    [
        dict(CUSTOM, hourly_windows=[]),  # no windows configured
        dict(CUSTOM, hourly_windows=["2013-02-11", "2013-02-13"]),  # overlap
        dict(CUSTOM, hourly_windows=["2013-02-11", "2013-02-11"]),  # duplicate
        dict(CUSTOM, hourly_windows=["2013-12-29"]),  # extends past year end
        dict(CUSTOM, hourly_windows=["2012-12-30"]),  # starts before snapshots
        dict(CUSTOM, hourly_windows=["2013-02-11 06:00"]),  # not midnight
        dict(CUSTOM, base_resolution="5h"),  # 24 % 5 != 0
        dict(CUSTOM, base_resolution="90min"),  # not a whole number of hours
    ],
)
def test_invalid_config_raises(sw, bad):
    with pytest.raises(ValueError):
        build_custom_snapshot_weightings(sw, bad)
