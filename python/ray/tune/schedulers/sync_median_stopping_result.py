from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import logging
import numpy as np

from ray.tune.trial import Trial
from ray.tune.schedulers.trial_scheduler import FIFOScheduler, TrialScheduler
from ray.tune.util import flatten_dict
from ray.tune.trial import Trial, Checkpoint

logger = logging.getLogger(__name__)


class SyncMedianStoppingResult(FIFOScheduler):
    """
    Args:
        time_attr (str): The training result attr to use for comparing time.
            Note that you can pass in something non-temporal such as
            `training_iteration` as a measure of progress, the only requirement
            is that the attribute should increase monotonically.
        metric (str): The training result objective value attribute. Stopping
            procedures will use this attribute.
        mode (str): One of {min, max}. Determines whether objective is
            minimizing or maximizing the metric attribute.
        grace_period (float): Only stop trials at least this old in time.
            The units are the same as the attribute named by `time_attr`.
        eval_interval (float): Compare trial median metric at this interval of time.
            The units are the same as the attribute named by `time_attr`.
        min_samples_required (int): Min samples to compute median over.
        verbose (bool): If True, will output the median and best result each
            time a trial reports. Defaults to True.
        running_window_size (float): Median is constructed from means of trial metric,
            the mean is constructed from the last running_window_size results. None
            implies use all results. To ensure results include a complete set of
            size running_window_size, set grace_period >= running_window_size.
            The units are the same as the attribute named by `time_attr`.
    """
    def __init__(self,
                 time_attr="time_total_s",
                 reward_attr=None,
                 metric="episode_reward_mean",
                 mode="max",
                 grace_period=60.0,
                 eval_interval=600.0,
                 min_samples_required=3,
                 verbose=True,
                 running_window_size= None):

        assert mode in ["min", "max"], "`mode` must be 'min' or 'max'!"

        if reward_attr is not None:
            mode = "max"
            metric = reward_attr
            logger.warning(
                "`reward_attr` is deprecated and will be removed in a future "
                "version of Tune. "
                "Setting `metric={}` and `mode=max`.".format(reward_attr))

        FIFOScheduler.__init__(self)
        self._stopped_trials = set()
        self._completed_trials = set()
        self._grace_period = grace_period
        self._eval_interval = eval_interval
        self._min_samples_required = min_samples_required
        self._metric = metric
        self._mode = mode
        self._time_attr = time_attr
        self._verbose = verbose
        self._running_window_size = running_window_size

        self._results = collections.defaultdict(list)
        self._last_result_times = collections.defaultdict(int)
        self._last_pause_times = collections.defaultdict(int)
        self._evaluated_interval = 0



    def fetch_lowest_common_interval(self):
        completed_intervals = [int(value/self._eval_interval)
             for trial, value in self._last_result_times.items() if
             trial not in self._stopped_trials]
        return min(completed_intervals)


    def on_trial_result(self, trial_runner, trial, result):
        """Callback for early stopping.

        This stopping rule stops a running trial if the trial's best objective
        value over running_window_size at step `t` is strictly worse than the
        median of the averages of the running trials'
        objectives reported over same running_window_size at step `t`.
        It does not evaluate the trails until all trials have completed up to step `t`.
        """

        if trial in self._stopped_trials:
            # This stops trials at completion of iteration
            return TrialScheduler.STOP  # fall back to FIFO

        result_time = result[self._time_attr]
        self._results[trial].append(result)
        self._last_result_times[trial] = result_time

        # Least common evaluation interval across trials
        lc_interval = self.fetch_lowest_common_interval()

        _trials = [_trial for _trial in trial_runner.get_trials()
            if _trial not in self._stopped_trials]

        _return = TrialScheduler.CONTINUE
        if  (lc_interval*self._eval_interval > self._grace_period) \
                and (lc_interval > self._evaluated_interval) \
                and len(_trials)>=self._min_samples_required:

            _means, _best_results = self._get_trial_metrics(_trials,
                                                            lc_interval * self._eval_interval)
            _median = np.nanmedian(_means)

            if self._verbose:
                for idx, _trial in enumerate(_trials):
                    logger.info("MedianStoppingResult: "
                                "trial {} best res={} vs median res={} at t={}".format(
                                _trial, _best_results[idx], _median, lc_interval*self._eval_interval))

            if self._mode == "max":
                _trials_to_stop = [best_result < _median for best_result in _best_results]
                _trials_to_stop = np.array(_trials)[_trials_to_stop]
            else:
                _trials_to_stop = [best_result > _median for best_result in _best_results]
                _trials_to_stop = np.array(_trials)[_trials_to_stop]

            _trials_to_stop = [_trial for _trial in _trials_to_stop if
                                _trial not in self._stopped_trials or
                                _trial not in self._completed_trials]
            for _trial in _trials_to_stop:
                if self._verbose:
                    logger.info("MedianStoppingResult: "
                                "early stopping {} at t={}".format(_trial,
                                    lc_interval*self._eval_interval))
                # Add trial to _stopped_trials list, will be formally stopped
                # after returning its current iteration at top of this method,
                self._stopped_trials.add(_trial)

                # If current trial is being stopped, stop it immediately
                if _trial == trial:
                    _return = TrialScheduler.STOP
            self._evaluated_interval += 1

        _waiting = [_trial.status in [Trial.PENDING, Trial.PAUSED] for _trial in _trials]

        # Only pause trials if there are waiting trials, if the trial is not
        # already being stopped, and if the current trial has completed at least
        # eval_interval time steps
        if (any(_waiting) and _return==TrialScheduler.CONTINUE and
            self._last_result_times[trial] - self._last_pause_times[trial]
                >= self._eval_interval):
            self._last_pause_times[trial] = result_time
            _return= TrialScheduler.PAUSE

        return _return

    def on_trial_complete(self, trial_runner, trial, result):
        self._results[trial].append(result)
        self._completed_trials.add(trial)

    def debug_string(self):
        return "Using MedianStoppingResult: num_stopped={}.".format(
            len(self._stopped_trials))

    def _get_trial_metrics(self, _trials, t_max=float("inf")):
        scores = []
        best_results = []

        for trial in _trials:
            _running_result, _best_result = self._running_result(trial, t_max)
            scores.append(_running_result)
            best_results.append(_best_result)

        return scores, best_results

    def _running_result(self, trial, t_max=float("inf")):
        results = self._results[trial]
        if self._running_window_size is not None:
            t_min = t_max - self._running_window_size
        else:
            t_min = 0
        scoped_results = results[t_min:t_max]
        if len(scoped_results) > 0:
            scoped_metrics = [flatten_dict(r)[self._metric] for r in scoped_results]
            compare_op = max if self._mode == "max" else min
            return np.mean(scoped_metrics), compare_op(scoped_metrics)
        else:
            return np.nan, float("inf") if self._mode == "max" else float("-inf")


    def choose_trial_to_run(self, trial_runner):
        """Ensures all trials get fair share of time (as defined by time_attr).
        This enables the scheduler to support a greater number of
        concurrent trials than can fit in the cluster at any given time.

        """
        candidates = []
        for trial in trial_runner.get_trials():
            if trial.status in [Trial.PENDING, Trial.PAUSED] and \
                    trial_runner.has_resources(trial.resources):
                candidates.append(trial)
        candidates.sort(
            key=lambda trial: self._last_result_times[trial])
        return candidates[0] if candidates else None