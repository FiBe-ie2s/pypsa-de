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

Both kinds of constraint carry *annual* budgets, while a rolling-horizon solve
rebuilds every constraint once per window. Without correction each window would
receive the full-year budget (see ``prorate_operational_limits`` and
``rolling_horizon_co2_constraint``).
"""

import contextlib
import logging

import numpy as np
import pandas as pd
import pypsa

logger = logging.getLogger(__name__)

# e_max_pu / e_min_pu are aggregated with min/max (not mean) in
# set_temporal_aggregation, so the block-mean ratio correction does not apply.
PLAIN_OVERWRITE_ATTRS = {"e_max_pu", "e_min_pu"}

# Bus carriers of pure accounting buffers (GHG / materials balances) that are
# meant to be unbounded and may safely be released from the capacity fixing.
# Energy-carrying stores (battery, EV battery, H2, heat, ...) must NEVER be
# released: with zero capital cost and unbounded e_nom_max they would become
# free, unlimited storage and flatten the dispatch (and prices) artificially.
ACCOUNTING_BUS_CARRIERS = {
    "co2",
    "co2 atmosphere",
    "co2 stored",
    "co2 sequestered",
    "non-sequestered HVC",
}


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
    Release pure accounting stores (e.g. "co2 atmosphere") from the fixing.

    In the source run these GHG/materials balance buffers were effectively
    unconstrained in size; keeping them fixed at their degenerate e_nom_opt
    could add an artificial bound to the dispatch problem.

    Only stores that both look unbounded (zero capital cost, infinite
    e_nom_max) *and* sit on an accounting bus (see ``ACCOUNTING_BUS_CARRIERS``)
    are released. This deliberately excludes energy-carrying stores such as EV
    batteries, which are also zero-cost with infinite e_nom_max but would
    otherwise become free, unlimited grid storage that flattens the dispatch.
    """
    bus_carrier = n.stores.bus.map(n.buses.carrier)
    free = n.stores.index[
        (n.stores.capital_cost == 0)
        & np.isinf(n.stores.e_nom_max)
        & bus_carrier.isin(ACCOUNTING_BUS_CARRIERS)
    ]
    n.stores.loc[free, "e_nom_extendable"] = True
    if len(free):
        logger.info(
            f"Released {len(free)} accounting store(s) from fixed capacities: "
            f"{list(free[:10])}"
        )

    # Guard: any energy store left extendable here would act as free storage.
    still_ext = n.stores.index[n.stores.e_nom_extendable & ~n.stores.index.isin(free)]
    if len(still_ext):
        raise RuntimeError(
            f"{len(still_ext)} store(s) remain extendable after fixing and are "
            f"not accounting buffers, e.g. {list(still_ext[:10])}. These would "
            "act as free unlimited storage."
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
        buses = n.buses.index[(n.buses.carrier == "AC") & n.buses.country.isin(group)]
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


def capture_final_state_of_charge(n: pypsa.Network) -> dict[str, pd.Series]:
    """
    Read the solved end-of-horizon storage levels of the source network.

    Must be called on the freshly loaded source network, before
    :func:`upsample_to_dense` re-indexes the dynamic data (which keeps only
    *input* series, not solved results like ``stores_t.e``).

    Capacity-expansion runs solve storage cyclically, so the level after the
    last snapshot equals the level before the first one — i.e. these values are
    exactly the physically consistent start-of-year filling levels.
    """
    levels = {}
    if not n.stores_t.e.empty:
        levels["Store"] = n.stores_t.e.iloc[-1].copy()
    if not n.storage_units_t.state_of_charge.empty:
        levels["StorageUnit"] = n.storage_units_t.state_of_charge.iloc[-1].copy()

    logger.info(
        "Captured source storage levels: "
        + ", ".join(f"{len(v)} {k}(s)" for k, v in levels.items())
        if levels
        else "Source network carries no solved storage levels."
    )
    return levels


def seed_initial_state_of_charge(
    n: pypsa.Network, levels: dict[str, pd.Series]
) -> pd.DataFrame:
    """
    Start a rolling-horizon run at the source run's storage levels.

    ``prepare_network(rolling_horizon=True)`` switches cyclic storage off and
    sets every initial level to zero, so the dispatch year starts with all
    storage empty. For a full-year operations run that silently removes seasonal
    storage: it can only ever fill up, never draw down a winter stock.

    Pure accounting buffers (``ACCOUNTING_BUS_CARRIERS``, e.g. "co2 atmosphere")
    are deliberately left at zero — their level *is* the cumulative emission
    balance of the dispatch year and must not inherit the source run's total.

    Returns
    -------
    pd.DataFrame
        Per component: seeded, skipped (accounting) and unmatched counts.
    """
    summary = []
    specs = [
        ("Store", n.stores, "e_initial", n.stores.bus.map(n.buses.carrier)),
        (
            "StorageUnit",
            n.storage_units,
            "state_of_charge_initial",
            n.storage_units.bus.map(n.buses.carrier),
        ),
    ]

    for c_name, static, attr, bus_carrier in specs:
        source_levels = levels.get(c_name)
        if source_levels is None or static.empty:
            continue

        accounting = static.index[bus_carrier.isin(ACCOUNTING_BUS_CARRIERS)]
        candidates = static.index.difference(accounting)
        matched = candidates.intersection(source_levels.index)

        static.loc[matched, attr] = source_levels.loc[matched]

        summary.append(
            {
                "component": c_name,
                "seeded": len(matched),
                "accounting_kept_at_zero": len(accounting),
                "unmatched": len(candidates.difference(matched)),
            }
        )

        unmatched = candidates.difference(matched)
        if len(unmatched):
            logger.warning(
                f"{c_name}: {len(unmatched)} component(s) without a source level "
                f"start empty, e.g. {list(unmatched[:5])}"
            )

    if summary:
        logger.info(
            "Seeded rolling-horizon storage levels from the source network:\n"
            + pd.DataFrame(summary).to_string(index=False)
        )
    return pd.DataFrame(summary)


def prorate_operational_limits(n: pypsa.Network, horizon: int) -> pd.Series:
    """
    Scale annual ``operational_limit`` budgets down to a single rolling window.

    ``optimize_with_rolling_horizon`` rebuilds the optimisation model for every
    window, and PyPSA re-creates each ``operational_limit`` constraint from the
    unchanged ``constant`` — i.e. the *annual* budget is imposed on every single
    window. For ``sense: "<="`` that only makes the limit far too lax, but for
    ``sense: "=="`` (e.g. "unsustainable biomass limit") the annual quantity is
    *enforced* in every window, inflating the yearly total by roughly
    ``len(snapshots) / horizon``.

    Every hour of the year ends up carrying one window's worth of the budget
    (overlapping hours are re-solved and overwritten, not accumulated), so the
    annual total is restored by scaling with the window's share of the year —
    independently of ``overlap``.

    Returns
    -------
    pd.Series
        The original constants, indexed by constraint name, so the caller can
        restore them if needed.
    """
    idx = n.global_constraints.index[n.global_constraints.type == "operational_limit"]
    original = n.global_constraints.loc[idx, "constant"].copy()

    if idx.empty:
        logger.info("No operational_limit constraints to scale for rolling horizon.")
        return original

    share = min(horizon / len(n.snapshots), 1.0)
    if share == 1.0:
        logger.info(
            f"Rolling-horizon window ({horizon}) covers all {len(n.snapshots)} "
            "snapshots; operational_limit constants left unchanged."
        )
        return original

    n.global_constraints.loc[idx, "constant"] = original * share
    logger.info(
        f"Scaled {len(idx)} operational_limit constraint(s) by {share:.6f} "
        f"({horizon}h window / {len(n.snapshots)} snapshots) for rolling horizon: "
        f"{list(idx)}"
    )
    return original


@contextlib.contextmanager
def _co2_budget_prorated(n: pypsa.Network, snapshots: pd.Index):
    """
    Temporarily shrink ``co2_atmosphere`` budgets to the elapsed share of year.

    The ``co2_atmosphere`` constraint bounds the *cumulative* CO2 store level at
    the last snapshot of the window, and ``optimize_with_rolling_horizon``
    carries ``e_initial`` from window to window. The per-window right-hand side
    must therefore be a growing path (budget x share of the year elapsed), not
    the annual budget (never binding until the very end) and not the annual
    budget divided by the number of windows (violated from the second window on,
    because the store already starts above it).
    """
    idx = n.global_constraints.index[n.global_constraints.type == "co2_atmosphere"]
    original = n.global_constraints.loc[idx, "constant"].copy()

    if idx.empty:
        yield
        return

    position = n.snapshots.get_indexer([snapshots[-1]])[0]
    if position < 0:
        raise ValueError(
            f"Rolling-horizon window ends at {snapshots[-1]}, which is not part "
            "of n.snapshots; cannot determine the elapsed share of the year."
        )
    elapsed = (position + 1) / len(n.snapshots)

    n.global_constraints.loc[idx, "constant"] = original * elapsed
    logger.info(
        f"co2_atmosphere budget for window ending {snapshots[-1]}: "
        f"{elapsed:.4f} of the annual budget."
    )
    try:
        yield
    finally:
        n.global_constraints.loc[idx, "constant"] = original


def rolling_horizon_co2_constraint(base_constraint):
    """
    Wrap ``add_co2_atmosphere_constraint`` so the budget grows with the year.

    The wrapped function keeps the signature ``(n, snapshots)`` expected by
    ``extra_functionality``. Scaling ``constant`` around the unmodified upstream
    call (instead of reimplementing it) keeps the store selection logic in a
    single place.
    """

    def extra_functionality(n: pypsa.Network, snapshots: pd.Index) -> None:
        with _co2_budget_prorated(n, snapshots):
            base_constraint(n, snapshots)

    return extra_functionality


class _FailedWindowWatcher(logging.Handler):
    """
    Collect PyPSA's per-window "Optimization failed" warnings.

    ``optimize_with_rolling_horizon`` only logs failed windows and keeps going,
    so without this the results of a partially failed run would be exported as
    if they were valid.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.failures: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "Optimization failed" in message:
            self.failures.append(message)


@contextlib.contextmanager
def watch_rolling_horizon_windows():
    """Raise afterwards if any rolling-horizon window failed to solve."""
    watcher = _FailedWindowWatcher()
    pypsa_logger = logging.getLogger("pypsa")
    pypsa_logger.addHandler(watcher)
    try:
        yield watcher
    finally:
        pypsa_logger.removeHandler(watcher)

    if watcher.failures:
        raise RuntimeError(
            f"{len(watcher.failures)} rolling-horizon window(s) failed to solve; "
            f"the dispatch and price series are incomplete. First failure: "
            f"{watcher.failures[0]}"
        )


def export_electricity_prices(n: pypsa.Network, path: str) -> None:
    """Write hourly marginal prices of all AC buses plus per-country means."""
    ac = n.buses.index[n.buses.carrier == "AC"]
    prices = n.buses_t.marginal_price[ac].copy()
    for country in sorted(n.buses.loc[ac, "country"].unique()):
        country_buses = ac[n.buses.loc[ac, "country"] == country]
        prices[f"price_{country}"] = n.buses_t.marginal_price[country_buses].mean(
            axis=1
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

    # must be read before upsampling drops the solved (non-input) series
    source_storage_levels = capture_final_state_of_charge(n)

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

    if rolling_horizon:
        # prepare_network() has just emptied all storage; start the dispatch
        # year at the source run's (cyclic) filling levels instead.
        seed_initial_state_of_charge(n, source_storage_levels)

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
            # Annual budgets would otherwise be imposed once per window.
            annual_limits = prorate_operational_limits(n, all_kwargs["horizon"])
            if extra_functionality is not None:
                all_kwargs["extra_functionality"] = rolling_horizon_co2_constraint(
                    extra_functionality
                )
            try:
                with watch_rolling_horizon_windows():
                    n.optimize.optimize_with_rolling_horizon(**all_kwargs)
            finally:
                # Export the network with the annual budgets it was built with,
                # not the per-window values used internally.
                n.global_constraints.loc[annual_limits.index, "constant"] = (
                    annual_limits
                )
            status, condition = "ok", ""
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
