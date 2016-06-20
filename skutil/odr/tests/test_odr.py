from __future__ import print_function, division
import numpy as np
from numpy.testing import (assert_array_equal, assert_almost_equal, assert_array_almost_equal)
from sklearn.datasets import load_iris
from skutil.odr import *
from skutil.utils.tests import assert_fails

X = load_iris().data

def test_qr():
	# test just the decomp first
	q = QRDecomposition(X)
	aux = q.qraux
	assert_array_almost_equal(aux, np.array([ 1.07056264,  1.0559255,   1.03857984,  1.04672249]))

	# test that we can get the rank
	assert q.get_rank() == 4

	# test that we can get the R matrix and that it's rank 4
	assert q.get_R_rank() == 4

	# next, let's test that we can get the coefficients:
	coef = q.get_coef(X)
	assert_array_almost_equal(coef, np.array(
		[[  1.00000000e+00,   1.96618714e-16,  -0.00000000e+00,  -2.00339858e-16],
		 [  3.00642915e-16,   1.00000000e+00,  -0.00000000e+00,   1.75787325e-16],
		 [ -4.04768123e-16,   4.83060041e-17,   1.00000000e+00,   4.23545747e-16],
		 [ -1.19866575e-16,  -1.74365433e-17,   1.10216442e-17,   1.00000000e+00]]
	))

	# ensure dimension error
	assert_fails(q.get_coef, ValueError, X[:140,:])
