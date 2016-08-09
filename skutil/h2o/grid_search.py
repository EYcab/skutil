from __future__ import division, print_function, absolute_import
from abc import ABCMeta, abstractmethod
import warnings
import time
import numpy as np
import pandas as pd

import h2o
from h2o.frame import H2OFrame
from h2o import H2OEstimator

from .pipeline import H2OPipeline
from .base import _check_is_frame, BaseH2OFunctionWrapper
from ..utils import is_numeric
from ..grid_search import _CVScoreTuple, _check_param_grid
from .split import *

from sklearn.externals.joblib import logger
from sklearn.base import clone, MetaEstimatorMixin
from sklearn.utils.validation import check_is_fitted
from sklearn.externals import six
from sklearn.utils.metaestimators import if_delegate_has_method
from sklearn.metrics import (accuracy_score,
							 explained_variance_score,
							 f1_score,
							 log_loss,
							 mean_absolute_error,
							 mean_squared_error,
							 median_absolute_error,
							 precision_score,
							 r2_score,
							 recall_score)

# >= sklearn 0.18
try:
	from sklearn.model_selection import ParameterSampler, ParameterGrid
except ImportError as i:
	from sklearn.grid_search import ParameterSampler, ParameterGrid


__all__ = [
	'H2OGridSearchCV',
	'H2ORandomizedSearchCV'
]


"""These parameters are ones h2o stores
that we don't necessarily want to clone.
"""

IGNORE = set([
	'model_id',
	'fold_column',
	'fold_assignment',
	'keep_cross_validation_predictions',
	'offset_column',
	'checkpoint',
	'training_frame',
	'validation_frame',
	'response_column',
	'ignored_columns',
	'max_confusion_matrix_size',
	'score_each_iteration',
	'histogram_type',
	'col_sample_rate',
	'stopping_metric',
	'weights_column',
	'stopping_rounds',
	'col_sample_rate_change_per_level',
	'max_hit_ratio_k',
	'nbins_cats',
	'class_sampling_factors',
	'ignore_const_cols',
	'keep_cross_validation_fold_assignment'
])


SCORERS = {
	'accuracy_score' : accuracy_score,
	'explained_variance_score' : explained_variance_score,
	'f1_score' : f1_score,
	'log_loss' : log_loss,
	'mean_absolute_error' : mean_absolute_error,
	'mean_squared_error' : mean_squared_error,
	'median_absolute_error' : median_absolute_error,
	'precision_score' : precision_score,
	'r2_score' : r2_score,
	'recall_score' : recall_score
}


def _clone_h2o_obj(estimator):
	est = clone(estimator)

	if isinstance(estimator, H2OPipeline):
		for i, step in enumerate(est.steps):
			# the step from the original estimator
			e = estimator.steps[i][1]

			if isinstance(e, H2OEstimator):
				# so it's the last step
				parms = e._parms
				for k,v in six.iteritems(parms):
					k = str(k) # h2o likes unicode...

					if (not k in IGNORE) and (not v is None):
						step[1]._parms[k] = v
			else:
				# otherwise it's an H2OFunctionTransformer
				pass

	return est


def _score(estimator, frame, target_feature, scorer, parms):
	# this is a bottleneck:
	y_truth = frame[target_feature].as_data_frame(use_pandas=True)[target_feature].tolist()

	# gen predictions...
	pred = estimator.predict(frame)
	return scorer(y_truth, pred, **parms)


def _fit_and_score(estimator, frame, feature_names, target_feature,
				   scorer, parameters, verbose, scoring_params,
				   train, test):
	
	if verbose > 1:
		if parameters is None:
			msg = ''
		else:
			msg = '%s' % (', '.join('%s=%s' % (k,v)
									 for k, v in parameters.items()))
		print("[CV] %s %s" % (msg, (64 - len(msg)) * '.'))

	# set the params for this estimator -- also set feature_names, target_feature
	if not isinstance(estimator, (H2OEstimator, H2OFunctionTransformer)):
		raise TypeError('estimator must be either an H2OEstimator '
						'or a H2OFunctionTransformer but got %s'
						% type(estimator))

	#it's probably a pipeline
	is_h2o_est = False
	if isinstance(estimator, H2OFunctionTransformer): 
		estimator.set_params(**parameters)
		setattr(estimator, 'feature_names', feature_names)
		setattr(estimator, 'target_feature',target_feature)

	else: # it's just an H2OEstimator
		is_h2o_est = True
		parm_dict = {}
		for k, v in six.iteritems(parameters):
			if '__' in k:
				raise ValueError('only one estimator passed to grid search, '
								 'but multiple named parameters passed: %s' % k)

			# {parm_name : v}
			parm_dict[k] = v

		# set the h2o estimator params. 
		for parm, value in six.iteritems(parm_dict):
			estimator._parms[parm] = value


	start_time = time.time()

	# generate split
	train_frame = frame[train, :]
	test_frame = frame[test, :]

	# do training:
	if not is_h2o_est:
		estimator.fit(train_frame)
	else:
		estimator.train(training_frame=train_frame, x=feature_names, y=target_feature)


	test_score = _score(estimator, test_frame, target_feature, scorer, scoring_params)
	scoring_time = time.time() - start_time

	if verbose > 2:
		msg += ', score=%f' % test_score
	if verbose > 1:
		end_msg = '%s -%s' % (msg, logger.short_format_time(scoring_time))
		print('[CV] %s %s' % ((64 - len(end_msg)) * '.', end_msg))

	return [test_score, test.shape[0], estimator, parameters]


class BaseH2OSearchCV(BaseH2OFunctionWrapper):

	__min_version__ = '3.8.3'
	__max_version__ = None
	
	@abstractmethod
	def __init__(self, estimator, feature_names,
				 target_feature, scoring=None, 
				 n_jobs=1, scoring_params=None, 
				 cv=5, verbose=0, iid=True, refit=True):

		super(BaseH2OSearchCV, self).__init__(target_feature=target_feature,
											  min_version=self.__min_version__,
											  max_version=self.__max_version__)

		self.estimator = estimator
		self.feature_names = feature_names
		self.scoring = scoring
		self.n_jobs = n_jobs
		self.scoring_params = scoring_params if not scoring_params is None else {}
		self.cv = cv
		self.verbose = verbose
		self.iid = iid
		self.refit = refit

	def _fit(self, X, parameter_iterable):
		"""Actual fitting,  performing the search over parameters."""
		X = _check_is_frame(X) # if it's a frame, will be turned into a matrix

		estimator = self.estimator

		# we need to require scoring...
		scoring = self.scoring
		if not scoring:
			raise ValueError('require string or callable for scoring')
		elif isinstance(scoring, str):
			if not scoring in SCORERS:
				raise ValueError('Scoring must be one of (%s) or a callable. '
								 'Got %s' % (', '.join(SCORERS.keys()), scoring))
			self.scorer_ = SCORERS[scoring]
		# else we'll let it fail through if it's a bad callable
		else:
			self.scorer_ = scoring

		# validate CV
		cv = check_cv(self.cv)
		base_estimator = _clone_h2o_obj(self.estimator)

		# do fits, scores
		out = [
			_fit_and_score(estimator=_clone_h2o_obj(base_estimator),
						   frame=X, feature_names=self.feature_names,
						   target_feature=self.target_feature,
						   scorer=self.scorer_, parameters=params,
						   verbose=self.verbose, scoring_params=self.scoring_params,
						   train=train, test=test)
			for params in parameter_iterable
			for train, test in cv.split(frame)
		]

		# Out is a list of quad: score, n_test_samples, estimator, parameters
		n_fits = len(out)
		n_folds = cv.get_n_splits()

		scores = list()
		grid_scores = list()
		for grid_start in range(0, n_fits, n_folds):
			n_test_samples = 0
			score = 0
			all_scores = []
			for this_score, this_n_test_samples, _, parameters in \
					out[grid_start:grid_start + n_folds]:
				all_scores.append(this_score)
				if self.iid:
					this_score *= this_n_test_samples
					n_test_samples += this_n_test_samples
				score += this_score
			if self.iid:
				score /= float(n_test_samples)
			else:
				score /= float(n_folds)
			scores.append((score, parameters))
			# TODO: shall we also store the test_fold_sizes?
			grid_scores.append(_CVScoreTuple(
				parameters,
				score,
				np.array(all_scores)))
		# Store the computed scores
		self.grid_scores_ = grid_scores

		# Find the best parameters by comparing on the mean validation score:
		# note that `sorted` is deterministic in the way it breaks ties
		best = sorted(grid_scores, key=lambda x: x.mean_validation_score,
					  reverse=True)[0]
		self.best_params_ = best.parameters
		self.best_score_ = best.mean_validation_score

		if self.refit:
			# fit the best estimator using the entire dataset
			# clone first to work around broken estimators
			best_estimator = _clone_h2o_obj(base_estimator)

			# set params -- remember h2o gets funky with this...
			if isinstance(best_estimator, H2OEstimator):
				for k,v in six.iteritems(best.parameters):
					best_estimator._parms[k] = v
				best_estimator.train(training_frame=X, x=self.feature_names, y=self.target_feature)
			else:
				best_estimator.set_params(**best.parameters)
				best_estimator.fit(X)

			self.best_estimator_ = best_estimator

		return self

	def score(self, frame):
		check_is_fitted(self, 'best_estimator_')
		return _score(self.best_estimator_, frame, self.target_feature, self.scorer_, self.scoring_params)

	@if_delegate_has_method(delegate='estimator')
	def predict(self, frame):
		frame = _check_is_frame(frame)
		return self.best_estimator_.predict(frame)

	@if_delegate_has_method(delegate='estimator')
	def transform(self, frame):
		frame = _check_is_frame(frame)
		return self.best_estimator_.transform(frame)

	

class H2OGridSearchCV(BaseH2OSearchCV):

	def __init__(self, estimator, param_grid, 
				 feature_names, target_feature, 
				 scoring=None, n_jobs=1, 
				 scoring_params=None, cv=5, 
				 verbose=0, iid=True, refit=True):

		super(H2OGridSearchCV, self).__init__(
				estimator=estimator,
				feature_names=feature_names,
				target_feature=target_feature,
				scoring=scoring, n_jobs=n_jobs,
				scoring_params=scoring_params,
				cv=cv, verbose=verbose,
				iid=iid, refit=refit
			)

		self.param_grid = param_grid
		_check_param_grid(param_grid)

	def fit(self, frame):
		return self._fit(frame, ParameterGrid(self.param_grid))


class H2ORandomizedSearchCV(BaseH2OSearchCV):

	def __init__(self, estimator, param_grid,
				 feature_names, target_feature, 
				 n_iter=10, random_state=None,
				 scoring=None, n_jobs=1, 
				 scoring_params=None, cv=5, 
				 verbose=0, iid=True, refit=True):

		super(H2ORandomizedSearchCV, self).__init__(
				estimator=estimator,
				feature_names=feature_names,
				target_feature=target_feature,
				scoring=scoring, n_jobs=n_jobs,
				scoring_params=scoring_params,
				cv=cv, verbose=verbose,
				iid=iid, refit=refit
			)

		self.param_grid = param_grid
		self.n_iter = n_iter
		self.random_state = random_state

	def fit(self, frame):
		sampled_params = ParameterSampler(self.param_grid,
										  self.n_iter,
										  random_state=self.random_state)

		return self._fit(frame, sampled_params)

