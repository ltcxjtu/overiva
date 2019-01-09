'''
Overdetermined Blind Source Separation offline example
======================================================

This script requires the `mir_eval` to run, and `tkinter` and `sounddevice` packages for the GUI option.
'''

import numpy as np
from scipy.io import wavfile

from mir_eval.separation import bss_eval_sources

from routines import PlaySoundGUI, grid_layout, semi_circle_layout, random_layout, gm_layout
from oiva import oiva
from oiva2 import oiva2
from oiva_group import oiva_group
from generate_samples import sampling, wav_read_center

# We concatenate a few samples to make them long enough
if __name__ == '__main__':

    choices = [
            'auxiva',
            'oiva',
            'oiva2',
            'oivag',
            ]

    import argparse
    parser = argparse.ArgumentParser(description='Demonstration of blind source separation using IVA or ILRMA.')
    parser.add_argument('-b', '--block', type=int, default=2048,
            help='STFT block size')
    parser.add_argument('-a', '--algo', type=str, default=choices[0], choices=choices,
            help='Chooses BSS method to run')
    parser.add_argument('-m', '--mics', type=int, default=5, help='Number of mics')
    parser.add_argument('--gui', action='store_true',
            help='Creates a small GUI for easy playback of the sound samples')
    parser.add_argument('--save', action='store_true',
            help='Saves the output of the separation to wav files')
    args = parser.parse_args()

    if args.gui:
        print('setting tkagg backend')
        # avoids a bug with tkinter and matplotlib
        import matplotlib
        matplotlib.use('TkAgg')

    import pyroomacoustics as pra

    # Simulation parameters
    fs = 16000
    absorption, max_order = 0.35, 17  # RT60 == 0.3
    #absorption, max_order = 0.45, 12  # RT60 == 0.2
    n_sources = 14
    n_mics = args.mics
    n_sources_target = 3  # the determined case

    use_fake_blinky = False
    use_real_R = False

    # fix the randomness for repeatability
    np.random.seed(10)

    # set the source powers, the first one is half
    source_std = np.ones(n_sources_target)
    source_std[0] /= np.sqrt(2.)

    SIR = 10  # dB
    SNR = 60  # dB, this is the SNR with respect to a single target source and microphone self-noise

    # STFT parameters
    framesize = 4096
    win_a = pra.hann(framesize)
    win_s = pra.transform.compute_synthesis_window(win_a, framesize // 2)

    # algorithm parameters
    n_iter = 41
    n_nmf_sub_iter = 100
    sparse_reg = 0.

    # pre-emphasis of blinky signals
    pre_emphasis = False

    # Geometry of the room and location of sources and microphones
    room_dim = np.array([10, 7.5, 3])
    '''
    mic_locs = np.c_[
            [4.03, 3.777, 1.1],
            [4.015, 3.755, 1.1],
            [4.03, 3.733, 1.1],
            [4.025, 3.71, 1.1],
            ]
    mic_locs = mic_locs[:,:n_mics]
    '''
    mic_locs = np.vstack((
        pra.circular_2D_array([4.1, 3.76], n_mics, np.pi / 2, 0.02),
        1.2 * np.ones((1, n_mics)),
        ))

    target_locs = semi_circle_layout([4.1, 3.755, 1.1], np.pi / 2, 2., n_sources_target, rot=0.743 * np.pi)
    #interferer_locs = grid_layout([3., 5.5], n_sources - n_sources_target, offset=[6.5, 1., 1.7])
    interferer_locs = random_layout([3., 5.5,1.5], n_sources - n_sources_target, offset=[6.5, 1., 0.5], seed=1)
    source_locs = np.concatenate((target_locs, interferer_locs), axis=1)
    #source_locs[2,:] += np.random.uniform(-0.02, 0.02, size=n_sources)
    '''
    source_locs = np.c_[
            [3., 4.,  1.7],  # target source 1
            [3., 6.,  1.7],  # target source 2
            [2., 1.5, 1.9],  # interferer 1
            [4., 1.5, 1.9],  # interferer 1
            [6., 1.5, 1.9],  # interferer 1
            [8., 1.5, 1.9],  # interferer 1
            ]
    '''

    # Prepare the signals
    wav_files = sampling(1, n_sources, '../icassp2019-blinky-iva/samples/metadata.json', gender_balanced=True, seed=16)[0]
    signals = wav_read_center(wav_files, seed=123)

    # Create the room itself
    room = pra.ShoeBox(room_dim, fs=fs, absorption=absorption, max_order=max_order)

    # Place a source of white noise playing for 5 s
    for sig, loc in zip(signals, source_locs.T,):
        room.add_source(loc, signal=sig)

    # Place the microphone array
    room.add_microphone_array(
            pra.MicrophoneArray(mic_locs, fs=room.fs)
            )

    # compute RIRs
    room.compute_rir()

    # define a callback that will do the signal mix to
    # get a the correct SNR and SIR
    callback_mix_kwargs = {
            'snr' : SNR,
            'sir' : SIR,
            'n_src' : n_sources,
            'n_tgt' : n_sources_target,
            'src_std' : source_std,
            'ref_mic' : 0,
            }

    def callback_mix(premix, snr=0, sir=0, ref_mic=0, n_src=None, n_tgt=None, src_std=None):

        # first normalize all separate recording to have unit power at microphone one
        p_mic_ref = np.std(premix[:,ref_mic,:], axis=1)
        premix /= p_mic_ref[:,None,None]
        premix[:n_tgt,:,:] *= src_std[:,None,None]

        # compute noise variance
        sigma_n = np.sqrt(10 ** (- snr / 10) * np.mean(src_std ** 2))

        # now compute the power of interference signal needed to achieve desired SIR
        num = 10 ** (- sir / 10) * np.sum(src_std ** 2)
        sigma_i = np.sqrt(num / (n_src - n_tgt))
        premix[n_tgt:n_src,:,:] *= sigma_i

        # Mix down the recorded signals
        mix = np.sum(premix[:n_src,:], axis=0) + sigma_n * np.random.randn(*premix.shape[1:])

        return mix

    # Run the simulation
    separate_recordings = room.simulate(
            callback_mix=callback_mix, callback_mix_kwargs=callback_mix_kwargs,
            return_premix=True,
            )
    mics_signals = room.mic_array.signals

    print('Simulation done.')


    # Monitor Convergence
    #####################

    ref = np.moveaxis(separate_recordings, 1, 2)
    if ref.shape[0] < n_mics:
        ref = np.concatenate((ref, np.random.randn(n_mics - ref.shape[0], ref.shape[1], ref.shape[2])), axis=0)

    SDR, SIR, cost_func = [], [], []
    def convergence_callback(Y, **kwargs):
        global SDR, SIR, ref
        from mir_eval.separation import bss_eval_sources
        y = pra.transform.synthesis(
                Y,
                framesize,
                framesize // 2,
                win=win_s,
                )

        if args.algo != 'blinkiva':
            new_ord = np.argsort(np.std(y, axis=0))[::-1]
            y = y[:,new_ord]

        m = np.minimum(y.shape[0]-framesize//2, ref.shape[1])
        sdr, sir, sar, perm = bss_eval_sources(ref[:n_sources_target,:m,0], y[framesize//2:m+framesize//2,:n_sources_target].T)
        SDR.append(sdr)
        SIR.append(sir)


    # START BSS
    ###########

    # pre-emphasis on blinky signals
    if pre_emphasis:
        mics_signals[n_mics:,:-1] = np.diff(mics_signals[n_mics:,:], axis=1)
        mics_signals[n_mics:,-1] = 0.

    # shape: (n_frames, n_freq, n_mics)
    X_all = pra.transform.analysis(
            mics_signals.T,
            framesize, framesize // 2,
            win=win_a,
            ) 
    X_mics =  X_all[:,:,:n_mics]

    # Run BSS
    if args.algo == 'auxiva':
        # Run AuxIVA
        Y = pra.bss.auxiva(X_mics, n_iter=n_iter, proj_back=True,
                callback=convergence_callback,
                )
    elif args.algo == 'oiva':
        # Run AuxIVA
        Y = oiva(X_mics, n_src=n_sources_target, n_iter=n_iter, proj_back=True,
                callback=convergence_callback,
                )
    elif args.algo == 'oiva2':
        # Run AuxIVA
        Y = oiva2(X_mics, n_src=n_sources_target, n_iter=n_iter, proj_back=True,
                callback=convergence_callback,
                )
    elif args.algo == 'oivag':
        # Run AuxIVA
        Y = oiva_group(X_mics, n_src=n_sources_target,
                n_iter=n_iter,
                n_sup_iter=1,
                proj_back=True,
                callback=convergence_callback,
                )
    else:
        raise ValueError('No such algorithm {}'.format(args.algo))

    # Run iSTFT
    y = pra.transform.synthesis(
            Y,
            framesize,
            framesize // 2,
            win=win_s,
            )

    # If some of the output are uniformly zero, just add a bit of noise to compare
    for k in range(y.shape[1]):
        if np.sum(np.abs(y[:,k])) < 1e-10:
            y[:,k] = np.random.randn(y.shape[0]) * 1e-10

    # For conventional methods of BSS, reorder the signals by decreasing power
    if args.algo != 'blinkiva':
        new_ord = np.argsort(np.std(y, axis=0))[::-1]
        y = y[:,new_ord]

    # Compare SIR
    #############
    m = np.minimum(y.shape[0]-framesize//2, ref.shape[1])
    sdr, sir, sar, perm = bss_eval_sources(ref[:n_sources_target,:m,0], y[framesize//2:m+framesize//2,:n_sources_target].T)

    # reorder the vector of reconstructed signals
    y_hat = y[:,perm]

    print('SDR:', sdr)
    print('SIR:', sir)

    import matplotlib.pyplot as plt

    plt.figure()

    for i in range(n_sources_target):
        plt.subplot(2,n_sources_target,i+1)
        plt.specgram(ref[i,:,0] + 1e-1, NFFT=1024, Fs=room.fs)
        plt.title('Source {} (clean)'.format(i))

        plt.subplot(2,n_sources_target, i+n_sources_target+1)
        plt.specgram(y_hat[:,i], NFFT=1024, Fs=room.fs)
        plt.title('Source {} (separated)'.format(i))

    plt.tight_layout(pad=0.5)

    plt.figure()
    a = np.array(SDR)
    b = np.array(SIR)
    for i, (sdr, sir) in enumerate(zip(a.T, b.T)):
        plt.plot(np.arange(a.shape[0]) * 10, sdr, label='SDR Source ' + str(i), marker='*')
        plt.plot(np.arange(a.shape[0]) * 10, sir, label='SIR Source ' + str(i), marker='o')
    plt.legend()
    plt.tight_layout(pad=0.5)

    if not args.gui:
        plt.show()
    else:
        plt.show(block=False)

    if args.save:
        from scipy.io import wavfile

        wavfile.write('bss_iva_mix.wav', room.fs,
                pra.normalize(mics_signals[0,:], bits=16).astype(np.int16))
        for i, sig in enumerate(y_hat):
            wavfile.write('bss_iva_source{}.wav'.format(i+1), room.fs,
                    pra.normalize(sig, bits=16).astype(np.int16))

    if args.gui:

        from tkinter import Tk
        # Make a simple GUI to listen to the separated samples
        root = Tk()
        my_gui = PlaySoundGUI(root, room.fs, mics_signals[0,:], y_hat.T, references=ref[:,:,0])
        root.mainloop()