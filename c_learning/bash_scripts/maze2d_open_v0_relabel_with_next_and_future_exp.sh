#!/bin/bash

EXP_LABEL=$1

SCRIPT_DIR=$(dirname "$BASH_SOURCE")
PROJECT_DIR=$(realpath "$SCRIPT_DIR/..")

export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$HOME/.mujoco/mujoco200/bin
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib/nvidia-000
export PYTHONPATH=$PROJECT_DIR

declare -a seeds=(0 1 2)

for seed in "${seeds[@]}"; do
  mkdir -p ~/offline_c_learning/c_learning_logs/"${EXP_LABEL}"_relabel_with_next_and_future/maze2d_open_v0/$seed
  nohup \
  python $PROJECT_DIR/train_eval.py \
    --gin_bindings="train_eval.env_name='maze2d-open-v0'" \
    --gin_bindings="train_eval.random_seed=${seed}" \
    --gin_bindings="train_eval.num_iterations=1000000" \
    --gin_bindings="obs_to_goal.start_index=0" \
    --gin_bindings="obs_to_goal.end_index=2" \
    --gin_bindings="goal_fn.relabel_next_prob=0.3" \
    --gin_bindings="goal_fn.relabel_future_prob=0.2" \
    --root_dir ~/offline_c_learning/c_learning_logs/"${EXP_LABEL}"_relabel_with_next_and_future/maze2d_open_v0/$seed \
  > ~/offline_c_learning/c_learning_logs/"${EXP_LABEL}"_relabel_with_next_and_future/maze2d_open_v0/$seed/stream.log 2>&1 &
done
