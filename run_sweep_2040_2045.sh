#!/usr/bin/env bash
# run_sweep_2040_2045.sh — extend default4712SS to 2040 & 2045.
#
# Two phases, one snakemake driver after another. Each driver BLOCKS until its
# SLURM jobs finish, so phase 2 only starts once the source networks of phase 1
# exist. Launch once and don't touch it again.
#
#   Phase 1  EXPANSION  (config.default4712SS.yaml)
#            solves the myopic chain 2040 (from existing 2035) then 2045.
#            Produces the source networks for the copperplate runs.
#   Phase 2  COPPERPLATE (config_default4712SS_fixedcap_2040_2045.yaml)
#            fixed-capacity 1H dispatch on those source networks -> _op.nc + prices.
#
# Usage:
#   pixi run bash run_sweep_2040_2045.sh -n     # DRY RUN: resolve the DAG, submit nothing
#   tmux new -s sweep4045                        # (or: screen -S sweep4045)
#   pixi run bash run_sweep_2040_2045.sh         # real run
#   Ctrl+b d   (tmux)  /  Ctrl+a d   (screen)    -> detach; keeps running
#   tmux attach -t sweep4045  /  screen -r sweep4045   -> reattach;  squeue --me
#
# ';' semantics (no `set -e`): a failed run does NOT block the following ones.

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
  # targets MUST come before --configfile: snakemake's --configfile is greedy
  # (nargs='+') and would otherwise swallow the target paths as config files.
  $SMK "$@" --configfile "$cfg"
  echo ">>> $(date '+%F %T')  ENDE (exit $?)  $cfg"
  echo
}

# --- Phase 1: EXPANSION 2040 + 2045 (myopic; builds on the existing 2035) ---
run config/config.default4712SS.yaml \
  results/default4712SS/KN2045_Mix/networks/base_s_27__none_2040.nc \
  results/default4712SS/KN2045_Mix/networks/base_s_27__none_2045.nc

# --- Phase 2: COPPERPLATE 2040 + 2045 (fixed-capacity 1H dispatch) ---
run config/config_default4712SS_fixedcap_2040_2045.yaml \
  results/default4712SS_fixedcap/KN2045_Mix/networks/base_s_27__none_2040_op.nc \
  results/default4712SS_fixedcap/KN2045_Mix/networks/base_s_27__none_2045_op.nc

echo ">>> ALL RUNS DONE $(date '+%F %T')"
