#!/bin/bash

EXP_LABEL=$1

SCRIPT_DIR=$(dirname "$BASH_SOURCE")
PROJECT_DIR=$(realpath "$SCRIPT_DIR/..")

export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$HOME/.mujoco/mujoco200/bin
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib/nvidia-000
export PYTHONPATH=$PROJECT_DIR
export HDF5_USE_FILE_LOCKING=FALSE

declare -a all_bc_lambdas=(0.125 0.25 0.5 0.75 1.0 1.5)
declare -a seeds=(0)

for bc_lambda in "${all_bc_lambdas[@]}"; do
  for seed in "${seeds[@]}"; do
    rm $CONDA_PREFIX/lib/python*/site-packages/mujoco_py/generated/mujocopy-buildlock
    mkdir -p ~/offline_c_learning/c_learning_offline_logs/"${EXP_LABEL}"/maze2d_open_v0_bc_lambda="${bc_lambda}"/$seed
    nohup \
    python $PROJECT_DIR/train_eval_offline_d4rl.py \
      --gin_bindings="train_eval_offline.env_name='maze2d-open-v0'" \
      --gin_bindings="train_eval_offline.random_seed=${seed}" \
      --gin_bindings="train_eval_offline.num_iterations=1000000" \
      --gin_bindings="train_eval_offline.max_future_steps=50" \
      --gin_bindings="obs_to_goal.start_index=0" \
      --gin_bindings="obs_to_goal.end_index=2" \
      --gin_bindings="offline_goal_fn.relabel_next_prob=0.3" \
      --gin_bindings="offline_goal_fn.relabel_next_future_prob=0.2" \
      --gin_bindings="offline_goal_fn.setting='b'" \
      --gin_bindings="offline_c_learning_agent.actor_loss.ce_loss=True" \
      --gin_bindings="offline_c_learning_agent.actor_loss.bc_loss=True" \
      --gin_bindings="offline_c_learning_agent.actor_loss.bc_lambda=${bc_lambda}" \
      --gin_bindings="offline_c_learning_agent.critic_loss.policy_ratio=False" \
      --root_dir ~/offline_c_learning/c_learning_offline_logs/"${EXP_LABEL}"/maze2d_open_v0_bc_lambda="${bc_lambda}"/$seed \
    > ~/offline_c_learning/c_learning_offline_logs/"${EXP_LABEL}"/maze2d_open_v0_bc_lambda="${bc_lambda}"/$seed/stream.log 2>&1 & \
    sleep 5
  done
done
