#!/usr/bin/env bash
# run_rh_rerun.sh — re-run the two rolling-horizon operations runs (rh96, rh336)
# CONCURRENTLY, after the rolling-horizon fixes:
#   * annual `operational_limit` budgets pro-rated per window
#   * CO2 budget as a growing path instead of the annual budget per window
#   * storage seeded from the source run instead of starting empty
#   * cyclic_state_of_charge typo in prepare_network()
#
# The perfect-foresight run is NOT re-run: every fix sits behind
# `if rolling_horizon:`, so results/default4712SS_fixedcap stays bit-identical.
#
# Two things this script does that a plain snakemake call would not:
#
#   1. --forcerun solve_operations_sector_network
#      We keep --rerun-triggers mtime (as run_sweep.sh does), which deliberately
#      ignores code changes. The existing *_op.nc files are newer than their
#      inputs, so without forcing, snakemake reports "Nothing to be done".
#      Forcing this rule re-runs only the solve step; the resource chain and the
#      donor networks stay cached.
#
#   2. --nolock, guarded by a pre-flight dry run
#      Snakemake locks the working directory, so two drivers in the same repo
#      refuse to start. With `run.shared_resources.policy: false` each run owns
#      its resources/<prefix>, logs/<prefix>, benchmarks/<prefix> and
#      results/<prefix> tree, so there is nothing to race over — PROVIDED no
#      upstream resource still needs building. The pre-flight dry run verifies
#      exactly that and refuses to go parallel otherwise.
#
# Usage:
#   pixi run bash run_rh_rerun.sh -n          # dry run only (sequential, locked)
#   pixi run bash run_rh_rerun.sh --archive   # move old results aside, then run
#   tmux new -s rh                            # (or: screen -S rh)
#   pixi run bash run_rh_rerun.sh             # real run, both in parallel
#   Ctrl+b d   (tmux)  /  Ctrl+a d   (screen)   -> detach; jobs keep running
#   tmux attach -t rh  /  screen -r rh          -> reattach;   squeue --me
#
# Follow along:
#   tail -f logs/rerun_rh96_*.log logs/rerun_rh336_*.log
#
# NOTE: without --archive the existing results/<prefix> trees are overwritten.

set -u
cd "$(dirname "$0")" || exit 1        # run from repo root regardless of cwd

FORCE_RULE="solve_operations_sector_network"

CFG_RH96="config/config_default4712SS_fixedcap_2035_rh96.yaml"
CFG_RH336="config/config_default4712SS_fixedcap_2035_rh336.yaml"

TARGETS_RH96=(
  results/default4712SS_fixedcap_rh96/KN2045_Mix/networks/base_s_27__none_2025_op.nc
  results/default4712SS_fixedcap_rh96/KN2045_Mix/networks/base_s_27__none_2035_op.nc
)
TARGETS_RH336=(
  results/default4712SS_fixedcap_rh336/KN2045_Mix/networks/base_s_27__none_2025_op.nc
  results/default4712SS_fixedcap_rh336/KN2045_Mix/networks/base_s_27__none_2035_op.nc
)

SMK="snakemake --profile slurm --rerun-triggers mtime --keep-going"

DRY=0
ARCHIVE=0
FORCE_PARALLEL=0
for arg in "$@"; do
  case "$arg" in
    -n|--dry-run)        DRY=1 ;;
    --archive)           ARCHIVE=1 ;;
    --force-parallel)    FORCE_PARALLEL=1 ;;   # skip the pre-flight veto
    *) echo "unknown option: $arg"; exit 2 ;;
  esac
done

# --- pre-flight: dry run, sequential and locked, one config at a time --------
# Returns 0 if only $FORCE_RULE is scheduled, 2 if other rules would run too.
preflight () {
  local cfg="$1"; shift
  local out rc extra
  echo ">>> pre-flight  $cfg"
  out="$($SMK -n --forcerun "$FORCE_RULE" "$@" --configfile "$cfg" 2>&1)"
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "$out" | tail -40
    echo ">>> pre-flight FAILED for $cfg (exit $rc)"
    return 1
  fi

  echo "$out" | sed -n '/^Job stats:/,/^total/p'

  extra="$(echo "$out" | awk -v rule="$FORCE_RULE" '
      /^Job stats:/ {inblock=1; next}
      inblock && $1 == "total" {inblock=0}
      inblock && NF == 2 && $2 ~ /^[0-9]+$/ && $1 != "job" && $1 != rule {print $1}
  ')"

  if [ -n "$extra" ]; then
    echo ">>> $cfg would ALSO run: $(echo "$extra" | tr '\n' ' ')"
    return 2
  fi
  echo ">>> pre-flight OK: only $FORCE_RULE is scheduled"
  return 0
}

echo "=== pre-flight checks $(date '+%F %T') ==="
preflight "$CFG_RH96"  "${TARGETS_RH96[@]}";  PF96=$?
echo
preflight "$CFG_RH336" "${TARGETS_RH336[@]}"; PF336=$?
echo

if [ $DRY -eq 1 ]; then
  echo ">>> DRY RUN only — nothing submitted (rh96 exit $PF96, rh336 exit $PF336)"
  exit 0
fi

if [ $PF96 -eq 1 ] || [ $PF336 -eq 1 ]; then
  echo ">>> ABORT: a pre-flight dry run failed. Fix that before submitting."
  exit 1
fi

if { [ $PF96 -eq 2 ] || [ $PF336 -eq 2 ]; } && [ $FORCE_PARALLEL -eq 0 ]; then
  cat <<'EOF'
>>> ABORT: upstream resources still have to be built.

Running two drivers in parallel with --nolock is only safe once every rule other
than the solve step is already cached, otherwise both may build the same file.
Build the missing resources sequentially first, e.g.

    pixi run bash run_sweep.sh -n     # inspect what is missing
    pixi run bash run_sweep.sh        # sequential, safe

or re-run this script with --force-parallel if you are sure the pending rules
write into disjoint per-run trees.
EOF
  exit 1
fi

# --- optional: keep the pre-fix results for comparison ----------------------
if [ $ARCHIVE -eq 1 ]; then
  STAMP="$(date +%Y%m%d)"
  for prefix in default4712SS_fixedcap_rh96 default4712SS_fixedcap_rh336; do
    if [ -d "results/$prefix" ]; then
      dest="results/${prefix}_pre_rhfix_${STAMP}"
      echo ">>> archiving results/$prefix -> $dest"
      mv "results/$prefix" "$dest" || exit 1
    fi
  done
  echo
fi

# --- launch both drivers in parallel ----------------------------------------
mkdir -p logs
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG96="logs/rerun_rh96_${STAMP}.log"
LOG336="logs/rerun_rh336_${STAMP}.log"

echo "=== launching $(date '+%F %T') ==="
echo ">>> rh96  -> $LOG96"
$SMK --nolock --forcerun "$FORCE_RULE" "${TARGETS_RH96[@]}" \
  --configfile "$CFG_RH96" > "$LOG96" 2>&1 &
PID96=$!

sleep 15   # stagger DAG resolution and .snakemake/log timestamps

echo ">>> rh336 -> $LOG336"
$SMK --nolock --forcerun "$FORCE_RULE" "${TARGETS_RH336[@]}" \
  --configfile "$CFG_RH336" > "$LOG336" 2>&1 &
PID336=$!

echo ">>> both drivers running (pids $PID96, $PID336); waiting..."
echo

wait $PID96;  RC96=$?
echo ">>> $(date '+%F %T')  rh96  finished (exit $RC96)"
wait $PID336; RC336=$?
echo ">>> $(date '+%F %T')  rh336 finished (exit $RC336)"
echo

if [ $RC96 -ne 0 ]; then echo "!!! rh96 failed  — see $LOG96";  fi
if [ $RC336 -ne 0 ]; then echo "!!! rh336 failed — see $LOG336"; fi

cat <<'EOF'

=== verify before trusting the price series ===================================
Expected after the fix (values are the 2035 rh96 run before the fix -> target):

  unsustainable biomass, realised / annual limit   91.75      -> ~1.0
  co2 atmosphere, level at year end                138.3 Mt   -> ~950.9 Mt
  CO2 shadow price (bus 'co2 atmosphere')          0 EUR/t    -> ~123.6 EUR/t
  load sinks, total dispatch                       29.9 PWh   -> O(100) MWh
  objective value                                  3.56e13    -> ~1.7e11
  H2 store, start / annual maximum                 0.1% / 10.4% -> ~93.8% / seasonal

A window that fails to solve now raises instead of being silently exported,
so a clean exit code means every window solved.
EOF

echo ">>> ALL DONE $(date '+%F %T')"
