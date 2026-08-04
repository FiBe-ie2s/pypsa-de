# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT

from pydantic import Field

from scripts.lib.validation.config._base import ConfigModel


class _UnitCommitmentConfig(ConfigModel):
    """Configuration for `solve_operations.unit_commitment` settings."""

    enable: bool = Field(
        False,
        description="Make selected dispatchable links committable so the linearized unit commitment (solving.options.linearized_unit_commitment) applies minimum-load, start-up-cost and up/down-time constraints in the dispatch run.",
    )
    source: str = Field(
        "data/unit_commitment.csv",
        description="CSV file with per-technology unit-commitment parameters (columns e.g. OCGT, CCGT; rows p_min_pu, ramp limits, min_up_time, min_down_time, start_up_cost).",
    )
    carriers: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of a network link carrier to the parameter column of `source` to use for it, e.g. {'CCGT': 'CCGT', 'H2 CCGT': 'CCGT'}. Heat-driven CHP carriers should be omitted, as their commitment is set by heat demand, not the electricity market.",
    )
    countries: list[str] = Field(
        default_factory=list,
        description="Restrict unit commitment to links whose output bus is in these countries, e.g. ['DE']. Bounds problem size, since each committable unit adds status variables and up/down-time constraints over all snapshots. Empty applies it everywhere.",
    )


class SolveOperationsConfig(ConfigModel):
    """Configuration for top level `solve_operations` settings: fixed-capacity hourly dispatch runs on solved networks from other runs."""

    source_network: str = Field(
        "",
        description="Path template to the solved sector-coupled network whose optimised capacities are fixed for the dispatch run. May contain the wildcards {clusters}, {opts}, {sector_opts} and {planning_horizons}, e.g. 'results/<prefix>/<run>/networks/base_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}.nc'. Required for the solve_operations_sector_network rule.",
    )
    copperplate_zones: list[list[str]] = Field(
        default_factory=list,
        description="Groups of country codes whose AC buses are merged into a single market zone before the dispatch solve, e.g. [['DE']] for one German bidding zone. Intra-zone transmission is replaced by lossless links of infinite capacity; sector-coupling links and cross-border interconnectors are untouched.",
    )
    unfix_free_stores: bool = Field(
        True,
        description="Release stores with zero capital cost and unbounded e_nom_max (pure accounting buffers such as 'co2 atmosphere') from the capacity fixing again, so their degenerate optimised size does not act as an artificial bound in the dispatch run.",
    )
    co2_atmosphere_constraint: bool = Field(
        True,
        description="Single-pass runs only: re-apply GlobalConstraints of type 'co2_atmosphere' (EU CO2 limit and national CO2 budgets) via add_co2_atmosphere_constraint. GlobalConstraints of type 'operational_limit' are always re-applied natively by PyPSA. Rolling-horizon runs ignore this and use co2_price instead.",
    )
    co2_price: float | dict[int, float] | None = Field(
        None,
        description="CO2 price in EUR/tonne applied as a fixed emission cost in rolling-horizon dispatch runs (required there), where the annual co2_atmosphere budget cannot be imposed per window. Either a single value for every year, or a mapping of planning horizon to value (e.g. {2025: 0, 2035: 123.6, 2045: 133.9}). Take each from the constant CO2 shadow price of the single-pass run of that year.",
    )
    unit_commitment: _UnitCommitmentConfig = Field(
        default_factory=_UnitCommitmentConfig,
        description="Linearized unit commitment for dispatchable links in the operations run.",
    )
