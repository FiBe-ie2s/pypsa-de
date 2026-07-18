# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT
"""
Solves a linear optimal dispatch ("operations") problem at native hourly
resolution using the capacities of a previously solved sector-coupled
capacity-expansion network of arbitrary (possibly mixed) temporal resolution.

The solved source network only carries time series for its own, coarser
snapshot set. To obtain a continuous hourly dispatch problem, the source
network is expanded to the snapshots of an hourly "donor" network (the
unsolved ``prepare_sector_network`` output of the operations run, built with
``clustering.temporal.resolution_sector: false``):

1. All time-varying inputs of the source network are upsampled to the donor
   snapshots (block-constant forward fill).
2. Wherever a component can be matched to a donor component (exact name, or
   its build-year suffix stripped, e.g. "DE1 0 onwind-2030" -> "DE1 0 onwind"),
   the hourly donor series replaces the block-constant one. Mean-aggregated
   series are rescaled with the ratio of the source block value to the donor
   block mean, so that modifications applied after ``prepare_sector_network``
   in the source run (e.g. ``modify_prenetwork`` demand scaling) survive,
   while unmodified series reproduce the donor exactly.
3. All optimised capacities are fixed; pure accounting stores (zero capital
   cost, unbounded e_nom_max, e.g. "co2 atmosphere") can be released again.
4. Optionally, the AC buses of configured country groups are merged into
   single market zones ("copperplating", e.g. one German bidding zone).

Only ``GlobalConstraint`` types that PyPSA re-applies natively (e.g.
``operational_limit``) remain active in the dispatch run; ``co2_atmosphere``
constraints are optionally re-added via ``add_co2_atmosphere_constraint``.
"""

import logging

import numpy as np
import pandas as pd
import pypsa

logger = logging.getLogger(__name__)

# e_max_pu / e_min_pu are aggregated with min/max (not mean) in
# set_temporal_aggregation, so the block-mean ratio correction does not apply.
PLAIN_OVERWRITE_ATTRS = {"e_max_pu", "e_min_pu"}


def build_block_map(
    source_snapshots: pd.DatetimeIndex, dense_snapshots: pd.DatetimeIndex
) -> pd.Series:
    """
    Map each dense snapshot to the source snapshot (block label) covering it.

    Source snapshots are assumed to label the start of their aggregation
    block, which holds for all temporal aggregation modes in this workflow
    (uniform resampling, segmentation and the "custom" window mapping).

    Parameters
    ----------
    source_snapshots : pd.DatetimeIndex
        Snapshots of the (coarser) source network.
    dense_snapshots : pd.DatetimeIndex
        Snapshots of the dense (hourly) target grid.

    Returns
    -------
    pd.Series
        Indexed by ``dense_snapshots``, values are the covering source
        snapshots.
    """
    source_snapshots = pd.DatetimeIndex(source_snapshots)
    dense_snapshots = pd.DatetimeIndex(dense_snapshots)
    if dense_snapshots[0] < source_snapshots[0]:
        raise ValueError(
            f"Dense snapshots start ({dense_snapshots[0]}) before the first "
            f"source snapshot ({source_snapshots[0]}); cannot forward-fill."
        )
    labels = pd.Series(source_snapshots, index=source_snapshots)
    return labels.reindex(dense_snapshots, method="ffill")


def upsample_to_dense(n: pypsa.Network, dense_snapshots: pd.DatetimeIndex) -> pd.Series:
    """
    Re-index the network to dense snapshots with block-constant time series.

    All time-varying input attributes are forward-filled from their block
    labels; snapshot weightings are reset to 1.

    Returns
    -------
    pd.Series
        The block map from :func:`build_block_map` for later use.
    """
    block_map = build_block_map(n.snapshots, dense_snapshots)

    stash = []
    for c in n.components:
        if c.static.empty:
            continue
        attrs = n.component_attrs[c.name]
        series_attrs = attrs.index[
            attrs.type.str.contains("series") & attrs.status.str.contains("Input")
        ]
        for attr in series_attrs:
            df = c.dynamic[attr]
            if df.empty:
                continue
            stash.append((c.name, attr, df.reindex(dense_snapshots, method="ffill")))

    n.set_snapshots(dense_snapshots)
    components = {c.name: c for c in n.components}
    for c_name, attr, df in stash:
        components[c_name].dynamic[attr] = df
    n.snapshot_weightings.loc[:, :] = 1.0

    logger.info(
        f"Upsampled network from {len(block_map.unique())} to "
        f"{len(dense_snapshots)} snapshots (weightings reset to 1)."
    )
    return block_map


def map_target_to_donor(
    target_names: pd.Index, build_years: pd.Series, donor_names: pd.Index
) -> dict[str, str]:
    """
    Match component names of the target network to donor component names.

    Exact matches win; otherwise the build-year suffix that the myopic
    workflow appends to assets (e.g. "-2030") is stripped, but only when the
    component's ``build_year`` attribute confirms the suffix, so names that
    merely end in a number are not mangled.
    """
    donor_set = set(donor_names)
    mapping = {}
    for name in target_names:
        if name in donor_set:
            mapping[name] = name
            continue
        build_year = build_years.get(name, 0)
        try:
            build_year = int(build_year)
        except (TypeError, ValueError):
            continue
        if build_year > 0:
            suffix = f"-{build_year}"
            if name.endswith(suffix):
                base = name[: -len(suffix)]
                if base in donor_set:
                    mapping[name] = base
    return mapping


def overwrite_dynamic_from_donor(
    n: pypsa.Network,
    donor: pypsa.Network,
    block_map: pd.Series,
    eps: float = 1e-7,
) -> pd.DataFrame:
    """
    Replace block-constant time series with hourly donor series.

    For mean-aggregated attributes the donor series is rescaled by the ratio
    of the target block value to the donor block mean: unmodified series
    reproduce the donor exactly (ratio 1), while post-aggregation
    modifications from the source run (e.g. demand scaling) are preserved.
    Blocks where the donor mean is ~0 keep the target's block value.
    Min/max-aggregated attributes (e_max_pu, e_min_pu) are copied directly.

    Returns
    -------
    pd.DataFrame
        Summary of overwritten and unmatched (kept block-constant) columns
        per component and attribute.
    """
    donor_components = {c.name: c for c in donor.components}
    summary = []

    for c in n.components:
        if c.static.empty or c.name not in donor_components:
            continue
        donor_c = donor_components[c.name]
        attrs = n.component_attrs[c.name]
        series_attrs = attrs.index[
            attrs.type.str.contains("series") & attrs.status.str.contains("Input")
        ]
        if "build_year" in c.static.columns:
            build_years = c.static["build_year"]
        else:
            build_years = pd.Series(0, index=c.static.index)

        for attr in series_attrs:
            target_df = c.dynamic[attr]
            donor_df = donor_c.dynamic[attr]
            if target_df.empty or donor_df.empty:
                continue

            mapping = map_target_to_donor(
                target_df.columns, build_years, donor_df.columns
            )
            unmatched = target_df.columns.difference(mapping.keys())
            if not mapping:
                continue

            t_cols = list(mapping.keys())
            d_cols = [mapping[t] for t in t_cols]
            donor_h = donor_df[d_cols].set_axis(t_cols, axis=1)

            if attr in PLAIN_OVERWRITE_ATTRS:
                target_df[t_cols] = donor_h
            else:
                donor_block_means = donor_h.groupby(block_map).mean()
                donor_b = donor_block_means.reindex(block_map.values).set_axis(
                    target_df.index, axis=0
                )
                target_b = target_df[t_cols]
                new = donor_h * (target_b / donor_b)
                new = new.where(donor_b.abs() > eps, target_b)
                target_df[t_cols] = new

            summary.append(
                {
                    "component": c.name,
                    "attribute": attr,
                    "overwritten": len(t_cols),
                    "kept_block_constant": len(unmatched),
                }
            )
            if len(unmatched):
                logger.info(
                    f"{c.name}.{attr}: {len(unmatched)} column(s) without donor "
                    f"match stay block-constant, e.g. {list(unmatched[:5])}"
                )

    return pd.DataFrame(summary)


def fix_all_capacities(n: pypsa.Network) -> None:
    """Fix all optimised capacities and verify nothing stays extendable."""
    n.optimize.fix_optimal_capacities()
    still_extendable = {}
    for c_name, attr in [
        ("Generator", "p_nom"),
        ("Link", "p_nom"),
        ("Store", "e_nom"),
        ("StorageUnit", "p_nom"),
        ("Line", "s_nom"),
    ]:
        ext = n.static(c_name)[f"{attr}_extendable"]
        if ext.any():
            still_extendable[c_name] = int(ext.sum())
    if still_extendable:
        raise RuntimeError(
            f"Components remain extendable after fixing capacities: {still_extendable}"
        )
    logger.info("Fixed all optimised capacities (nothing extendable).")


def unfix_free_stores(n: pypsa.Network) -> pd.Index:
    """
    Release accounting stores (zero capital cost, unbounded e_nom_max) again.

    In the source run these buffers (e.g. "co2 atmosphere") were effectively
    unconstrained in size; keeping them fixed at their degenerate e_nom_opt
    would add an artificial bound to the dispatch problem.
    """
    free = n.stores.index[
        (n.stores.capital_cost == 0) & np.isinf(n.stores.e_nom_max)
    ]
    n.stores.loc[free, "e_nom_extendable"] = True
    if len(free):
        logger.info(
            f"Released {len(free)} accounting store(s) from fixed capacities: "
            f"{list(free[:10])}"
        )
    return free


def apply_copperplate(n: pypsa.Network, zones: list[list[str]]) -> list[list[str]]:
    """
    Merge the AC buses of each country group into one market zone.

    Reuses :func:`scripts.cluster_network.copperplate_buses`: branches between
    buses of the same zone are removed and replaced by lossless links of
    infinite capacity. Sector-coupling links and cross-border interconnectors
    keep one endpoint outside the zone and are untouched.

    Returns
    -------
    list[list[str]]
        The bus groups that were copperplated.
    """
    from scripts.cluster_network import copperplate_buses

    groups = []
    for group in zones:
        buses = n.buses.index[
            (n.buses.carrier == "AC") & n.buses.country.isin(group)
        ]
        if len(buses) < 2:
            logger.warning(
                f"Copperplate group {group} has fewer than two AC buses; skipping."
            )
            continue
        groups.append(list(buses))

    if not groups:
        return []

    if "copper" not in n.carriers.index:
        n.add("Carrier", "copper")

    for grp in groups:
        members = set(grp)
        for c_name in ("Line", "Link"):
            df = n.static(c_name)
            if df.empty:
                continue
            removed = df.index[df.bus0.isin(members) & df.bus1.isin(members)]
            if len(removed):
                logger.info(
                    f"Copperplate zone {sorted(set(n.buses.loc[grp, 'country']))}: "
                    f"removing {len(removed)} intra-zone {c_name}(s): "
                    f"{list(removed[:10])}"
                )

    copperplate_buses(n, groups)
    return groups


def export_electricity_prices(n: pypsa.Network, path: str) -> None:
    """Write hourly marginal prices of all AC buses plus per-country means."""
    ac = n.buses.index[n.buses.carrier == "AC"]
    prices = n.buses_t.marginal_price[ac].copy()
    for country in sorted(n.buses.loc[ac, "country"].unique()):
        country_buses = ac[n.buses.loc[ac, "country"] == country]
        prices[f"price_{country}"] = (
            n.buses_t.marginal_price[country_buses].mean(axis=1)
        )
    prices.to_csv(path)


if __name__ == "__main__":
    if "snakemake" not in globals():
        from scripts._helpers import mock_snakemake

        snakemake = mock_snakemake(
            "solve_operations_sector_network",
            configfiles="config/config.test02_fixedcap.yaml",
            opts="",
            clusters="27",
            sector_opts="none",
            planning_horizons="2035",
        )

    from scripts._benchmark import memory_logger
    from scripts._helpers import (
        configure_logging,
        set_scenario_config,
        update_config_from_wildcards,
    )
    from scripts.solve_network import (
        add_co2_atmosphere_constraint,
        collect_kwargs,
        prepare_network,
    )

    configure_logging(snakemake)
    set_scenario_config(snakemake)
    update_config_from_wildcards(snakemake.config, snakemake.wildcards)

    options = snakemake.params.solve_operations
    cf_solving = snakemake.params.solving["options"]
    planning_horizons = snakemake.wildcards.get("planning_horizons", None)

    np.random.seed(cf_solving.get("seed", 123))

    logger.info(f"Source network (capacities): {snakemake.input.source_network}")
    logger.info(f"Donor network (hourly time series): {snakemake.input.donor_network}")

    n = pypsa.Network(snakemake.input.source_network)
    donor = pypsa.Network(snakemake.input.donor_network)

    if not (donor.snapshot_weightings.objective == 1.0).all():
        logger.warning(
            "Donor network has non-unit snapshot weightings; expected a "
            "native-hourly network (clustering.temporal.resolution_sector: false)."
        )

    block_map = upsample_to_dense(n, donor.snapshots)
    summary = overwrite_dynamic_from_donor(n, donor, block_map)
    if not summary.empty:
        logger.info(f"Donor overwrite summary:\n{summary.to_string(index=False)}")

    fix_all_capacities(n)
    if options.get("unfix_free_stores", True):
        unfix_free_stores(n)

    apply_copperplate(n, options.get("copperplate_zones", []))

    # prepare_network re-adds this constraint from config
    readd = n.global_constraints.index.intersection(["co2_sequestration_limit"])
    if len(readd):
        n.remove("GlobalConstraint", readd)

    rolling_horizon = cf_solving.get("rolling_horizon", False)
    prepare_network(
        n,
        solve_opts=cf_solving,
        foresight=snakemake.params.foresight,
        planning_horizons=planning_horizons,
        co2_sequestration_potential=snakemake.params["co2_sequestration_potential"],
        limit_max_growth=snakemake.params.get("sector", {}).get("limit_max_growth"),
        rolling_horizon=rolling_horizon,
    )

    extra_functionality = (
        add_co2_atmosphere_constraint
        if options.get("co2_atmosphere_constraint", True)
        else None
    )

    logging_frequency = snakemake.config.get("solving", {}).get(
        "mem_logging_frequency", 30
    )

    with memory_logger(
        filename=getattr(snakemake.log, "memory", None), interval=logging_frequency
    ) as mem:
        if rolling_horizon:
            logger.info("Solving operations network with rolling horizon...")
            all_kwargs, _ = collect_kwargs(
                snakemake.config,
                snakemake.params.solving,
                planning_horizons,
                log_fn=snakemake.log.solver,
                mode="rolling_horizon",
            )
            if extra_functionality is not None:
                all_kwargs["extra_functionality"] = extra_functionality
            n.optimize.optimize_with_rolling_horizon(**all_kwargs)
            status, condition = "", ""
        else:
            logger.info("Solving operations network in a single pass...")
            model_kwargs, solve_kwargs = collect_kwargs(
                snakemake.config,
                snakemake.params.solving,
                planning_horizons,
                log_fn=snakemake.log.solver,
                mode="single",
            )
            n.optimize.create_model(**model_kwargs)
            if extra_functionality is not None:
                extra_functionality(n, n.snapshots)
            status, condition = n.optimize.solve_model(**solve_kwargs)

    logger.info(f"Maximum memory usage: {mem.mem_usage}")

    if not rolling_horizon and status != "ok":
        logger.warning(
            f"Solving status '{status}' with termination condition '{condition}'"
        )
    if "infeasible" in condition:
        raise RuntimeError("Solving status 'infeasible'.")

    n.meta = dict(snakemake.config, **dict(wildcards=dict(snakemake.wildcards)))
    n.export_to_netcdf(snakemake.output.network)
    export_electricity_prices(n, snakemake.output.prices)
