#!/bin/bash
#SBATCH --job-name=fearless-rank
#SBATCH --partition=rtx
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=48G
#SBATCH --time=01:00:00
set -euo pipefail
umask 077
: "${FEARLESS_RUN_DIR:?Set FEARLESS_RUN_DIR to a private run directory}"
cd "$FEARLESS_RUN_DIR"
export PYTHONPATH="$PWD/code"
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-8}"
export OPENBLAS_NUM_THREADS=1
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
"$FEARLESS_PYTHON" -m fearless.model train --games games.jsonl --catalog catalog.json \
    --output bundle --device gpu --threads "$OMP_NUM_THREADS" --iterations 300 --bootstrap-samples 1000 --joint-experiment
