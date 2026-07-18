# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT
#
# Fixed-capacity hourly dispatch ("operations") runs on solved sector-coupled
# networks from other pypsa-de runs. Purely additive: no rule of the regular
# capacity-expansion workflow is modified. Activated by setting
# `solve_operations.source_network` in a case config; see
# config/config.test02_fixedcap.yaml for an example.


def solve_operations_source_network(w):
    template = config_provider("solve_operations", "source_network", default="")(w)
    if not template:
        raise ValueError(
            "Set solve_operations.source_network in the config to the solved "
            "network of the source run, e.g. "
            '"results/<prefix>/<run>/networks/base_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}.nc"'
        )
    return template.format(**dict(w))


rule solve_operations_sector_network:
    input:
        source_network=solve_operations_source_network,
        donor_network=lambda w: (
            resources(
                "networks/base-extended_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}.nc"
            )
            if config_provider("sector", "district_heating", "subnodes", "enable")(w)
            else resources(
                "networks/base_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}.nc"
            )
        ),
    output:
        network=RESULTS
        + "networks/base_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}_op.nc",
        prices=RESULTS
        + "csvs/electricity_prices_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}_op.csv",
    log:
        solver=normpath(
            RESULTS
            + "logs/solve_operations_sector_network/base_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}_op_solver.log"
        ),
        memory=RESULTS
        + "logs/solve_operations_sector_network/base_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}_op_memory.log",
        python=RESULTS
        + "logs/solve_operations_sector_network/base_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}_op_python.log",
    benchmark:
        (
            RESULTS
            + "benchmarks/solve_operations_sector_network/base_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}"
        )
    shadow:
        shadow_config
    threads: solver_threads
    resources:
        mem_mb=config_provider("solving", "mem_mb"),
        runtime=config_provider("solving", "runtime", default="6h"),
    params:
        solve_operations=config_provider("solve_operations"),
        solving=config_provider("solving"),
        foresight=config_provider("foresight"),
        co2_sequestration_potential=config_provider(
            "sector", "co2_sequestration_potential", default=200
        ),
    message:
        "Solving fixed-capacity operations network for {wildcards.clusters} clusters, {wildcards.planning_horizons} planning horizon"
    script:
        scripts("solve_operations_sector_network.py")
