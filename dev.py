import numpy as np
import random
import os
import h5py
import zarr
import sys
import pandas as pd
import daiquiri
#import bsddb3
import time
import scipy
import pickle
import collections
import itertools
import tqdm
import shutil

import matplotlib as mp
# Force matplotlib to not use any Xwindows backend.
mp.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns

import tsinfer
import msprime



def plot_breakpoints(ts, map_file, output_file):
    # Read in the recombination map using the read_hapmap method,
    recomb_map = msprime.RecombinationMap.read_hapmap(map_file)

    # Now we get the positions and rates from the recombination
    # map and plot these using 500 bins.
    positions = np.array(recomb_map.get_positions()[1:])
    rates = np.array(recomb_map.get_rates()[1:])
    num_bins = 500
    v, bin_edges, _ = scipy.stats.binned_statistic(
        positions, rates, bins=num_bins)
    x = bin_edges[:-1][np.logical_not(np.isnan(v))]
    y = v[np.logical_not(np.isnan(v))]
    fig, ax1 = plt.subplots(figsize=(16, 6))
    ax1.plot(x, y, color="blue", label="Recombination rate")
    ax1.set_ylabel("Recombination rate")
    ax1.set_xlabel("Chromosome position")

    # Now plot the density of breakpoints along the chromosome
    breakpoints = np.array(list(ts.breakpoints()))
    ax2 = ax1.twinx()
    v, bin_edges = np.histogram(breakpoints, num_bins, density=True)
    ax2.plot(bin_edges[:-1], v, color="green", label="Breakpoint density")
    ax2.set_ylabel("Breakpoint density")
    ax2.set_xlim(1.5e7, 5.3e7)
    plt.legend()
    fig.savefig(output_file)


def make_errors(v, p):
    """
    For each sample an error occurs with probability p. Errors are generated by
    sampling values from the stationary distribution, that is, if we have an
    allele frequency of f, a 1 is emitted with probability f and a
    0 with probability 1 - f. Thus, there is a possibility that an 'error'
    will in fact result in the same value.
    """
    w = np.copy(v)
    if p > 0:
        m = v.shape[0]
        frequency = np.sum(v) / m
        # Randomly choose samples with probability p
        samples = np.where(np.random.random(m) < p)[0]
        # Generate observations from the stationary distribution.
        errors = (np.random.random(samples.shape[0]) < frequency).astype(int)
        w[samples] = errors
    return w


def generate_samples(ts, error_p):
    """
    Returns samples with a bits flipped with a specified probability.

    Rejects any variants that result in a fixed column.
    """
    S = np.zeros((ts.sample_size, ts.num_mutations), dtype=np.int8)
    for variant in ts.variants():
        done = False
        # Reject any columns that have no 1s or no zeros
        while not done:
            S[:, variant.index] = make_errors(variant.genotypes, error_p)
            s = np.sum(S[:, variant.index])
            done = 0 < s < ts.sample_size
    return S.T


def tsinfer_dev(
        n, L, seed, num_threads=1, recombination_rate=1e-8,
        error_rate=0, method="C", log_level="WARNING",
        debug=True, progress=False, path_compression=True):

    np.random.seed(seed)
    random.seed(seed)
    L_megabases = int(L * 10**6)

    # daiquiri.setup(level=log_level)

    ts = msprime.simulate(
            n, Ne=10**4, length=L_megabases,
            recombination_rate=recombination_rate, mutation_rate=1e-8,
            random_seed=seed)
    if debug:
        print("num_sites = ", ts.num_sites)
    assert ts.num_sites > 0

    G = generate_samples(ts, error_rate)
    sample_data = tsinfer.SampleData.initialise(
        sequence_length=ts.sequence_length, chunk_size=10)
    sample_data.add_population(metadata={"name": "pop0"})
    for j in range(ts.num_samples):
        sample_data.add_sample(population=0, metadata={"name": "sample_{}".format(j)})
    for site, genotypes in zip(ts.sites(), G):
        sample_data.add_site(site.position, ["0", "1"], genotypes)
    sample_data.finalise()

    # # print(sample_data)
    # print(sample_data.data.tree())
    # print(sample_data.data.info)
    # print(sample_data.site_inference.info)
    # print(sample_data.site_inference[:])

    ancestor_data = tsinfer.AncestorData.initialise(sample_data, chunk_size=10)
    tsinfer.build_ancestors(sample_data, ancestor_data, method=method)
    ancestor_data.finalise()

    print(ancestor_data.data.tree())
    print(ancestor_data.data.info)
    # print(ancestor_data)

    ancestors_ts = tsinfer.match_ancestors(sample_data, ancestor_data, method=method)
    output_ts = tsinfer.match_samples(sample_data, ancestors_ts, method=method)
    print("inferred_num_edges = ", output_ts.num_edges)


def build_profile_inputs(n, num_megabases):
    L = num_megabases * 10**6
    input_file = "tmp__NOBACKUP__/profile-n={}-m={}.input.hdf5".format(
            n, num_megabases)
    if os.path.exists(input_file):
        ts = msprime.load(input_file)
    else:
        ts = msprime.simulate(
            n, length=L, Ne=10**4, recombination_rate=1e-8, mutation_rate=1e-8,
            random_seed=10)
        print("Ran simulation: n = ", n, " num_sites = ", ts.num_sites,
                "num_trees =", ts.num_trees)
        ts.dump(input_file)
    filename = "tmp__NOBACKUP__/profile-n={}-m={}.samples".format(n, num_megabases)
    if os.path.exists(filename):
        os.unlink(filename)
    # daiquiri.setup(level="DEBUG")
    sample_data = tsinfer.SampleData.initialise(
        sequence_length=ts.sequence_length, filename=filename, num_flush_threads=4)
    sample_data.add_population({"name": "pop0"})
    progress_monitor = tqdm.tqdm(total=ts.num_samples)
    for j in range(ts.num_samples):
        sample_data.add_sample(population=0, metadata={"name": "sample_{}".format(j)})
        progress_monitor.update()
    progress_monitor.close()
    progress_monitor = tqdm.tqdm(total=ts.num_sites)
    for variant in ts.variants():
        sample_data.add_site(
            variant.site.position, variant.alleles, variant.genotypes)
        progress_monitor.update()
    sample_data.finalise()
    progress_monitor.close()

    print(sample_data)

#     filename = "tmp__NOBACKUP__/profile-n={}_m={}.ancestors".format(n, num_megabases)
#     if os.path.exists(filename):
#         os.unlink(filename)
#     ancestor_data = tsinfer.AncestorData.initialise(sample_data, filename=filename)
#     tsinfer.build_ancestors(sample_data, ancestor_data, progress=True)
#     ancestor_data.finalise()

if __name__ == "__main__":

    np.set_printoptions(linewidth=20000)
    np.set_printoptions(threshold=20000000)

    build_profile_inputs(10, 1)
    # build_profile_inputs(100, 10)
    # build_profile_inputs(1000, 100)
    # build_profile_inputs(10**4, 100)
    # build_profile_inputs(10**5, 100)

    # tsinfer_dev(38, 2, seed=6, num_threads=0, method="C", recombination_rate=1e-8)


#     for seed in range(1, 10000):
#         print(seed)
#         # tsinfer_dev(40, 2.5, seed=seed, num_threads=1, genotype_quality=1e-3, method="C")

