# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT

from pydantic import Field

from scripts.lib.validation.config._base import ConfigModel


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
        description="Re-apply GlobalConstraints of type 'co2_atmosphere' (EU CO2 limit and national CO2 budgets) in the dispatch run via add_co2_atmosphere_constraint. GlobalConstraints of type 'operational_limit' are always re-applied natively by PyPSA.",
    )
