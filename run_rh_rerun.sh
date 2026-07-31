#!/usr/bin/env bash
# run_rh_rerun.sh — re-run the two rolling-horizon operations runs (rh336, rh96)
# one after another, after the rolling-horizon fixes. Launch once, leave overnight.
#
# --forcerun is needed because --rerun-triggers mtime ignores code changes: the
# old *_op.nc are newer than their inputs and would be treated as up to date.
# (If you deleted the old *_op.nc, --forcerun is harmless but not required.)
#
# Usage:
#   pixi run bash run_rh_rerun.sh -n     # DRY RUN: resolve the DAG, submit nothing
#   tmux new -s rh                       # (or: screen -S rh)
#   pixi run bash run_rh_rerun.sh        # real run
#   Ctrl+b d   (tmux)  ->  detach; jobs keep running;  squeue --me

set -u
cd "$(dirname "$0")" || exit 1        # run from repo root regardless of cwd

DRY=""
if [ "${1:-}" = "-n" ]; then
  DRY="-n"
  echo ">>> DRY RUN - resolving the DAG only, nothing is submitted"
fi

SMK="snakemake --profile slurm --rerun-triggers mtime --keep-going $DRY"

run () {                               # $1 = config, remaining args = targets
  local cfg="$1"; shift
  echo ">>> $(date '+%F %T')  START  $cfg"
  # targets MUST come before --forcerun and --configfile: both take multiple
  # arguments and would otherwise swallow the target paths.
  $SMK "$@" --forcerun solve_operations_sector_network --configfile "$cfg"
  echo ">>> $(date '+%F %T')  ENDE (exit $?)  $cfg"
  echo
}

# --- rh336 (~2-week window): 2025 hindcast + 2035 export ---
run config/config_default4712SS_fixedcap_2035_rh336.yaml \
  results/default4712SS_fixedcap_rh336/KN2045_Mix/networks/base_s_27__none_2025_op.nc \
  results/default4712SS_fixedcap_rh336/KN2045_Mix/networks/base_s_27__none_2035_op.nc

# --- rh96 (~4-day window): 2025 hindcast + 2035 export ---
run config/config_default4712SS_fixedcap_2035_rh96.yaml \
  results/default4712SS_fixedcap_rh96/KN2045_Mix/networks/base_s_27__none_2025_op.nc \
  results/default4712SS_fixedcap_rh96/KN2045_Mix/networks/base_s_27__none_2035_op.nc

echo ">>> ALL RUNS DONE $(date '+%F %T')"
