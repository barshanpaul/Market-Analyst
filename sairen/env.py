"""
Sairen environments.
"""

# Copyright (C) 2016  Doctor J
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import logging
import threading
import time
from queue import Queue
from collections import deque

import gym
import numpy as np
from gym.spaces import Box
from ibroke import IBroke, Bar, create_logger


__version__ = "0.3.1"
__all__ = ('MarketEnv', 'Bar')
# These are used to bound observation Boxes, not sure how important it really is.
MAX_INSTRUMENT_PRICE = 1e6
MAX_INSTRUMENT_VOLUME = 1e9
MAX_INSTRUMENT_QUANTITY = 20000
MAX_TRADE_SIZE = 1e6
MAX_TIME = time.time() + 10 * 365 * 24 * 60 * 60
BAR_BOUNDS = Bar(time=MAX_TIME, bid=MAX_INSTRUMENT_PRICE, bidsize=MAX_TRADE_SIZE, ask=MAX_INSTRUMENT_PRICE, asksize=MAX_TRADE_SIZE, last=MAX_INSTRUMENT_PRICE, lastsize=MAX_TRADE_SIZE, lasttime=MAX_TIME, open=MAX_INSTRUMENT_PRICE, high=MAX_INSTRUMENT_PRICE, low=MAX_INSTRUMENT_PRICE, close=MAX_INSTRUMENT_PRICE, vwap=MAX_INSTRUMENT_PRICE, volume=MAX_INSTRUMENT_VOLUME, open_interest=MAX_INSTRUMENT_VOLUME)
RENDER_HEADERS_EVERY_STEPS = 50     #: Print column names to stdout for human-rendered output every this many steps


class MarketEnv(gym.Env):
    """Access the Interactive Brokers trading API as an OpenAI Gym environment.

    ``MarketEnv`` provides observations of real-time market data for a single financial instrument.
    The action is a float in the range [-1, 1] to set the (absolute) target position in that instrument.

    Calling :meth:`close()` (or terminating Python) will cancel any open orders, flatten positions, and
    disconnect from IB.
    """

    action_space = Box(-1, 1, shape=(1,))
    """
    MarketEnv's action is a continuous float from -1 to 1 that sets the target position as a fraction of the
    environment's ``max_quantity`` parameter.  -1 means set the position to short ``max_quantity``, 0 means
    exit/close/flatten/no position, and 1 means set the position to long ``max_quantity``. These are "target" positions,
    so an action of 1 means "regardless of current position, buy or sell (or do nothing) as necessary to make my position
    ``max_quantity``."  Intermediate values are scaled by ``max_quantity`` and rounded to the nearest integer.  Orders
    are issued at market price so they are filled quickly.
    """
    metadata = {'render.modes': ['human']}

    # IBroke is event-driven (its methods are called asynchronously by IBPy), whereas Env is essentially an external
    # iterator (the caller calls step() when it's ready for the next observation). To play together, when IBroke's on_bar()
    # callback receives a new bar (observation), it's stored in a queue.  When Env.step() is called, it takes the next bar
    # out.  If no bars are available, step() will block waiting for a bar to appear in the queue. If more than one
    # bar is available, it means that step() is falling behind on processing bars, and a warning will be printed.

    def __init__(self, instrument, max_quantity=1, obs_type='time', obs_size=1, obs_xform=None, episode_steps=None, host='localhost', port=7497, client_id=None, loglevel=logging.INFO):
        """
        :param str,tuple instrument: ticker string or :class:`IBroke` ``(symbol, sec_type, exchange, currency, expiry, strike, opt_type)`` tuple.
        :param int max_quantity: The number of shares/contracts that will be bought (or sold) when the action is 1 (or -1).
        :param str obs_type: ``time`` for bars at regular intervals, or ``tick`` for bars at every quote change.
          Raw observations are numpy float ndarrays with the following fields::

                time, bid, bidsize, ask, asksize, last, lastsize, lasttime,
                open, high, low, close, vwap, volume, open_interest

          See the :class:`Bar` convenience namedtuple for detailed field descriptions.
        :param float obs_size: How often you get an observation in seconds.  Ignored for ``obs_type='tick'``.
        :param func obs_xform: Callable that takes a raw input observation array and transforms it,
          returning either another numpy array or ``None`` to indicate data is not ready yet.
        :param int,None episode_steps: Number of steps after ``reset()`` to run before returning `done`, or ``None`` to run indefinitely.
          The final step in an episode will have its action forced to close any open positions so PNL can be properly accounted.
        :param int client_id: A unique integer identifying which API client made an order.  Different instances of Sairen running at the same time must use
          different ``client_id`` values.  In order to discover and modify pre-existing open orders, you must use the same ``client_id`` the orders were created with.
        :param int loglevel: The `logging level <https://docs.python.org/3/library/logging.html#logging-levels>`_ to use.
        """
        super().__init__()
        self.log = create_logger('sairen', loglevel)
        assert 1 <= max_quantity <= MAX_INSTRUMENT_QUANTITY, max_quantity
        self.max_quantity = int(max_quantity)
        assert episode_steps is None or episode_steps > 0
        self.episode_steps = episode_steps
        self.obs_type = obs_type
        self.data_q = None      # Initialized in _reset
        self.profit = 0.0       # Since last step; zeroed every step
        self.episode_profit = 0.0     # Since last reset
        self.reward = None      # Save most recent reward so we can use it in render()
        self.raw_obs = None     # Raw market data from IBroke
        self.observation = None # Most recent transformed observation
        self.pos_desired = 0    # Action translated into target number of contracts
        self.done = True        # Start in the "please call reset()" state
        self.step_num = 0       # Count calls to step() since last reset()
        self.unrealized_gain = 0.0
        self._finish_on_next_step = False
        assert obs_xform is None or callable(obs_xform)
        self._xform = (lambda obs, *args: obs) if obs_xform is None else obs_xform         # Default xform is identity

        self.ib = IBroke(host=host, port=port, client_id=client_id, verbose=2)
        self.instrument = self.ib.get_instrument(instrument)
        self.log.info('Sairen %s trading %s up to %d contracts', __version__, self.instrument.tuple(), self.max_quantity)
        self.ib.register(self.instrument, on_bar=self._on_mktdata, bar_type=obs_type, bar_size=obs_size, on_order=self._on_order, on_alert=self._on_alert)
        self.observation_space = getattr(obs_xform, 'observation_space', Box(low=np.zeros(len(BAR_BOUNDS)), high=np.array(BAR_BOUNDS)))
        self.log.debug('XFORM %s', self._xform)
        self.log.debug('OBS SPACE %s', self.observation_space)
        np.set_printoptions(linewidth=9999)
        self.pos_actual = self.ib.get_position(self.instrument)     # Actual last reported number of contracts held
        self.act_start_time = None
        self.act_time = deque(maxlen=10)        # Track recent agent action times

    def _on_mktdata(self, instrument, bar):
        """Called by IBroke on new market data; transforms observation and, if ready, puts it in data_q."""
        self.log.debug('OBS RAW %s', bar)
        self.raw_obs = bar
        self.pos_actual = self.ib.get_position(self.instrument)
        self.unrealized_gain = self.pos_actual * self.instrument.leverage * ((bar.bid if self.pos_actual > 0 else bar.ask) - (self.ib.get_cost(self.instrument) or 0))     # If pos > 0, what could we sell for?  Assume buy at the ask, sell at the bid
        data = np.asarray(bar, dtype=float)
        obs = self._xform(data, self.unrealized_gain, self.pos_actual, self.max_quantity)
        self.log.debug('OBS XFORM %s', obs)

        if obs is not None and self.ib.connected and not self.done and self.data_q is not None:     # guard against step() being called before reset().  It also turns out that you can still receive market data while "disconnected"...
            self.data_q.put_nowait(obs)
            if self.data_q.qsize() > 1:
                self.log.warning('Your agent is falling behind! Observation queue contains %d items.', self.data_q.qsize())

    def _on_order(self, order):
        """Called when order status changes by IBroke."""
        self.log.debug('ORDER %s\t(thread %d)', order, threading.get_ident())
        self.profit += order.profit

    def _on_alert(self, instrument, msg):
        self.log.warning('ALERT: %s', msg)

    def flatten(self):
        """Cancel any open orders and close any positions."""
        self.ib.flatten(self.instrument)
        time.sleep(1)       # Give order time to fill

    def finish_on_next_step(self):
        """Sets a flag so that the next call to :meth:`step` will flatten any positions and return ``done = True``."""
        self._finish_on_next_step = True

    @property
    def info(self):
        """A dict of information useful for monitoring the environment."""
        return {
            'step': self.step_num,
            'episode_profit': self.episode_profit,
            'position_desired': self.pos_desired,
            'position_actual': self.ib.get_position(self.instrument),
            'unrealized_gain': self.unrealized_gain,
            'avg_cost': self.ib.get_cost(self.instrument) or 0.0,
            'agent_time_last': self.act_time[-1] if self.act_time else np.nan,
            'agent_time_avg': np.mean(self.act_time) if self.act_time else np.nan,
        }

    def _close(self):
        """Cancel open orders, flatten position, and disconnect."""
        self.log.info('Cancelling, closing, disconnecting.')
        if hasattr(self, 'ib'):     # We may not have ever connected, but _close gets called atexit anyway.
            self.done = True        # Stop observations going into the queue
            self.flatten()
            self.ib.disconnect()

    def _reset(self):
        """Flatten positions, reset accounting, and return the first bar."""
        self.log.debug('RESET')
        self.done = True        # Prevent _on_mktdata() from putting things in the queue and triggering step() while we flatten
        self.flatten()
        self.profit = 0.0
        self.episode_profit = 0.0
        self.reward = 0.0
        self.unrealized_gain = 0.0
        self.observation = None
        self.done = False
        self.action = 0.0
        self.pos_desired = 0
        self._finish_on_next_step = False
        self.step_num = 0
        self.data_q = Queue()
        self.observation = self.data_q.get()       # Blocks until bar ready
        self.act_start_time = time.time()
        return self.observation

    def _step(self, action):
        """Execute any trades necessary to allocate our position to the float `action` in [-1, 1] (short, long),
        wait for the next cooked observation, and return the observation and reward."""
        self.act_time.append(time.time() - self.act_start_time)
        self.step_num += 1
        self.log.debug('STEP {}: {}\t({:.2f}s)'.format(self.step_num, action, self.act_time[-1]))
        if self.done:
            raise ValueError("I'm done, yo.  Call reset() if you want another play.")

        # If last step, set action to flatten, done = True
        done = False
        if self._finish_on_next_step or (self.episode_steps is not None and self.step_num >= self.episode_steps):
            action = 0.0
            done = True     # Don't set self.done before waiting on self.data_q, because it will never put anything in.

        self.action = float(action)        # Save raw un-clipped action (but make sure it's a float)
        action = np.clip(action, self.action_space.low, self.action_space.high)
        assert self.action_space.contains(action), 'action {}, low {}, high {}'.format(action, self.action_space.low, self.action_space.high)       # requires an array
        action = np.asscalar(action)

        # Issue order to take action
        self.ib.cancel_all(self.instrument)
        position = self.ib.get_position(self.instrument)
        open_orders = sum(1 for _ in self.ib.get_open_orders())
        # Try to prevent orders and/or positions piling up when things get busy.
        if abs(position) <= self.max_quantity + 1 and open_orders <= 2:
            self.pos_desired = int(round(action * self.max_quantity))
            self.log.debug('ORDER TARGET %d', self.pos_desired)
            self.ib.order_target(self.instrument, self.pos_desired)
        else:
            self.log.warning('Constipation: position %d, %d open orders, skipping action.', position, open_orders)

        if done:
            # TODO: Actually wait until order settles.
            time.sleep(1)       # Wait for final close order to fill

        self.reward = self.profit       # Reward is profit since last step
        self.episode_profit += self.profit
        self.profit = 0
        self.observation = self.data_q.get()       # block until next obs ready

        self.done = done        # Don't set until after waiting on queue, or queue will never get filled.
        info = self.info
        self.log.debug('OBS %s\tINFO %s', self.observation, info)
        self.log.debug('REWARD %.2f\tDONE %s', self.reward, self.done)
        self.act_start_time = time.time()
        return self.observation, self.reward, self.done, info

    def _render(self, mode='human', close=False):
        if mode == 'human':
            if not close:
                if self.instrument.sec_type == 'CASH':
                    FIELDS = (
                        ('time', '{time}', 8, 'UTC observation timestamp'),
                        ('step', '{step:d}', '>5', 'Step number in this episode (first action is step 1)'),
                        ('pnl', '{pnl:.2f}', '>7', 'Episode profit'),
                        ('unreal', '{unreal:.2f}', '>7', 'Episode unrealized gain'),
                        ('reward', '{reward:.2f}', '>7', 'Last reward'),
                        ('action', '{action: 6.2f}', '>6', 'Last action (raw float)'),
                        ('position', '{pos: 4d}@{cost:<7.5f}', '>15', 'Actual shares/contracts currently held'),
                        ('bid/ask', '{bid:7.5f}/{ask:<7.5f}', '>21', 'Most recent bid and ask prices'))
                else:
                    FIELDS = (
                        ('time', '{time}', 8, ''),
                        ('step', '{step:d}', '>5', ''),
                        ('pnl', '{pnl:.2f}', '>7', ''),
                        ('unreal', '{unreal:.2f}', '>7', ''),
                        ('reward', '{reward:.2f}', '>7', ''),
                        ('action', '{action: 6.2f}', '>6', ''),
                        ('position', '{pos: 4d}@{cost:<7.2f}', '>12', ''),
                        ('bid/ask', '{bid:7.2f}/{ask:<7.2f}', '>15', ''),
                        ('sizes', '{bidsize:4.0f}x{asksize:<4.0f}', '>9', 'Most recent bid and ask sizes'),
                        ('last', '{last:7.2f}@{lastsize:<4.0f}', '>12', 'Most recent trade price'),
                        ('volume', '{volume:>8.0f}', '>8', 'Total cumulative volume for the day'))

                if self.step_num % RENDER_HEADERS_EVERY_STEPS == 1:
                    print(*('{:{}}'.format(name, width) for name, _, width, _ in FIELDS))
                data = dict(self.raw_obs._asdict(), step=self.step_num, reward=self.reward, unreal=self.unrealized_gain, action=self.action, pnl=self.episode_profit, pos=int(self.pos_actual), cost=self.info['avg_cost'] or 0.0, raw_obs=self.raw_obs, time=datetime.datetime.utcfromtimestamp(round(self.raw_obs[0])).time())
                print(*('{:{}}'.format(fmt.format(**data), width) for _, fmt, width, _ in FIELDS))
                self.log.debug('INFO %s', sorted(self.info.items()))
        else:
            raise NotImplementedError("Render mode '{}' not implemented".format(mode))

    def _seed(self, seed=None):
        raise Warning("Don't you wish you could seed() the stock market!")
