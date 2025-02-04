# coding=utf-8
# Copyright 2023 The Google Research Authors.
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
from typing import NamedTuple, Optional

import acme
import numpy as np
import jax
import jax.numpy as jnp
import optax
from acme import types
from acme.jax import networks as networks_lib
from acme.jax import utils
from acme.utils import counting
from acme.utils import loggers


class TrainingState(NamedTuple):
    """Contains training state for the learner."""
    policy_optimizer_state: optax.OptState
    q_optimizer_state: optax.OptState
    policy_params: networks_lib.Params
    q_params: networks_lib.Params
    target_q_params: networks_lib.Params
    key: networks_lib.PRNGKey
    alpha_optimizer_state: Optional[optax.OptState] = None
    alpha_params: Optional[networks_lib.Params] = None


class ContrastiveLearner(acme.Learner):
    """Contrastive RL learner."""

    _state: TrainingState

    def __init__(
            self,
            networks,
            rng,
            policy_optimizer,
            q_optimizer,
            iterator,
            counter,
            logger,
            obs_to_goal,
            config):
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

        def alpha_loss(log_alpha,
                       policy_params,
                       transitions,
                       key):
            """Eq 18 from https://arxiv.org/pdf/1812.05905.pdf."""

            state = transitions.observation[:, 0, :config.obs_dim]
            goal = transitions.observation[:, 1, :config.obs_dim]

            dist_params = networks.policy_network.apply(
                policy_params, jnp.concatenate([state, goal], axis=-1))
            action = networks.sample(dist_params, key)
            log_prob = networks.log_prob(dist_params, action)
            alpha = jnp.exp(log_alpha)
            alpha_loss = alpha * jax.lax.stop_gradient(
                -log_prob - config.target_entropy)
            return jnp.mean(alpha_loss)

        def critic_loss(q_params,
                        policy_params,
                        target_q_params,
                        transitions,
                        key):
            batch_size = transitions.observation.shape[0]
            # Note: We might be able to speed up the computation for some of the
            # baselines to making a single network that returns all the values. This
            # avoids computing some of the underlying representations multiple times.
            if config.use_td:
                # For TD learning, the diagonal elements are the immediate next state.
                # s, g = jnp.split(transitions.observation[:, 0], [config.obs_dim], axis=1)
                s, _ = jnp.split(transitions.observation[:, 0], [config.obs_dim], axis=1)
                g, _ = jnp.split(transitions.observation[:, 1], [config.obs_dim], axis=1)
                # g = jnp.roll(g, 1, axis=0)
                next_s, _ = jnp.split(transitions.next_observation[:, 0], [config.obs_dim],
                                      axis=1)
                # if config.add_mc_to_td:
                #     next_fraction = (1 - config.discount) / ((1 - config.discount) + 1)
                #     num_next = int(batch_size * next_fraction)
                #     new_g = jnp.concatenate([
                #         obs_to_goal(next_s[:num_next]),
                #         g[num_next:],
                #     ], axis=0)
                # else:
                #     new_g = obs_to_goal(next_s)
                # obs = jnp.concatenate([s, new_g], axis=1)
                # transitions = transitions._replace(observation=obs)
            I = jnp.eye(batch_size)  # pylint: disable=invalid-name
            # logits = networks.q_network.apply(
            #     q_params, transitions.observation, transitions.action)
            # logits = networks.q_network.apply(
            #     q_params, s, transitions.action, next_s, next_s,
            # )
            # logits = networks.q_network.apply(
            #     q_params, transitions.observation[:, :config.obs_dim], transitions.action,
            #     transitions.observation[:, config.obs_dim:], transitions.observation[:, config.obs_dim:]
            # )
            # logits = networks.q_network.apply(
            #     q_params, s, g, next_s)
            # pos_logits = jnp.einsum('ijkl,ik->ijl', logits, transitions.action)
            # c-learning
            # pos_logits = networks.q_network.apply(
            #     q_params, s, transitions.action, next_s, next_s)
            # c-learning for arbitrary fs, TD-InfoNCE
            pos_logits = networks.q_network.apply(
                q_params, s, transitions.action[:, 0], g, next_s)
            # pos_logits = jnp.einsum('ijk,ij->ik', logits, transitions.action)

            if config.use_td:
                # Make sure to use the twin Q trick.
                # predict logits
                # assert len(pos_logits.shape) == 2
                # A_phi_psi
                assert len(pos_logits.shape) == 3

                # We evaluate the next-state Q function using random goals
                # s, g = jnp.split(transitions.observation, [config.obs_dim], axis=1)
                # del s
                # next_s = transitions.next_observation[:, 0, :config.obs_dim]
                goal_indices = jnp.roll(jnp.arange(batch_size, dtype=jnp.int32), -1)
                rand_g = g[goal_indices]
                # rand_g, _ = jnp.split(transitions.observation[:, 2], [config.obs_dim], axis=1)
                # rand_g = transitions.observation[:, 1, :config.obs_dim]
                # transitions = transitions._replace(
                #     next_observation=jnp.concatenate([next_s, g], axis=1))
                # c-learning
                # next_dist_params = networks.policy_network.apply(
                #     policy_params, jnp.concatenate([next_s, rand_g], axis=1))
                # c-learning for arbitrary fs, TD-InfoNCE
                next_dist_params = networks.policy_network.apply(
                    policy_params, jnp.concatenate([next_s, g], axis=-1))

                # discrete environment
                # c = next_dist_params.cumsum(axis=1)
                # u = jax.random.uniform(key, shape=(len(c), 1))
                # next_a = (u < c).argmax(axis=1)
                # next_a = jax.nn.one_hot(next_a, 5)

                # continuous environment
                # key, subkey = jax.random.split(key)
                next_a = networks.sample(next_dist_params, key)

                # next_action = networks.sample(next_dist_params, key)
                # index = next_action.argmax(axis=-1)
                # hard_next_action = jax.nn.one_hot(index, 5)
                # hard_next_action = hard_next_action - jax.lax.stop_gradient(next_action) + next_action
                # next_q = networks.q_network.apply(target_q_params,
                #                                   transitions.next_observation,
                #                                   next_action)  # This outputs logits.
                # next_q = networks.q_network.apply(target_q_params,
                #                                   next_s,
                #                                   hard_next_action,
                #                                   g, g)  # This outputs logits.
                # c-learning
                # next_q = networks.q_network.apply(target_q_params,
                #                                   next_s, next_a, rand_g, rand_g)
                # c-learning for arbitrary fs, TD-InfoNCE
                next_q = networks.q_network.apply(target_q_params,
                                                  next_s, next_a, g, rand_g)

                # # next_q = networks.q_network.apply(target_q_params, next_s, next_action, rand_g, rand_g)
                # # next_q = jax.nn.sigmoid(next_q)
                # # next_v = jnp.min(next_q, axis=-1)
                # next_q = jnp.min(next_q, axis=-1)
                # # next_q = jnp.mean(next_q, axis=-1)
                # next_q = jax.lax.stop_gradient(next_q)
                # # A_phi_psi
                # next_v = jnp.diag(next_q)
                # # diag(logits) are predictions for future states.
                # # diag(next_q) are predictions for random states, which correspond to
                # # the predictions logits[range(B), goal_indices].
                # # So, the only thing that's meaningful for next_q is the diagonal. Off
                # # diagonal entries are meaningless and shouldn't be used.
                # # w = next_v / (1 - next_v)
                # w = jnp.exp(next_v)
                # w_clipping = 20.0
                # w = jnp.clip(w, 0, w_clipping)
                # # w = jnp.einsum('ij,ij->i', w, next_action)
                # # (B, B, 2) --> (B, 2), computes diagonal of each twin Q.

                # TD-InfoNCE w
                next_v = jnp.min(next_q, axis=-1)
                if config.use_arbitrary_func_reg:
                    w = jnp.exp(next_v) / batch_size
                    # w = jnp.exp(next_v)
                else:
                    w = jax.nn.softmax(next_v, axis=1)
                    # w = batch_size * jax.nn.softmax(next_v, axis=1)
                w = jax.lax.stop_gradient(w)  # (B, B)

                # A_phi_psi
                # pos_logits = jax.vmap(jnp.diag, -1, -1)(pos_logits)
                # loss_pos = optax.sigmoid_binary_cross_entropy(
                #     logits=pos_logits, labels=1)  # [B, 2]

                # TD-InfoNCE
                I = I[:, :, None].repeat(pos_logits.shape[-1], axis=-1)
                loss_pos = jax.vmap(optax.softmax_cross_entropy, -1, -1)(
                    pos_logits, I)

                # neg_logits = logits[jnp.arange(batch_size), goal_indices]
                # neg_logits = networks.q_network.apply(q_params, s, transitions.action, g, rand_g)
                # c-learning
                # neg_logits = networks.q_network.apply(q_params, s, transitions.action, rand_g, rand_g)
                # c-learning for arbitrary fs, TD-InfoNCE
                neg_logits = networks.q_network.apply(q_params, s, transitions.action[:, 0], g, rand_g)

                # # neg_logits = jnp.einsum('ijk,ij->ik', neg_logits, transitions.action)
                # # A_phi_psi
                # neg_logits = jax.vmap(jnp.diag, -1, -1)(neg_logits)
                # loss_neg1 = w[:, None] * optax.sigmoid_binary_cross_entropy(
                #     logits=neg_logits, labels=1)  # [B, 2]
                # loss_neg2 = optax.sigmoid_binary_cross_entropy(
                #     logits=neg_logits, labels=0)  # [B, 2]
                #
                # if config.add_mc_to_td:
                #     loss = ((1 + (1 - config.discount)) * loss_pos
                #             + config.discount * loss_neg1 + 2 * loss_neg2)
                # else:
                #     loss = ((1 - config.discount) * loss_pos
                #             + config.discount * loss_neg1 + loss_neg2)

                # TD-InfoNCE loss
                loss_neg = jax.vmap(optax.softmax_cross_entropy, -1, -1)(
                    neg_logits, w[:, :, None].repeat(neg_logits.shape[-1], axis=-1))

                loss = (1 - config.discount) * loss_pos + config.discount * loss_neg

                if config.use_arbitrary_func_reg:
                    # regularization
                    reg = jnp.mean((jax.nn.logsumexp(neg_logits, axis=1) - jnp.log(batch_size)) ** 2)
                    reg_loss = config.arbitrary_func_reg_coef * reg
                    loss += jnp.mean(reg_loss)
                else:
                    reg_loss = 0.0

                # Take the mean here so that we can compute the accuracy.
                # logits = jnp.mean(logits, axis=-1)

            else:  # For the MC losses.
                def loss_fn(_logits):  # pylint: disable=invalid-name
                    if config.use_cpc:
                        return (optax.softmax_cross_entropy(logits=_logits, labels=I)
                                + 0.01 * jax.nn.logsumexp(_logits, axis=1) ** 2)
                    else:
                        return optax.sigmoid_binary_cross_entropy(logits=_logits, labels=I)

                if len(logits.shape) == 3:  # twin q
                    # loss.shape = [.., num_q]
                    loss = jax.vmap(loss_fn, in_axes=2, out_axes=-1)(logits)
                    loss = jnp.mean(loss, axis=-1)
                    # Take the mean here so that we can compute the accuracy.
                    logits = jnp.mean(logits, axis=-1)
                else:
                    loss = loss_fn(logits)

            loss = jnp.mean(loss)
            # correct = (jnp.argmax(logits, axis=1) == jnp.argmax(I, axis=1))
            # logits_pos = jnp.sum(logits * I) / jnp.sum(I)
            # logits_neg = jnp.sum(logits * (1 - I)) / jnp.sum(1 - I)
            # if len(logits.shape) == 3:
            #     logsumexp = jax.nn.logsumexp(logits[:, :, 0], axis=1) ** 2
            # else:
            #     logsumexp = jax.nn.logsumexp(logits, axis=1) ** 2
            metrics = {
                # 'binary_accuracy': jnp.mean((logits > 0) == I),
                # 'categorical_accuracy': jnp.mean(correct),
                'logits_pos': jnp.mean(jax.vmap(jnp.diag, -1, -1)(pos_logits)),
                'logits_pos1': jnp.mean(jnp.diag(pos_logits[..., 0])),
                'logits_pos2': jnp.mean(jnp.diag(pos_logits[..., 1])),
                'logits_neg': jnp.mean(neg_logits),
                'logits_neg1': jnp.mean(neg_logits[..., 0]),
                'logits_neg2': jnp.mean(neg_logits[..., 1]),
                # 'logsumexp': logsumexp.mean(),
                # 'w': jnp.mean(w),
                # 'w': jnp.mean(jnp.exp(next_q)),
                'w': jnp.mean(jnp.diag(w)),
                'w_mean': jnp.mean(w),
                "reg_loss": jnp.mean(reg_loss),
            }

            return loss, metrics

        def actor_loss(policy_params,
                       q_params,
                       alpha,
                       transitions,
                       key,
                       ):
            obs = transitions.observation
            if config.use_gcbc:
                dist_params = networks.policy_network.apply(
                    policy_params, obs)
                log_prob = networks.log_prob(dist_params, transitions.action)
                actor_loss = -1.0 * jnp.mean(log_prob)
            else:
                state = obs[:, 0, :config.obs_dim]
                goal = obs[:, 0, config.obs_dim:]

                # state = obs[:, 0, :config.obs_dim]
                # goal = obs[:, 1, :config.obs_dim]

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

                batch_size = new_state.shape[0]

                new_obs = jnp.concatenate([new_state, new_goal], axis=1)
                dist_params = networks.policy_network.apply(
                    policy_params, new_obs)

                # discrete environment
                # c = dist_params.cumsum(axis=1)
                # u = jax.random.uniform(key, shape=(len(c), 1))
                # hard_action = (u < c).argmax(axis=1)
                # hard_action = jax.nn.one_hot(hard_action, 5)
                # action = hard_action - jax.lax.stop_gradient(dist_params) + dist_params  # propagate gradient

                # continuous environment
                # key, subkey = jax.random.split(key)
                action = networks.sample(dist_params, key)
                log_prob = networks.log_prob(dist_params, action)

                # action_dist = networks.sample(dist_params, key)
                # log_prob = networks.log_prob(dist_params, action)
                # index = action.argmax(axis=-1)
                # hard_action = jax.nn.one_hot(index, 5)
                # hard_action = hard_action - jax.lax.stop_gradient(action) + action
                # q_action = networks.q_network.apply(
                #     q_params, new_obs, action)
                # q_action = networks.q_network.apply(
                #     q_params, new_state, hard_action, new_goal, new_goal)
                q_action = networks.q_network.apply(
                    q_params, new_state, action, new_goal, new_goal)
                # predict logits
                # if len(q_action.shape) == 2:  # twin q trick
                # A_phi_psi
                if len(q_action.shape) == 3:  # twin q trick
                    assert q_action.shape[-1] == 2
                    q_action = jnp.min(q_action, axis=-1)
                # actor_loss = alpha * log_prob - jnp.diag(q_action)
                # q_action = jnp.sum(q_action * action_dist, axis=-1)
                # q_action = jnp.einsum('ij,ij->i', q_action, action)
                # actor_loss = -jnp.diag(q_action)
                # predict logits
                # actor_loss = -q_action
                # A_phi_psi
                # actor_loss = alpha * log_prob - jnp.diag(q_action)

                # TD-InfoNCE
                if config.use_arbitrary_func_reg:
                    actor_q_loss = -jnp.diag(q_action)
                else:
                    I = jnp.eye(batch_size)
                    actor_q_loss = optax.softmax_cross_entropy(logits=q_action, labels=I)
                actor_loss = alpha * log_prob + actor_q_loss  # (B, num_actions)

                assert 0.0 <= config.bc_coef <= 1.0
                if config.bc_coef > 0:
                    orig_action = transitions.action
                    if config.random_goals == 0.5:
                        orig_action = jnp.concatenate([orig_action, orig_action], axis=0)

                    bc_loss = -1.0 * networks.log_prob(dist_params, orig_action)
                    actor_loss = (config.bc_coef * bc_loss
                                  + (1 - config.bc_coef) * actor_loss)

            return jnp.mean(actor_loss)

        alpha_grad = jax.value_and_grad(alpha_loss)
        critic_grad = jax.value_and_grad(critic_loss, has_aux=True)
        actor_grad = jax.value_and_grad(actor_loss)

        def update_step(
                state,
                transitions,
        ):

            key, key_alpha, key_critic, key_actor = jax.random.split(state.key, 4)
            if adaptive_entropy_coefficient:
                alpha_loss, alpha_grads = alpha_grad(state.alpha_params,
                                                     state.policy_params, transitions,
                                                     key_alpha)
                alpha = jnp.exp(state.alpha_params)
            else:
                alpha = config.entropy_coefficient

            if not config.use_gcbc:
                (critic_loss, critic_metrics), critic_grads = critic_grad(
                    state.q_params, state.policy_params, state.target_q_params,
                    transitions, key_critic)

            actor_loss, actor_grads = actor_grad(state.policy_params, state.q_params,
                                                 alpha, transitions, key_actor)

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

                new_target_q_params = jax.tree_map(
                    lambda x, y: x * (1 - config.tau) + y * config.tau,
                    state.target_q_params, q_params)
                metrics = critic_metrics

            metrics.update({
                'critic_loss': critic_loss,
                'actor_loss': actor_loss,
            })

            new_state = TrainingState(
                policy_optimizer_state=policy_optimizer_state,
                q_optimizer_state=q_optimizer_state,
                policy_params=policy_params,
                q_params=q_params,
                target_q_params=new_target_q_params,
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

        def make_initial_state(key):
            """Initialises the training state (parameters and optimiser state)."""
            key_policy, key_q, key = jax.random.split(key, 3)

            policy_params = networks.policy_network.init(key_policy)
            policy_optimizer_state = policy_optimizer.init(policy_params)

            q_params = networks.q_network.init(key_q)
            q_optimizer_state = q_optimizer.init(q_params)

            state = TrainingState(
                policy_optimizer_state=policy_optimizer_state,
                q_optimizer_state=q_optimizer_state,
                policy_params=policy_params,
                q_params=q_params,
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

    def step(self):
        with jax.profiler.StepTraceAnnotation('step', step_num=self._counter):
            sample = next(self._iterator)
            transitions = types.Transition(*sample.data)

            sample2 = next(self._iterator)
            transitions2 = types.Transition(*sample2.data)

            double_transitions = jax.tree_map(lambda x, y: np.stack([x, y], axis=1),
                                              transitions, transitions2)

            # sample3 = next(self._iterator)
            # transitions3 = types.Transition(*sample3.data)
            #
            # triple_transitions = jax.tree_map(lambda x, y, z: np.stack([x, y, z], axis=1),
            #                                   transitions, transitions2, transitions3)

            self._state, metrics = self._update_step(self._state, double_transitions)
            # self._state, metrics = self._update_step(self._state, triple_transitions)

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

    def get_variables(self, names):
        variables = {
            'policy': self._state.policy_params,
            'critic': self._state.q_params,
        }
        return [variables[name] for name in names]

    def save(self):
        return self._state

    def restore(self, state):
        self._state = state
