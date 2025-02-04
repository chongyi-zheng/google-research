# coding=utf-8
# Copyright 2022 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Contrastive RL learner implementation."""
import time
import copy
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple, Callable

import acme
from acme import types
from acme.jax import networks as networks_lib
from acme.jax import utils
from acme.utils import counting
from acme.utils import loggers
from acme.jax import savers
from contrastive import config as contrastive_config
from contrastive import networks as contrastive_networks
import jax
import jax.numpy as jnp
import tensorflow as tf
import optax
import reverb
import numpy as np


class TrainingState(NamedTuple):
    """Contains training state for the learner."""
    policy_optimizer_state: optax.OptState
    q_optimizer_state: optax.OptState
    behavioral_cloning_policy_optimizer_state: optax.OptState
    policy_params: networks_lib.Params
    q_params: networks_lib.Params
    target_q_params: networks_lib.Params
    behavioral_cloning_policy_params: networks_lib.Params
    key: networks_lib.PRNGKey
    alpha_optimizer_state: Optional[optax.OptState] = None
    alpha_params: Optional[networks_lib.Params] = None


class ContrastiveLearner(acme.Learner):
    """Contrastive RL learner."""

    _state: TrainingState

    def __init__(
            self,
            networks: contrastive_networks.ContrastiveNetworks,
            rng: jnp.ndarray,
            policy_optimizer: optax.GradientTransformation,
            q_optimizer: optax.GradientTransformation,
            behavioral_cloning_policy_optimizer: optax.GradientTransformation,
            iterator: Iterator[reverb.ReplaySample],
            counter: counting.Counter,
            logger: loggers.Logger,
            obs_to_goal: Callable[jnp.ndarray, jnp.ndarray],
            config: contrastive_config.ContrastiveConfig,
            trained_learner_state: TrainingState = None):
        """Initialize the Contrastive RL learner.

        Args:
          networks: Contrastive RL networks.
          rng: a key for random number generation.
          policy_optimizer: the policy optimizer.
          q_optimizer: the Q-function optimizer.
          iterator: an iterator over training data.
          counter: counter object used to keep track of steps.
          logger: logger object to be used by learner.
          obs_to_goal: a function for extracting the goal coordinates.
          config: the experiment config file.
        """
        if config.add_mc_to_td:
            assert config.use_td
        adaptive_entropy_coefficient = config.entropy_coefficient is None
        self._num_sgd_steps_per_step = config.num_sgd_steps_per_step
        self._obs_dim = config.obs_dim
        self._use_td = config.use_td
        if adaptive_entropy_coefficient:
            # alpha is the temperature parameter that determines the relative
            # importance of the entropy term versus the reward.
            log_alpha = jnp.asarray(0., dtype=jnp.float32)
            alpha_optimizer = optax.adam(learning_rate=3e-4)
            alpha_optimizer_state = alpha_optimizer.init(log_alpha)
        else:
            if config.target_entropy:
                raise ValueError('target_entropy should not be set when '
                                 'entropy_coefficient is provided')

        self._trained_learner_state = trained_learner_state

        def alpha_loss(log_alpha: jnp.ndarray,
                       policy_params: networks_lib.Params,
                       behavioral_cloning_policy_params: networks_lib.Params,
                       transitions: types.Transition,
                       key: networks_lib.PRNGKey) -> jnp.ndarray:
            """Eq 18 from https://arxiv.org/pdf/1812.05905.pdf."""
            dist_params = networks.policy_network.apply(
                policy_params, transitions.observation)
            action = networks.sample(dist_params, key)
            log_prob = networks.log_prob(dist_params, action)
            alpha = jnp.exp(log_alpha)

            if config.actor_loss_with_reverse_kl:
                behavioral_cloning_dist_params = \
                    networks.behavioral_cloning_policy_network.apply(
                        behavioral_cloning_policy_params,
                        transitions.observation[:, :self._obs_dim])
                log_beta_prob = networks.log_prob(
                    behavioral_cloning_dist_params, action)
                alpha_loss = alpha * jax.lax.stop_gradient(
                    log_beta_prob - log_prob - config.target_entropy)
            else:
                alpha_loss = alpha * jax.lax.stop_gradient(
                    -log_prob - config.target_entropy)
            return jnp.mean(alpha_loss)

        def critic_loss(q_params: networks_lib.Params,
                        policy_params: networks_lib.Params,
                        target_q_params: networks_lib.Params,
                        behavioral_cloning_policy_params: networks_lib.Params,
                        transitions: types.Transition,
                        key: networks_lib.PRNGKey) -> jnp.ndarray:
            batch_size = transitions.observation.shape[0]
            # Note: We might be able to speed up the computation for some of the
            # baselines to making a single network that returns all the values. This
            # avoids computing some of the underlying representations multiple times.
            if config.use_td:
                # For TD learning, the diagonal elements are the immediate next state.
                s, g = jnp.split(transitions.observation, [config.obs_dim], axis=1)
                # next_s, _ = jnp.split(transitions.next_observation, [config.obs_dim],
                #                       axis=1)
                next_s, next_g = jnp.split(transitions.next_observation, [config.obs_dim],
                                           axis=1)
                if config.add_mc_to_td:
                    next_fraction = (1 - config.discount) / ((1 - config.discount) + 1)
                    num_next = int(batch_size * next_fraction)
                    new_g = jnp.concatenate([
                        obs_to_goal(next_s[:num_next]),
                        g[num_next:],
                    ], axis=0)
                else:
                    new_g = obs_to_goal(next_s)
                obs = jnp.concatenate([s, new_g], axis=1)
                transitions = transitions._replace(observation=obs)
            I = jnp.eye(batch_size)  # pylint: disable=invalid-name
            logits = networks.q_network.apply(
                q_params, transitions.observation, transitions.action)
            if self._trained_learner_state:
                oracle_logits = networks.q_network.apply(
                    self._trained_learner_state.q_params,
                    transitions.observation,
                    transitions.action)

            # TODO (chongyiz): debug next action here!!!!!
            # logits = networks.q_network.apply(
            #     q_params, transitions.next_observation, transitions.extras['next_action'])
            if config.use_td and config.use_eq_5:
                # (chongyiz): Note we can only do actual next action to sample next future goals
                # for equation (5)
                next_g_obs = jnp.concatenate([s, next_g], axis=1)
                next_g_logits = networks.q_network.apply(
                    q_params, next_g_obs, transitions.action)
            if config.negative_action_sampling:
                dist_params = networks.policy_network.apply(
                    policy_params, transitions.observation)
                neg_action = networks.sample(dist_params, key)
                neg_action_logits = networks.q_network.apply(
                    q_params, transitions.observation, neg_action)

            if config.use_td:
                # Make sure to use the twin Q trick.
                assert len(logits.shape) == 3

                # We evaluate the next-state Q function using random goals
                # s, g = jnp.split(transitions.observation, [config.obs_dim], axis=1)
                # del s
                next_s = transitions.next_observation[:, :config.obs_dim]
                goal_indices = jnp.roll(jnp.arange(batch_size, dtype=jnp.int32), -1)

                if config.use_eq_5:
                    assert len(next_g_logits.shape) == 3

                    pos_logits = jax.vmap(jnp.diag, -1, -1)(logits)
                    loss_pos1 = optax.sigmoid_binary_cross_entropy(
                        logits=pos_logits, labels=1)  # [B, 2]
                    next_g_pos_logits = jax.vmap(jnp.diag, -1, -1)(next_g_logits)
                    loss_pos2 = optax.sigmoid_binary_cross_entropy(
                        logits=next_g_pos_logits, labels=1)  # [B, 2]

                    neg_logits = logits[jnp.arange(batch_size), goal_indices]
                    loss_neg = optax.sigmoid_binary_cross_entropy(
                        logits=neg_logits, labels=0)  # [B, 2]

                    loss = ((1 - config.discount) * loss_pos1
                            + config.discount * loss_pos2 + loss_neg)
                else:
                    # g = g[goal_indices]
                    rand_g = new_g[goal_indices]

                    transitions = transitions._replace(
                        next_observation=jnp.concatenate([next_s, rand_g], axis=1))
                    # TODO (chongyiz): interpolate between C-Learning and SARSA to see what happens
                    assert 0.0 <= config.c_learning_prob <= 1.0
                    rand = np.random.uniform()
                    if (rand > config.c_learning_prob) and config.actual_next_action:
                        next_action = transitions.extras['next_action']
                    elif (rand > config.c_learning_prob) and config.fitted_next_action:
                        next_dist_params = networks.behavioral_cloning_policy_network.apply(
                            behavioral_cloning_policy_params,
                            transitions.next_observation[:, :self._obs_dim])
                        next_action = networks.sample(next_dist_params, key)
                    elif rand <= config.c_learning_prob:
                        next_dist_params = networks.policy_network.apply(
                            policy_params, transitions.next_observation)
                        next_action = networks.sample(next_dist_params, key)
                    else:
                        raise NotImplementedError
                    if config.next_action_add_gaussian_noise:
                        next_action += jax.random.normal(key, next_action.shape)
                    next_q = networks.q_network.apply(target_q_params,
                                                      transitions.next_observation,
                                                      next_action)  # This outputs logits.
                    next_q = jax.nn.sigmoid(next_q)
                    next_v = jnp.min(next_q, axis=-1)
                    next_v = jax.lax.stop_gradient(next_v)
                    next_v = jnp.diag(next_v)
                    # diag(logits) are predictions for future states.
                    # diag(next_q) are predictions for random states, which correspond to
                    # the predictions logits[range(B), goal_indices].
                    # So, the only thing that's meaningful for next_q is the diagonal. Off
                    # diagonal entries are meaningless and shouldn't be used.
                    w_before_clipping = next_v / (1 - next_v)

                    if self._trained_learner_state:
                        oracle_next_q = networks.q_network.apply(
                            self._trained_learner_state.target_q_params,
                            transitions.next_observation,
                            next_action
                        )
                        oracle_next_v = jax.nn.sigmoid(oracle_next_q)
                        oracle_next_v = jnp.min(oracle_next_v, axis=-1)
                        oracle_next_v = jnp.diag(oracle_next_v)
                        oracle_w_before_clipping = oracle_next_v / (1 - oracle_next_v)

                    w_clipping = 20.0
                    # w_clipping = config.w_clipping
                    w = jnp.clip(w_before_clipping, 0, w_clipping)
                    # (B, B, 2) --> (B, 2), computes diagonal of each twin Q.
                    pos_logits = jax.vmap(jnp.diag, -1, -1)(logits)
                    if self._trained_learner_state:
                        oracle_pos_logits = jax.vmap(jnp.diag, -1, -1)(oracle_logits)
                    loss_pos = optax.sigmoid_binary_cross_entropy(
                        logits=pos_logits, labels=1)  # [B, 2]

                    neg_logits = logits[jnp.arange(batch_size), goal_indices]
                    if self._trained_learner_state:
                        oracle_neg_logits = oracle_logits[jnp.arange(batch_size), goal_indices]
                    loss_neg1 = w[:, None] * optax.sigmoid_binary_cross_entropy(
                        logits=neg_logits, labels=1)  # [B, 2]
                    loss_neg2 = optax.sigmoid_binary_cross_entropy(
                        logits=neg_logits, labels=0)  # [B, 2]

                    if config.add_mc_to_td:
                        loss = ((1 + (1 - config.discount)) * loss_pos
                                + config.discount * loss_neg1 + 2 * loss_neg2)
                    else:
                        loss = ((1 - config.discount) * loss_pos
                                + config.discount * loss_neg1 + loss_neg2)
                # Take the mean here so that we can compute the accuracy.
                logits = jnp.mean(logits, axis=-1)

            else:  # For the MC losses.
                def loss_fn(_logits):  # pylint: disable=invalid-name
                    if config.use_cpc:
                        return (optax.softmax_cross_entropy(logits=_logits, labels=I)
                                + 0.01 * jax.nn.logsumexp(_logits, axis=1) ** 2)
                    else:
                        return optax.sigmoid_binary_cross_entropy(logits=_logits, labels=I)

                if len(logits.shape) == 3:  # twin q
                    # loss.shape = [.., num_q]
                    if config.negative_action_sampling:
                        pos_logits = jax.vmap(jnp.diag, -1, -1)(logits)
                        pos_loss = optax.sigmoid_binary_cross_entropy(
                            logits=pos_logits, labels=1)  # [B, 2]
                        if config.negative_action_sampling_future_goals:
                            neg_logits = jax.vmap(jnp.diag, -1, -1)(neg_action_logits)
                        else:
                            rnd_goal_indices = jnp.roll(jnp.arange(batch_size, dtype=jnp.int32), -1)
                            neg_logits = neg_action_logits[jnp.arange(batch_size), rnd_goal_indices]
                        neg_loss = optax.sigmoid_binary_cross_entropy(
                            logits=neg_logits, labels=0)
                        # TODO (chongyiz): Can we simply sum the pos_loss and neg_loss here?
                        loss = jnp.stack(
                            [jnp.mean(pos_loss, axis=-1), jnp.mean(neg_loss, axis=-1)],
                            axis=-1)
                    else:
                        loss = jax.vmap(loss_fn, in_axes=2, out_axes=-1)(logits)
                        loss = jnp.mean(loss, axis=-1)
                    # Take the mean here so that we can compute the accuracy.
                    logits = jnp.mean(logits, axis=-1)
                else:
                    loss = loss_fn(logits)

            loss = jnp.mean(loss)
            correct = (jnp.argmax(logits, axis=1) == jnp.argmax(I, axis=1))
            logits_pos = jnp.sum(logits * I) / jnp.sum(I)
            logits_neg = jnp.sum(logits * (1 - I)) / jnp.sum(1 - I)
            q_pos, q_neg = jnp.sum(jax.nn.sigmoid(logits) * I) / jnp.sum(I), \
                           jnp.sum(jax.nn.sigmoid(logits) * (1 - I)) / jnp.sum(1 - I)
            q_pos_ratio, q_neg_ratio = q_pos / (1 - q_pos), q_neg / (1 - q_neg)
            # TODO (chongyiz): The magnitude between q_pos_ratio and q_neg_ratio are unbalanced now!
            # q_ratio = (q_pos_ratio + q_neg_ratio) / 2
            if len(logits.shape) == 3:
                logsumexp = jax.nn.logsumexp(logits[:, :, 0], axis=1) ** 2
            else:
                logsumexp = jax.nn.logsumexp(logits, axis=1) ** 2
            metrics = {
                'binary_accuracy': jnp.mean((logits > 0) == I),
                'categorical_accuracy': jnp.mean(correct),
                'logits_pos': logits_pos,
                'logits_neg': logits_neg,
                'logsumexp': logsumexp.mean(),
                'q_pos_ratio': q_pos_ratio,
                'q_neg_ratio': q_neg_ratio,
                # 'q_ratio': q_ratio,
            }

            if self._use_td and self._trained_learner_state:
                oracle_logits = jnp.mean(oracle_logits, axis=-1)
                oracle_logits_pos = jnp.sum(oracle_logits * I) / jnp.sum(I)
                oracle_logits_neg = jnp.sum(oracle_logits * (1 - I)) / jnp.sum(1 - I)
                oracle_q_pos, oracle_q_neg = \
                    jnp.sum(jax.nn.sigmoid(oracle_logits) * I) / jnp.sum(I), \
                    jnp.sum(jax.nn.sigmoid(oracle_logits) * (1 - I)) / jnp.sum(1 - I)
                oracle_q_pos_ratio, oracle_q_neg_ratio = \
                    oracle_q_pos / (1 - oracle_q_pos), \
                    oracle_q_neg / (1 - oracle_q_neg)

                metrics['w_before_clipping'] = w_before_clipping.mean()
                metrics['oracle_w_before_clipping'] = oracle_w_before_clipping.mean()
                # metrics['pos_logits'] = pos_logits.mean()
                metrics['oracle_logits_pos'] = oracle_logits_pos
                # metrics['neg_logits'] = neg_logits.mean()
                metrics['oracle_logits_neg'] = oracle_logits_neg
                metrics['oracle_q_pos_ratio'] = oracle_q_pos_ratio.mean()
                metrics['oracle_q_neg_ratio'] = oracle_q_neg_ratio.mean()

            return loss, metrics

        def behavioral_cloning_loss(behavioral_cloning_policy_params: networks_lib.Params,
                                    transitions: types.Transition,
                                    key: networks_lib.PRNGKey):
            del key
            dist_params = networks.behavioral_cloning_policy_network.apply(
                behavioral_cloning_policy_params,
                transitions.observation[:, :self._obs_dim])
            log_prob = networks.log_prob(dist_params, transitions.action)
            bc_loss = -1.0 * jnp.mean(log_prob)

            return bc_loss

        def actor_loss(policy_params: networks_lib.Params,
                       q_params: networks_lib.Params,
                       target_q_params: networks_lib.Params,
                       behavioral_cloning_policy_params: networks_lib.Params,
                       alpha: jnp.ndarray,
                       transitions: types.Transition,
                       key: networks_lib.PRNGKey,
                       ) -> jnp.ndarray:
            obs = transitions.observation
            if config.use_gcbc:
                dist_params = networks.policy_network.apply(
                    policy_params, obs)
                log_prob = networks.log_prob(dist_params, transitions.action)
                actor_loss = -1.0 * jnp.mean(log_prob)
            else:
                state = obs[:, :config.obs_dim]
                goal = obs[:, config.obs_dim:]

                if config.random_goals == 0.0:
                    new_state = state
                    new_goal = goal
                elif config.random_goals == 0.5:
                    new_state = jnp.concatenate([state, state], axis=0)
                    new_goal = jnp.concatenate([goal, jnp.roll(goal, 1, axis=0)], axis=0)
                else:
                    assert config.random_goals == 1.0
                    new_state = state
                    new_goal = jnp.roll(goal, 1, axis=0)

                new_obs = jnp.concatenate([new_state, new_goal], axis=1)
                dist_params = networks.policy_network.apply(
                    policy_params, new_obs)
                action = networks.sample(dist_params, key)
                log_prob = networks.log_prob(dist_params, action)
                if config.actor_loss_with_target_critic:
                    q_action = networks.q_network.apply(
                        target_q_params, new_obs, action)
                else:
                    q_action = networks.q_network.apply(
                        q_params, new_obs, action)
                if len(q_action.shape) == 3:  # twin q trick
                    assert q_action.shape[2] == 2
                    q_action = jnp.min(q_action, axis=-1)
                # TODO (chongyiz): implement reverse KL
                if config.actor_loss_with_reverse_kl:
                    actor_loss = -jnp.diag(q_action)
                    assert 0.0 <= config.reverse_kl_coef <= 1.0
                    if config.reverse_kl_coef > 0:
                        behavioral_cloning_dist_params = \
                            networks.behavioral_cloning_policy_network.apply(
                                behavioral_cloning_policy_params,
                                new_obs[:, :self._obs_dim])
                        log_beta_prob = networks.log_prob(
                            behavioral_cloning_dist_params, action)
                        reverse_kl_loss = log_prob - log_beta_prob
                        actor_loss = config.reverse_kl_coef * reverse_kl_loss + \
                                     (1 - config.reverse_kl_coef) * actor_loss
                else:
                    actor_loss = alpha * log_prob - jnp.diag(q_action)
                    assert 0.0 <= config.bc_coef <= 1.0
                    if config.bc_coef > 0:
                        orig_action = transitions.action
                        if config.random_goals == 0.5:
                            orig_action = jnp.concatenate([orig_action, orig_action], axis=0)

                        bc_loss = -1.0 * networks.log_prob(dist_params, orig_action)
                        actor_loss = config.bc_coef * bc_loss + (1 - config.bc_coef) * actor_loss

            return jnp.mean(actor_loss)

        alpha_grad = jax.value_and_grad(alpha_loss)
        critic_grad = jax.value_and_grad(critic_loss, has_aux=True)
        actor_grad = jax.value_and_grad(actor_loss)
        behavioral_cloning_grad = jax.value_and_grad(behavioral_cloning_loss)

        def update_step(
                state: TrainingState,
                transitions: types.Transition,
        ) -> Tuple[TrainingState, Dict[str, jnp.ndarray]]:

            key, key_alpha, key_critic, key_actor, key_behavioral_cloning = \
                jax.random.split(state.key, 5)
            # key, key_alpha, key_critic, key_actor = jax.random.split(state.key, 4)
            if adaptive_entropy_coefficient:
                alpha_loss, alpha_grads = alpha_grad(state.alpha_params,
                                                     state.policy_params,
                                                     state.behavioral_cloning_policy_params,
                                                     transitions,
                                                     key_alpha)
                alpha = jnp.exp(state.alpha_params)
            else:
                alpha = config.entropy_coefficient

            if not config.use_gcbc:
                (critic_loss, critic_metrics), critic_grads = critic_grad(
                    state.q_params, state.policy_params, state.target_q_params,
                    state.behavioral_cloning_policy_params, transitions, key_critic)

            actor_loss, actor_grads = actor_grad(state.policy_params, state.q_params,
                                                 state.target_q_params,
                                                 state.behavioral_cloning_policy_params,
                                                 alpha, transitions, key_actor)

            behavioral_cloning_loss, behavioral_cloning_grads = behavioral_cloning_grad(
                state.behavioral_cloning_policy_params, transitions, key_behavioral_cloning)

            # Apply policy gradients
            actor_update, policy_optimizer_state = policy_optimizer.update(
                actor_grads, state.policy_optimizer_state)
            policy_params = optax.apply_updates(state.policy_params, actor_update)

            # Apply critic gradients
            if config.use_gcbc:
                metrics = {}
                critic_loss = 0.0
                q_params = state.q_params
                q_optimizer_state = state.q_optimizer_state
                new_target_q_params = state.target_q_params
            else:
                critic_update, q_optimizer_state = q_optimizer.update(
                    critic_grads, state.q_optimizer_state)

                q_params = optax.apply_updates(state.q_params, critic_update)

                new_target_q_params = jax.tree_multimap(
                    lambda x, y: x * (1 - config.tau) + y * config.tau,
                    state.target_q_params, q_params)
                metrics = critic_metrics

            # (chongyiz): Apply behavioral cloning gradients
            behavioral_cloning_update, behavioral_cloning_policy_optimizer_state = \
                behavioral_cloning_policy_optimizer.update(
                    behavioral_cloning_grads,
                    state.behavioral_cloning_policy_optimizer_state)
            behavioral_cloning_policy_params = optax.apply_updates(
                state.behavioral_cloning_policy_params,
                behavioral_cloning_update)

            metrics.update({
                'critic_loss': critic_loss,
                'actor_loss': actor_loss,
                'behavioral_cloning_loss': behavioral_cloning_loss,
            })

            new_state = TrainingState(
                policy_optimizer_state=policy_optimizer_state,
                q_optimizer_state=q_optimizer_state,
                behavioral_cloning_policy_optimizer_state=behavioral_cloning_policy_optimizer_state,
                policy_params=policy_params,
                q_params=q_params,
                target_q_params=new_target_q_params,
                behavioral_cloning_policy_params=behavioral_cloning_policy_params,
                key=key,
            )
            if adaptive_entropy_coefficient:
                # Apply alpha gradients
                alpha_update, alpha_optimizer_state = alpha_optimizer.update(
                    alpha_grads, state.alpha_optimizer_state)
                alpha_params = optax.apply_updates(state.alpha_params, alpha_update)
                metrics.update({
                    'alpha_loss': alpha_loss,
                    'alpha': jnp.exp(alpha_params),
                })
                new_state = new_state._replace(
                    alpha_optimizer_state=alpha_optimizer_state,
                    alpha_params=alpha_params)

            return new_state, metrics

        # General learner book-keeping and loggers.
        self._counter = counter or counting.Counter()
        # (chongyiz): dummy increment to prevent field missing in the evaluator CSV log
        self._counter.increment(steps=0, walltime=0)
        self._logger = logger or loggers.make_default_logger(
            'learner', asynchronous=True, serialize_fn=utils.fetch_devicearray,
            time_delta=10.0)

        # Iterator on demonstration transitions.
        self._iterator = iterator

        update_step = utils.process_multiple_batches(update_step,
                                                     config.num_sgd_steps_per_step)
        # Use the JIT compiler.
        if config.jit:
            self._update_step = jax.jit(update_step)
        else:
            self._update_step = update_step

        def make_initial_state(key: networks_lib.PRNGKey) -> TrainingState:
            """Initialises the training state (parameters and optimiser state)."""
            key_policy, key_q, key_behavioral_cloning_policy, key = jax.random.split(key, 4)

            policy_params = networks.policy_network.init(key_policy)
            policy_optimizer_state = policy_optimizer.init(policy_params)

            q_params = networks.q_network.init(key_q)
            q_optimizer_state = q_optimizer.init(q_params)

            behavioral_cloning_policy_params = \
                networks.behavioral_cloning_policy_network.init(
                    key_behavioral_cloning_policy)
            behavioral_cloning_policy_optimizer_state = \
                behavioral_cloning_policy_optimizer.init(
                    behavioral_cloning_policy_params)

            state = TrainingState(
                policy_optimizer_state=policy_optimizer_state,
                q_optimizer_state=q_optimizer_state,
                behavioral_cloning_policy_optimizer_state=behavioral_cloning_policy_optimizer_state,
                policy_params=policy_params,
                q_params=q_params,
                behavioral_cloning_policy_params=behavioral_cloning_policy_params,
                target_q_params=q_params,
                key=key)

            if adaptive_entropy_coefficient:
                state = state._replace(alpha_optimizer_state=alpha_optimizer_state,
                                       alpha_params=log_alpha)
            return state

        # Create initial state.
        self._state = make_initial_state(rng)

        # Do not record timestamps until after the first learning step is done.
        # This is to avoid including the time it takes for actors to come online
        # and fill the replay buffer.
        self._timestamp = None

        # # (chongyiz): load the trained agent here.
        # if config.trained_agent_dir is not None:
        #     trained_learner = copy.deepcopy(self)
        #     ckpt = savers.Checkpointer(
        #         object_to_save={'learner': trained_learner},
        #         directory=config.trained_agent_dir,
        #         subdirectory='learner',
        #         add_uid=False,
        #     )
        #     ckpt.restore()
        #
        #     q0 = self._state.q_params.copy()
        #     q1 = trained_learner._state.q_params.copy()
        #
        #     for k, v in q0.items():
        #         for k_, v_ in v.items():
        #             assert (q0[k][k_] - q1[k][k_]).sum() != 0, f'New parameters are the same as old {k}.{k_}'
        #             print(f'{k}.{k_} parameters successfully updated!')
        #
        # print()
        # print()

    def step(self):
        with jax.profiler.StepTraceAnnotation('step', step_num=self._counter):
            sample = next(self._iterator)
            transitions = types.Transition(*sample.data)
            self._state, metrics = self._update_step(self._state, transitions)

        # Compute elapsed time.
        timestamp = time.time()
        elapsed_time = timestamp - self._timestamp if self._timestamp else 0
        self._timestamp = timestamp

        # Increment counts and record the current time
        counts = self._counter.increment(steps=1, walltime=elapsed_time)
        if elapsed_time > 0:
            metrics['steps_per_second'] = (
                    self._num_sgd_steps_per_step / elapsed_time)
        else:
            metrics['steps_per_second'] = 0.

        # Attempts to write the logs.
        self._logger.write({**metrics, **counts})

    def get_variables(self, names: List[str]) -> List[Any]:
        variables = {
            'policy': self._state.policy_params,
            'critic': self._state.q_params,
        }
        return [variables[name] for name in names]

    def save(self) -> TrainingState:
        return self._state

    def restore(self, state: TrainingState):
        self._state = state
