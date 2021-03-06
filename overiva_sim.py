# Copyright (c) 2019 Robin Scheibler
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
This file contains the code to run the systematic simulation for evaluation
of overiva and other algorithms.
"""
import argparse, json, os, sys
import numpy as np
import pyroomacoustics as pra
import rrtools

from routines import (
    PlaySoundGUI,
    grid_layout,
    semi_circle_layout,
    random_layout,
    gm_layout,
)

# Get the data if needed
from get_data import get_data, samples_dir
get_data()

# Routines for manipulating audio samples
sys.path.append(samples_dir)
from generate_samples import sampling, wav_read_center

# find the absolute path to this file
base_dir = os.path.abspath(os.path.split(__file__)[0])


def init(parameters):
    parameters["base_dir"] = base_dir


def one_loop(args):
    global parameters

    import time
    import numpy

    np = numpy

    import pyroomacoustics

    pra = pyroomacoustics

    import sys

    sys.path.append(parameters["base_dir"])

    from routines import semi_circle_layout, random_layout, gm_layout, grid_layout
    from overiva import overiva
    from ive import ogive
    from auxiva_pca import auxiva_pca

    # import samples helper routine
    from get_data import samples_dir
    sys.path.append(samples_dir)
    from generate_samples import wav_read_center

    n_targets, n_mics, rt60, sinr, wav_files, seed = args

    # this is the underdetermined case. We don't do that.
    if n_mics < n_targets:
        return []

    # set MKL to only use one thread if present
    try:
        import mkl

        mkl.set_num_threads(1)
    except ImportError:
        pass

    # set the RNG seed
    rng_state = np.random.get_state()
    np.random.seed(seed)

    # STFT parameters
    framesize = parameters["stft_params"]["framesize"]
    win_a = pra.hann(framesize)
    win_s = pra.transform.compute_synthesis_window(win_a, framesize // 2)

    # Generate the audio signals

    # get the simulation parameters from the json file
    # Simulation parameters
    n_repeat = parameters["n_repeat"]
    fs = parameters["fs"]
    snr = parameters["snr"]

    n_interferers = parameters["n_interferers"]
    ref_mic = parameters["ref_mic"]
    room_dim = np.array(parameters["room_dim"])

    sources_var = np.ones(n_targets)
    sources_var[0] = parameters["weak_source_var"]

    # total number of sources
    n_sources = n_interferers + n_targets

    # Geometry of the room and location of sources and microphones
    interferer_locs = random_layout(
        [3.0, 5.5, 1.5], n_interferers, offset=[6.5, 1.0, 0.5], seed=1
    )

    target_locs = semi_circle_layout(
        [4.1, 3.755, 1.2],
        np.pi / 1.5,
        2.0,  # 120 degrees arc, 2 meters away
        n_targets,
        rot=0.743 * np.pi,
    )

    source_locs = np.concatenate((target_locs, interferer_locs), axis=1)

    mic_locs = semi_circle_layout([4.1, 3.76, 1.2], np.pi, 0.04, n_mics, rot=np.pi / 2. * 0.99)

    signals = wav_read_center(wav_files, seed=123)

    # Create the room itself
    room = pra.ShoeBox(
        room_dim,
        fs=fs,
        absorption=parameters["rt60_list"][rt60]["absorption"],
        max_order=parameters["rt60_list"][rt60]["max_order"],
    )

    # Place all the sound sources
    for sig, loc in zip(signals[-n_sources:, :], source_locs.T):
        room.add_source(loc, signal=sig)

    assert len(room.sources) == n_sources, (
        "Number of signals ({}) doesn"
        "t match number of sources ({})".format(signals.shape[0], n_sources)
    )

    # Place the microphone array
    room.add_microphone_array(pra.MicrophoneArray(mic_locs, fs=room.fs))

    # compute RIRs
    room.compute_rir()

    # Run the simulation
    premix = room.simulate(return_premix=True)  # shape (n_src, n_mics, n_samples)
    n_samples = premix.shape[2]

    # Normalize the signals so that they all have unit
    # variance at the reference microphone
    p_mic_ref = np.std(premix[:, ref_mic, :], axis=1)
    premix /= p_mic_ref[:, None, None]

    # scale to pre-defined variance
    premix[:n_targets, :, :] *= np.sqrt(sources_var[:, None, None])

    # compute noise variance
    sigma_n = np.sqrt(10 ** (-snr / 10) * np.sum(sources_var))

    # now compute the power of interference signal needed to achieve desired SINR
    sigma_i = np.sqrt(
        np.maximum(0, 10 ** (-sinr / 10) * np.sum(sources_var) - sigma_n ** 2)
        / n_interferers
    )
    premix[n_targets:, :, :] *= sigma_i

    # sum up the background
    # shape (n_mics, n_samples)
    background = (
            np.sum(premix[n_targets:, :, :], axis=0)
            + sigma_n * np.random.randn(*premix.shape[1:])
            )

    # Mix down the recorded signals
    mix = np.sum(premix[:n_targets], axis=0) + background

    # shape (n_targets+1, n_samples, n_mics)
    ref = np.zeros((n_targets+1, premix.shape[2], premix.shape[1]), dtype=premix.dtype)  
    ref[:n_targets, :, :] = premix[:n_targets, :, :].swapaxes(1, 2)
    ref[n_targets, :, :] = background.T

    synth = np.zeros_like(ref)
    synth[n_targets, :, 0] = np.random.randn(synth.shape[1])  # fill this to compare to background

    # START BSS
    ###########

    # shape: (n_frames, n_freq, n_mics)
    X_all = pra.transform.analysis(mix.T, framesize, framesize // 2, win=win_a)
    X_mics = X_all[:, :, :n_mics]

    # convergence monitoring callback
    def convergence_callback(Y, n_targets, SDR, SIR, ref, framesize, win_s, algo_name):
        from mir_eval.separation import bss_eval_sources

        if Y.shape[2] == 1:
            y = pra.transform.synthesis(
                Y[:, :, 0], framesize, framesize // 2, win=win_s
            )[:, None]
        else:
            y = pra.transform.synthesis(Y, framesize, framesize // 2, win=win_s)

        if algo_name not in parameters["overdet_algos"]:
            new_ord = np.argsort(np.std(y, axis=0))[::-1]
            y = y[:, new_ord]

        m = np.minimum(y.shape[0] - framesize // 2, ref.shape[1])

        synth[:n_targets, :m, 0] = y[framesize // 2 : m + framesize // 2, :n_targets].T

        sdr, sir, sar, perm = bss_eval_sources(
                ref[:n_targets+1, :m, 0], synth[:, :m, 0]
        )
        SDR.append(sdr[:n_targets].tolist())
        SIR.append(sir[:n_targets].tolist())

    # store results in a list, one entry per algorithm
    results = []

    # compute the initial values of SDR/SIR
    init_sdr = []
    init_sir = []
    if not parameters["monitor_convergence"]:
        convergence_callback(
            X_mics, n_targets, init_sdr, init_sir, ref, framesize, win_s, "init"
        )

    for full_name, params in parameters["algorithm_kwargs"].items():

        name = params['algo']
        kwargs = params['kwargs']

        if name == "auxiva_pca" and n_targets == 1:
            # PCA doesn't work for single source scenario
            continue
        elif name == "ogive" and n_targets != 1:
            # OGIVE is only for single target
            continue

        results.append(
            {
                "algorithm": full_name,
                "n_targets": n_targets,
                "n_mics": n_mics,
                "rt60": rt60,
                "sinr": sinr,
                "seed": seed,
                "sdr": [],
                "sir": [],  # to store the result
                "runtime" : np.nan,
                "n_samples" : n_samples,
            }
        )

        if parameters["monitor_convergence"]:

            def cb(Y):
                convergence_callback(
                    Y,
                    n_targets,
                    results[-1]["sdr"],
                    results[-1]["sir"],
                    ref,
                    framesize,
                    win_s,
                    name,
                )

        else:
            cb = None
            # avoid one computation by using the initial values of sdr/sir
            results[-1]["sdr"].append(init_sdr[0])
            results[-1]["sir"].append(init_sir[0])

        try:
            t_start = time.perf_counter()

            if name == "auxiva":
                # Run AuxIVA
                # this calls full IVA when `n_src` is not provided
                Y = overiva(X_mics, callback=cb, **kwargs)

            elif name == "auxiva_pca":

                # Run AuxIVA
                Y = auxiva_pca(X_mics, n_src=n_targets, callback=cb, **kwargs)

            elif name == "overiva":
                # Run BlinkIVA
                Y = overiva(X_mics, n_src=n_targets, callback=cb, **kwargs)

            elif name == "ilrma":
                # Run AuxIVA
                Y = pra.bss.ilrma(X_mics, callback=cb, **kwargs)

            elif name == "ogive":
                # Run OGIVE
                Y = ogive(X_mics, callback=cb, **kwargs)

            else:
                continue

            t_finish = time.perf_counter()

            # The last evaluation
            convergence_callback(
                Y,
                n_targets,
                results[-1]["sdr"],
                results[-1]["sir"],
                ref,
                framesize,
                win_s,
                name,
            )

            results[-1]["runtime"] = t_finish - t_start

        except:
            import os, json

            pid = os.getpid()
            # report last sdr/sir as np.nan
            results[-1]["sdr"].append(np.nan)
            results[-1]["sir"].append(np.nan)
            # now write the problem to file
            fn_err = os.path.join(
                parameters["_results_dir"], "error_{}.json".format(pid)
            )
            with open(fn_err, "a") as f:
                f.write(json.dumps(results[-1], indent=4))
            # skip to next iteration
            continue

    # restore RNG former state
    np.random.set_state(rng_state)

    return results


def generate_arguments(parameters):
    """ This will generate the list of arguments to run simulation for """

    rng_state = np.random.get_state()
    np.random.seed(parameters["seed"])

    gen_files_seed = int(np.random.randint(2 ** 32, dtype=np.uint32))
    all_wav_files = sampling(
        parameters["n_repeat"],
        parameters["n_interferers"] + np.max(parameters["n_targets_list"]),
        parameters["samples_list"],
        gender_balanced=True,
        seed=gen_files_seed,
    )

    args = []

    for n_targets in parameters["n_targets_list"]:
        for n_mics in parameters["n_mics_list"]:

            # we don't do underdetermined
            if n_targets > n_mics:
                continue

            for rt60 in parameters["rt60_list"].keys():
                for sinr in parameters["sinr_list"]:
                    for wav_files in all_wav_files:

                        # generate the seed for this simulation
                        seed = int(np.random.randint(2 ** 32, dtype=np.uint32))

                        # add the new combination to the list
                        args.append([n_targets, n_mics, rt60, sinr, wav_files, seed])

    np.random.set_state(rng_state)

    return args


if __name__ == "__main__":

    rrtools.run(
        one_loop,
        generate_arguments,
        func_init=init,
        base_dir=base_dir,
        results_dir="data/",
        description="Simulation for Independent Vector Analysis with more Microphones than Sources (submitted WASPAA 2019)",
    )
