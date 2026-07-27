#!/usr/bin/env bash
# run_sweep.sh — fixed-capacity operations sweep, one snakemake driver after
# another. Order: PF, then rh336, then rh96; 2025 (hindcast) + 2035 (export).
# Each driver blocks until its SLURM jobs finish, then the next one starts, so
# you launch this once and don't touch it again.
#
# Usage:
#   pixi run bash run_sweep.sh -n     # DRY RUN: resolve the DAG, submit nothing
#   tmux new -s runs                  # (or: screen -S runs)
#   pixi run bash run_sweep.sh        # real run
#   Ctrl+b d   (tmux)  /  Ctrl+a d   (screen)   -> detach; job keeps running
#   tmux attach -t runs  /  screen -r runs      -> reattach later;  squeue --me
#
# ';' semantics (no `set -e`): a failed run does NOT block the following ones.

set -u
cd "$(dirname "$0")" || exit 1        # run from repo root regardless of cwd

DRY=""
if [ "${1:-}" = "-n" ]; then
  DRY="-n"
  echo ">>> DRY RUN — resolving the DAG only, nothing is submitted"
fi

SMK="snakemake --profile slurm --rerun-triggers mtime --keep-going $DRY"

run () {                               # $1 = config, remaining args = targets
  local cfg="$1"; shift
  echo ">>> $(date '+%F %T')  START  $cfg"
  $SMK --configfile "$cfg" "$@"
  echo ">>> $(date '+%F %T')  ENDE (exit $?)  $cfg"
  echo
}

# --- PF: 2025 hindcast (2035 already solved -> skipped if up to date) ---
run config/config_default4712SS_fixedcap_2035.yaml \
  results/default4712SS_fixedcap/KN2045_Mix/networks/base_s_27__none_2025_op.nc

# --- rh336 (~2-week window): 2025 hindcast + 2035 export ---
run config/config_default4712SS_fixedcap_2035_rh336.yaml \
  results/default4712SS_fixedcap_rh336/KN2045_Mix/networks/base_s_27__none_2025_op.nc \
  results/default4712SS_fixedcap_rh336/KN2045_Mix/networks/base_s_27__none_2035_op.nc

# --- rh96 (~4-day window): 2025 hindcast + 2035 export ---
run config/config_default4712SS_fixedcap_2035_rh96.yaml \
  results/default4712SS_fixedcap_rh96/KN2045_Mix/networks/base_s_27__none_2025_op.nc \
  results/default4712SS_fixedcap_rh96/KN2045_Mix/networks/base_s_27__none_2035_op.nc

echo ">>> ALLE LÄUFE FERTIG $(date '+%F %T')"
