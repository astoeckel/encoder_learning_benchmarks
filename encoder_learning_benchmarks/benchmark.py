#  Encoder Learning Benchmark
#  Copyright (C) 2020 Andreas Stöckel
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.

import sys

from .common import *


def print_progress(i_epoch, n_epochs, err_training, err_validation):
    fmt_str = "\r{:5d}/{:d} (err_training={:0.4f}, err_validation={:0.4f})"
    sys.stdout.write(
        fmt_str.format(i_epoch, n_epochs, err_training, err_validation))
    if i_epoch == n_epochs:
        sys.stdout.write("\n")


def print_json_progress(i_epoch, n_epochs, err_training, err_validation):
    fmt_str = '{{"type": "progress", "i": {}, "n": {}}}\n'
    sys.stdout.write(fmt_str.format(i_epoch, n_epochs))
    sys.stdout.flush()


def run_single_trial(optimizer,
                     dataset,
                     network,
                     decoder_learner,
                     encoder_learner=None,
                     passthrough=None,
                     rng=np.random,
                     epoch_size=None,
                     batch_size=100,
                     sequential=False,
                     n_epochs=1000,
                     n_smpls_per_epoch=10000,
                     compute_test_error=False,
                     progress=print_progress,
                     callback=None):
    # Make sure the given arguments are sane
    assert isinstance(dataset, Dataset)
    assert isinstance(network, Network)
    assert isinstance(decoder_learner, DecoderLearningRule)
    assert (encoder_learner is None) or (isinstance(encoder_learner,
                                                    EncoderLearningRule))
    assert (passthrough is None) or (isinstance(passthrough, Network))
    assert (epoch_size is None) or (epoch_size > 0)
    assert batch_size > 0
    assert n_epochs > 0

    # Create a new private rng
    rng = mkrng(rng)

    # Fetch important dimensions
    n_dim_in = dataset.n_dim_in
    n_dim_hidden = network.n_dim_hidden
    n_dim_out = dataset.n_dim_out

    # Try to determine a reasonable epoch size
    if epoch_size is None:
        if dataset.is_finite:
            epoch_size = dataset.n_max_smpls_training
        else:
            epoch_size = n_smpls_per_epoch
    if dataset.is_finite:
        epoch_size = min(dataset.n_max_smpls_training, epoch_size)

    # The batch size may not be larger than the epoch size
    batch_size = min(epoch_size, batch_size)
    assert (epoch_size // batch_size) > 0

    # Create the error vectors
    errs_training = np.ones(n_epochs) * np.nan
    errs_validation = np.ones(n_epochs) * np.nan

    def sample(sample_validation=False, sample_test=False):
        # Initialize the returned variables
        xs_trn, ys_trn, xs_val, ys_val, xs_test, ys_test = [None] * 6

        # Compute the number of samples to sample from the training, validation,
        # and test set
        N, finite = epoch_size, dataset.is_finite
        n_smpls_trn = min(N, dataset.n_max_smpls_training) if finite else N
        n_smpls_val = min(N, dataset.n_max_smpls_validation) if finite else N
        n_smpls_test = min(N, dataset.n_max_smpls_test) if finite else N

        # Sample and return the individual datasets
        xs_trn, ys_trn = dataset.sample(n_smpls_trn, "training")
        if sample_validation:
            xs_val, ys_val = dataset.sample(n_smpls_val, "validation")
        if sample_test:
            xs_test, ys_test = dataset.sample(n_smpls_test, "test")
        return xs_trn, ys_trn, xs_val, ys_val, xs_test, ys_test

    def eval_net(xs, D):
        As = network.activities(xs)
        if not passthrough is None:
            As = passthrough.activities(As)
        return As, As @ D.T

    def eval_net_and_errs(xs, ys, D):
        As, ys_hat = eval_net(xs, D)
        return As, ys_hat - ys

    def merge(lhs, rhs):
        assert len(lhs) == len(rhs)
        for i in range(len(lhs)):
            if not rhs[i] is None:
                lhs[i] = rhs[i]
        return lhs

    # Initialize the decoder matrix
    D = np.zeros((n_dim_out, n_dim_hidden))
    if passthrough is None:
        Jpt = None

    # Add the decoder to the parameter map
    params = {"D": D, **network.params}
    params_initial = copy.deepcopy(params)

    # Initialize the individual dataset samples
    xs_trn, ys_trn, xs_val, ys_val, xs_test, ys_test = samples = [None] * 6

    # Iterate over all epochs
    old_errs_settings = np.seterr(**{
        'divide': 'raise',
        'over': 'raise',
        'under': 'ignore',
        'invalid': 'raise'
    })

    try:
        for i_epoch in range(n_epochs):
            # If this is the first epoch or the dataset is infinite, sample new
            # data. Note that we will not override the test and validation set
            # (this is the purpose of the "merge" defined function above).
            first_epoch = i_epoch == 0
            if (i_epoch == 0) or (not dataset.is_finite):
                xs_trn, ys_trn, xs_val, ys_val, xs_test, ys_test = merge(
                    samples,
                    sample(first_epoch, compute_test_error and first_epoch))

            # Generate the batch indices
            if (not encoder_learner is None
                ) or decoder_learner.returns_gradient:
                n_batches = epoch_size // batch_size
                batch_idcs = np.arange(n_batches * batch_size)
                if not sequential:  # Do not shuffle if the samples are in a sequence
                    rng.shuffle(batch_idcs)
                batch_idcs = batch_idcs.reshape(n_batches, batch_size)
            else:
                # If we neither have an encoder learner, nor does our decoder
                # learner support mini batches, just go to the next epoch.
                n_batches = 0

            # Iterate over all mini-batches
            for i_batch in range(n_batches):
                # Fetch the training data for this batch
                idcs = batch_idcs[i_batch]
                xs_trn_batch, ys_trn_batch = xs_trn[idcs], ys_trn[idcs]

                # Compute the activities of the network for the given input
                As, errs = eval_net_and_errs(xs_trn_batch, ys_trn_batch, D)

                # If the decoder learner returns a gradient, update the decoder
                # at this point in time.
                dparams = {}
                if decoder_learner.returns_gradient:
                    dparams["D"] = decoder_learner.step(
                        As, ys_trn_batch, errs, D)

                # Compute the Jacobian corresponding to the passthough network
                if not passthrough is None:
                    Jpt = passthrough.jacobian(network.activities(xs_trn_batch))

                # Update the network parameters
                if not encoder_learner is None:
                    dparams.update(
                        encoder_learner.step(As, xs_trn_batch, errs, D, Jpt,
                                             network))

                # Update the parameters
                optimizer.step(params, dparams)

                # Normalise the network parameters
                network.normalize_params()

                # If the decoder learner does not return a gradient, perform an
                # update now. This ensures that the callback below will always
                # see a decoder that is optimized for the updated decoders.
                if not decoder_learner.returns_gradient:
                    As_global, errs_global = \
                        eval_net_and_errs(xs_trn, ys_trn, D)
                    D[...] = \
                        decoder_learner.step(As_global, ys_trn, errs_global, D)

                # If a callback was given, call it with information about the
                # current batch -- this is the place into which the animation
                # code is hooked
                if not callback is None:
                    callback({
                        "i_epoch": i_epoch,
                        "i_batch": i_batch,
                        "n_epochs": n_epochs,
                        "n_batches": n_batches,
                        "xs_trn_batch": xs_trn_batch,
                        "ys_trn_batch": ys_trn_batch,
                        "xs_trn": xs_trn,
                        "ys_trn": ys_trn,
                        "D": D,
                        "network": network,
                        "eval_net": eval_net,
                        "eval_net_and_errs": eval_net_and_errs,
                    })

            # If the decoder was not updated in lockstep with the encoders, update
            # the decoders before computing the final epoch error
            if (encoder_learner is None) and (not decoder_learner.returns_gradient):
                As, errs = eval_net_and_errs(xs_trn, ys_trn, D)
                D[...] = decoder_learner.step(As, ys_trn, errs, D)

            # Compute the validation and training error after the update
            _, ys_trn_hat = eval_net(xs_trn, D)
            _, ys_val_hat = eval_net(xs_val, D)
            errs_training[i_epoch] = dataset.error(ys_trn, ys_trn_hat)
            errs_validation[i_epoch] = dataset.error(ys_val, ys_val_hat)

            if not progress is None:
                progress(i_epoch + 1, n_epochs, errs_training[i_epoch],
                         errs_validation[i_epoch])

        # If so desired, compute the test error
        err_test = None
        if compute_test_error:
            ys_test_hat = network.activities(xs_test) @ D.T
            err_test = dataset.error(ys_test, ys_test_hat)

    except (FloatingPointError, np.linalg.LinAlgError) as e:
        raise e
        # Something broke. Write NaN values to the result files to indicate
        # this.
        errs_training[i_epoch:] = np.nan
        errs_validation[i_epoch] = np.nan
        err_test = np.nan
    finally:
        np.seterr(**old_errs_settings)

    # Assemble the result array
    res = {
        "epochs": np.arange(1, n_epochs + 1),
        "n_epochs": n_epochs,
        "errs_training": errs_training,
        "errs_validation": errs_validation,
        "err_test": err_test,
    }
    for param_key in params.keys():
        res["p_initial_" + param_key] = params_initial[param_key]
        res["p_final_" + param_key] = params[param_key]
    return res

