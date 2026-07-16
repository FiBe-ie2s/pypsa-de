# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT
"""
Defines the time aggregation to be used for sector-coupled network.

Description
-----------
Computes a time aggregation scheme for the given network, in the form of a CSV
file with the snapshot weightings, indexed by the new subset of snapshots. This
rule only computes said aggregation scheme; aggregation of time-varying network
data is done in ``prepare_sector_network.py``.
"""

import logging

import numpy as np
import pandas as pd
import pypsa
import tsam.timeseriesaggregation as tsam
import xarray as xr

from scripts._helpers import (
    configure_logging,
    set_scenario_config,
    update_config_from_wildcards,
)

logger = logging.getLogger(__name__)


def build_custom_snapshot_weightings(
    snapshot_weightings: pd.DataFrame, custom: dict
) -> pd.DataFrame:
    """
    Build snapshot weightings for the "custom" temporal resolution.

    Keeps the native (hourly) resolution inside the user-defined windows
    given in ``custom["hourly_windows"]`` (each ``custom["window_days"]``
    days long) and aggregates all other snapshots to the coarser
    ``custom["base_resolution"]`` (e.g. "6h").

    Implemented as a groupby on the actual native snapshots: every native
    snapshot is labelled with its aggregation target (itself inside a
    window, the start of its coarse block outside), then the native
    weightings are summed per label. This is robust to non-contiguous
    indices (e.g. dropped leap days or multi-year snapshots) because only
    snapshots that actually exist are grouped.

    Parameters
    ----------
    snapshot_weightings : pd.DataFrame
        Native snapshot weightings (index: hourly timestamps, columns:
        objective, stores, generators), i.e. ``n.snapshot_weightings``.
    custom : dict
        The ``clustering.temporal.custom`` config block with keys
        ``base_resolution``, ``window_days`` and ``hourly_windows``.

    Returns
    -------
    pd.DataFrame
        Aggregated snapshot weightings: hourly rows (weight 1) inside the
        windows, coarse rows (weight = block length in hours) elsewhere.
    """
    sns = pd.DatetimeIndex(snapshot_weightings.index)

    base_resolution = str(custom["base_resolution"]).lower()  # e.g. "6h"
    window_days = int(custom["window_days"])
    window_starts = sorted(pd.Timestamp(d) for d in custom["hourly_windows"])

    # --- validation -------------------------------------------------------
    if not window_starts:
        raise ValueError(
            "clustering.temporal.custom.hourly_windows must contain at least "
            "one window start date when resolution_sector is 'custom'."
        )
    base_hours = pd.Timedelta(base_resolution) / pd.Timedelta("1h")
    # base_resolution must divide 24h: pd.DatetimeIndex.floor() aligns blocks
    # to midnight only in that case, which also guarantees that no coarse
    # block straddles a (midnight) window boundary.
    if base_hours != int(base_hours) or 24 % int(base_hours) != 0:
        raise ValueError(
            f"clustering.temporal.custom.base_resolution {custom['base_resolution']!r} "
            "must be a whole number of hours dividing 24 (e.g. 2h, 3h, 4h, 6h, "
            "8h, 12h) so coarse blocks align with midnight window boundaries."
        )
    if len(set(window_starts)) != len(window_starts):
        raise ValueError(
            "clustering.temporal.custom.hourly_windows contains duplicate dates: "
            f"{[str(d.date()) for d in window_starts]}"
        )
    for start in window_starts:
        if start != start.normalize():
            raise ValueError(
                f"Window start {start} must be at midnight (a plain date such "
                "as '2013-02-11') so windows align with coarse blocks."
            )
    windows = [(s, s + pd.Timedelta(days=window_days)) for s in window_starts]
    for (s1, e1), (s2, _) in zip(windows[:-1], windows[1:]):
        if e1 > s2:
            raise ValueError(
                f"Hourly windows starting {s1.date()} and {s2.date()} overlap "
                f"({window_days} days each). Choose non-overlapping windows."
            )
    step = pd.Timedelta("1h")  # native resolution of the base network
    for s, e in windows:
        if s < sns[0] or e > sns[-1] + step:
            raise ValueError(
                f"Hourly window {s.date()} to {e.date()} (exclusive) is not "
                f"fully inside the snapshots ({sns[0]} to {sns[-1]})."
            )

    # --- label every native snapshot with its aggregation target -----------
    # Inside a window: the snapshot itself (kept at native resolution).
    # Outside: the start of its coarse block (floor to base_resolution).
    # Note: no explicit leap-day handling is needed. If drop_leap_day removed
    # Feb 29 from the snapshots, those hours simply do not exist here and the
    # surrounding coarse blocks keep correct (smaller) weights automatically.
    in_window = np.zeros(len(sns), dtype=bool)
    for s, e in windows:
        in_window |= (sns >= s) & (sns < e)
    labels = pd.Series(sns.floor(base_resolution), index=sns)
    labels[in_window] = sns[in_window]

    # Sum native weightings per target snapshot (groupby sorts the index).
    sw = snapshot_weightings.groupby(labels).sum()
    sw.index.name = snapshot_weightings.index.name

    # Sanity check: total modelled time must be conserved.
    assert np.isclose(sw.objective.sum(), snapshot_weightings.objective.sum()), (
        "Sum of aggregated objective weightings does not match the native total."
    )

    n_hourly = int(in_window.sum())
    logger.info(
        "Custom temporal aggregation: %d native snapshots -> %d effective "
        "snapshots (%d hourly in %d windows of %d days, %d blocks at %s).",
        len(sns),
        len(sw),
        n_hourly,
        len(windows),
        window_days,
        len(sw) - n_hourly,
        base_resolution,
    )
    logger.info(
        f"Distribution of snapshot durations:\n{sw.objective.value_counts()}"
    )
    return sw


if __name__ == "__main__":
    if "snakemake" not in globals():
        from scripts._helpers import mock_snakemake

        snakemake = mock_snakemake(
            "time_aggregation",
            configfiles="test/config.overnight.yaml",
            opts="",
            clusters="37",
            sector_opts="Co2L0-24h-T-H-B-I-A-dist1",
            planning_horizons="2030",
        )

    configure_logging(snakemake)
    set_scenario_config(snakemake)
    update_config_from_wildcards(snakemake.config, snakemake.wildcards)

    n = pypsa.Network(snakemake.input.network)
    # Full clustering.temporal config dict (includes the "custom" sub-block)
    temporal_config = snakemake.params.time_resolution

    if temporal_config["resolution_elec"] not in (False, 1, "1h", "1H"):
        raise ValueError(
            f"Invalid configuration: expected 'resolution_elec' = False for the "
            f"sector-coupled model, received {temporal_config['resolution_elec']!r}. "
            "Use 'resolution_sector' to define temporal resolution instead."
        )
    resolution = temporal_config["resolution_sector"]

    # Representative snapshots
    if not resolution or isinstance(resolution, str) and "sn" in resolution.lower():
        logger.info("Use representative snapshot or no aggregation at all")
        # Output an empty csv; this is taken care of in prepare_sector_network.py
        pd.DataFrame().to_csv(snakemake.output.snapshot_weightings)

    # Custom aggregation: native hourly resolution inside user-defined
    # windows, coarser base resolution (e.g. 6h) for the rest of the year.
    # Configured under clustering.temporal.custom.
    elif isinstance(resolution, str) and resolution.lower() == "custom":
        custom = temporal_config.get("custom") or {}
        if not custom.get("hourly_windows"):
            raise ValueError(
                "resolution_sector 'custom' requires a configured "
                "clustering.temporal.custom block with non-empty "
                "'hourly_windows'."
            )
        snapshot_weightings = build_custom_snapshot_weightings(
            n.snapshot_weightings, custom
        )
        snapshot_weightings.to_csv(snakemake.output.snapshot_weightings)

    # Plain resampling
    elif isinstance(resolution, str) and "h" in resolution.lower():
        offset = resolution.lower()
        logger.info(f"Averaging every {offset} hours")

        # Resample years separately to handle non-contiguous years
        years = pd.DatetimeIndex(n.snapshots).year.unique()
        snapshot_weightings = []
        for year in years:
            sws_year = n.snapshot_weightings[n.snapshots.year == year]
            sws_year = sws_year.resample(offset).sum()
            snapshot_weightings.append(sws_year)
        snapshot_weightings = pd.concat(snapshot_weightings)

        # The resampling produces a contiguous date range. In case the original
        # index was not contiguous, all rows with zero weight must be dropped
        # (corresponding to time steps not included in the original snapshots).
        zeros_i = snapshot_weightings.query("objective == 0").index
        snapshot_weightings.drop(zeros_i, inplace=True)

        swi = snapshot_weightings.index
        leap_days = swi[(swi.month == 2) & (swi.day == 29)]
        if snakemake.params.drop_leap_day and not leap_days.empty:
            for year in leap_days.year.unique():
                year_leap_days = leap_days[leap_days.year == year]
                leap_weights = snapshot_weightings.loc[year_leap_days].sum()
                march_first = pd.Timestamp(year, 3, 1, 0, 0, 0)
                snapshot_weightings.loc[march_first] = leap_weights
            snapshot_weightings = snapshot_weightings.drop(leap_days).sort_index()

        sns = snapshot_weightings.index
        snapshot_weightings = snapshot_weightings.loc[sns]
        snapshot_weightings.to_csv(snakemake.output.snapshot_weightings)

    # Temporal segmentation
    elif isinstance(resolution, str) and "seg" in resolution.lower():
        segments = int(resolution[:-3])
        logger.info(f"Use temporal segmentation with {segments} segments")

        # Get all time-dependent data
        dfs = [
            pnl
            for c in n.components
            for attr, pnl in c.dynamic.items()
            if not pnl.empty and attr != "e_min_pu"
        ]
        if snakemake.input.hourly_heat_demand_total:
            dfs.append(
                xr.open_dataset(snakemake.input.hourly_heat_demand_total)
                .to_dataframe()
                .unstack(level=1)
            )
        if snakemake.input.solar_thermal_total:
            dfs.append(
                xr.open_dataset(snakemake.input.solar_thermal_total)
                .to_dataframe()
                .unstack(level=1)
            )
        df = pd.concat(dfs, axis=1)

        # Reset columns to flat index
        df = df.T.reset_index(drop=True).T

        # Normalise all time-dependent data
        annual_max = df.max().replace(0, 1)
        df = df.div(annual_max, level=0)

        # Get representative segments
        agg = tsam.TimeSeriesAggregation(
            df,
            hoursPerPeriod=len(df),
            noTypicalPeriods=1,
            noSegments=segments,
            segmentation=True,
            solver=snakemake.params.solver_name,
        )
        agg = agg.createTypicalPeriods()

        weightings = agg.index.get_level_values("Segment Duration")
        offsets = np.insert(np.cumsum(weightings[:-1]), 0, 0)
        snapshot_weightings = n.snapshot_weightings.loc[n.snapshots[offsets]].mul(
            weightings, axis=0
        )

        logger.info(
            f"Distribution of snapshot durations:\n{snapshot_weightings.objective.value_counts()}"
        )

        snapshot_weightings.to_csv(snakemake.output.snapshot_weightings)
