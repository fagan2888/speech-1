"""
RNN Speech Generation Model
Ishaan Gulrajani
"""
import os, sys
sys.path.append(os.getcwd())

try: # This only matters on Ishaan's computer
    import experiment_tools
    experiment_tools.wait_for_gpu(high_priority=False, debug=False)
except ImportError:
    pass

import numpy
numpy.random.seed(123)
import random
random.seed(123)

import dataset

import theano
import theano.tensor as T
import theano.ifelse
import lib
import lasagne
import scipy.io.wavfile

import time
import functools
import itertools

# Hyperparams
BATCH_SIZE = 128
SEQ_LEN = 256 # How many audio samples to include in each truncated BPTT pass
SEQ_LEN_ANNEAL_ITERS = 1
DIM = 512 # Model dimensionality. 512 is sufficient for model development; 1024 if you want good samples.
N_GRUS = 4 # How many GRUs to stack in the frame-level model
Q_LEVELS = None # How many levels to use when discretizing samples. e.g. 256 = 8-bit scalar quantization
GRAD_CLIP = 1 # Elementwise grad clip threshold

# Dataset
# DATA_PATH = '/media/seagate/blizzard/parts'
# N_FILES = 141703
DATA_PATH = '/PersimmonData/kiwi_parts'
N_FILES = 516
BITRATE = 16000

# Other constants
TRAIN_MODE = 'iters' # 'iters' to use PRINT_ITERS and STOP_ITERS, 'time' to use PRINT_TIME and STOP_TIME
GENERATE_AND_SAVE = True
PRINT_ITERS = 1000 # Print cost, generate samples, save model checkpoint every N iterations.
STOP_ITERS = 200*1000 # Stop after this many iterations
PRINT_TIME = 60*60 # Print cost, generate samples, save model checkpoint every N seconds.
STOP_TIME = 60*60*12 # Stop after this many seconds of actual training (not including time req'd to generate samples etc.)
TEST_SET_SIZE = 128 # How many audio files to use for the test set
Q_ZERO = None#numpy.int32(Q_LEVELS//2) # Discrete value correponding to zero amplitude

from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
theano_srng = RandomStreams(seed=234)

# data_feeder = dataset.feed_epoch(DATA_PATH, N_FILES, BATCH_SIZE, 256, 1, Q_LEVELS, Q_ZERO)
# for i in xrange(100*500):
#     data_feeder.next()

print "Model settings:"
all_vars = [(k,v) for (k,v) in locals().items() if (k.isupper() and k != 'T')]
all_vars = sorted(all_vars, key=lambda x: x[0])
for var_name, var_value in all_vars:
    print "\t{}: {}".format(var_name, var_value)

def gaussian_nll(x, mu, log_sigma):
    sigma_squared = T.exp(2*log_sigma)
    return (
        lib.floatX(0.5*numpy.log(2*numpy.pi)) + 
        (2*log_sigma) + 
        ( ((x-mu)**2) / (2*sigma_squared) )
    )

def sample_level_rnn(input_sequences, h0, reset):
    """
    input_sequences.shape: (batch size, seq len)
    h0.shape:              (batch size, N_GRUS, DIM)
    reset.shape:           ()
    output.shape:          (batch size, seq len, Q_LEVELS)
    """

    learned_h0 = lib.param(
        'SampleLevel.h0',
        numpy.zeros((N_GRUS, DIM), dtype=theano.config.floatX)
    )
    learned_h0 = T.alloc(learned_h0, h0.shape[0], N_GRUS, DIM)
    h0 = theano.ifelse.ifelse(reset, learned_h0, h0)

    # Embedded inputs
    #################

    # FRAME_SIZE = Q_LEVELS
    # frames = lib.ops.Embedding('SampleLevel.Embedding', Q_LEVELS, Q_LEVELS, input_sequences)

    # Real-valued inputs
    ####################

    # 'frames' of size 1
    FRAME_SIZE = 1
    frames = input_sequences.reshape((
        input_sequences.shape[0],
        input_sequences.shape[1],
        1
    ))
    # # Rescale frames from ints in [0, Q_LEVELS) to floats in [-2, 2]
    # # (a reasonable range to pass as inputs to the RNN)
    # frames = (frames.astype('float32') / lib.floatX(Q_LEVELS/2)) - lib.floatX(1)
    # frames *= lib.floatX(2)

    gru0 = lib.ops.LowMemGRU('SampleLevel.GRU0', FRAME_SIZE, DIM, frames, h0=h0[:, 0])
    # gru0 = T.nnet.relu(lib.ops.Linear('SampleLevel.GRU0FF', DIM, DIM, gru0, initialization='he'))
    grus = [gru0]
    for i in xrange(1, N_GRUS):
        gru = lib.ops.LowMemGRU('SampleLevel.GRU'+str(i), DIM, DIM, grus[-1], h0=h0[:, i])
        # gru = T.nnet.relu(lib.ops.Linear('SampleLevel.GRU'+str(i)+'FF', DIM, DIM, gru, initialization='he'))
        grus.append(gru)

    # We apply the softmax later
    output = lib.ops.Linear(
        'Output',
        N_GRUS*DIM,
        2,
        T.concatenate(grus, axis=2)
    )
    # output = lib.ops.Linear(
    #     'Output',
    #     DIM,
    #     Q_LEVELS,
    #     grus[-1]
    # )

    last_hidden = T.stack([gru[:,-1] for gru in grus], axis=1)

    return (output, last_hidden)

sequences   = T.matrix('sequences')
h0          = T.tensor3('h0')
reset       = T.iscalar('reset')

input_sequences = sequences[:, :-1]
target_sequences = sequences[:, 1:]

sample_level_outputs, new_h0 = sample_level_rnn(input_sequences, h0, reset)

cost = T.mean(gaussian_nll(
    target_sequences.flatten(), 
    sample_level_outputs.flatten()[::2], 
    sample_level_outputs.flatten()[1::2]
))
# cost = T.nnet.categorical_crossentropy(
#     T.nnet.softmax(sample_level_outputs.reshape((-1, Q_LEVELS))),
#     target_sequences.flatten()
# ).mean()

# By default we report cross-entropy cost in bits. 
# Switch to nats by commenting out this line:
# cost = cost * lib.floatX(1.44269504089)

params = lib.search(cost, lambda x: hasattr(x, 'param'))
lib._train.print_params_info(cost, params)

grads = T.grad(cost, wrt=params, disconnected_inputs='warn')
grads = [T.clip(g, lib.floatX(-GRAD_CLIP), lib.floatX(GRAD_CLIP)) for g in grads]

updates = lasagne.updates.adam(grads, params)

train_fn = theano.function(
    [sequences, h0, reset],
    [cost, new_h0],
    updates=updates,
    on_unused_input='warn'
)

generate_outputs, generate_new_h0 = sample_level_rnn(sequences, h0, reset)
g_mu = generate_outputs[:,:,0]
g_log_sigma = generate_outputs[:,:,1]
g_samples = g_mu + (T.exp(g_log_sigma)*theano_srng.normal(g_mu.shape))
generate_fn = theano.function(
    [sequences, h0, reset],
    [g_samples, generate_new_h0],
    on_unused_input='warn'
)

def generate_and_save_samples(tag):

    def write_audio_file(name, data):
        # data = data.astype('float32')
        # data -= data.min()
        # data /= data.max()
        # data -= 0.5
        # data *= 0.95
        scipy.io.wavfile.write(name+'.wav', BITRATE, data)

    # Generate 5 sample files, each 5 seconds long
    N_SEQS = 10
    LENGTH = 5*BITRATE

    samples = numpy.zeros((N_SEQS, LENGTH), dtype='float32')
    samples[:, 0] = 0

    h0 = numpy.zeros((N_SEQS, N_GRUS, DIM), dtype='float32')

    for t in xrange(1, LENGTH):
        samples[:, t:t+1], h0 = generate_fn(
            samples[:, t-1:t],
            h0,
            numpy.int32(t == 1)
        )

    for i in xrange(N_SEQS):
        write_audio_file("sample_{}_{}".format(tag, i), samples[i])

print "Training!"
total_iters = 0
total_time = 0.
last_print_time = 0.
last_print_iters = 0
curr_seq_len = 2
costs = []
for epoch in itertools.count():

    h0 = numpy.zeros((BATCH_SIZE, N_GRUS, DIM), dtype='float32')
    data_feeder = dataset.feed_epoch(DATA_PATH, N_FILES, BATCH_SIZE, curr_seq_len, 1, Q_LEVELS, Q_ZERO)

    for seqs, reset in data_feeder:
        start_time = time.time()
        cost, h0 = train_fn(seqs, h0, reset)
        total_time += time.time() - start_time
        total_iters += 1

        costs.append(cost)

        if (TRAIN_MODE=='iters' and total_iters-last_print_iters == PRINT_ITERS) or \
            (TRAIN_MODE=='time' and total_time-last_print_time >= PRINT_TIME):
            
            print "epoch:{}\ttotal iters:{}\ttrain cost:{}\ttotal time:{}\ttime per iter:{}".format(
                epoch,
                total_iters,
                numpy.mean(costs),
                total_time,
                total_time / total_iters
            )
            tag = "iters{}_time{}".format(total_iters, total_time)

            if GENERATE_AND_SAVE:
                generate_and_save_samples(tag)
                lib.save_params('params_{}.pkl'.format(tag))

            costs = []
            last_print_time += PRINT_TIME
            last_print_iters += PRINT_ITERS

        if total_iters % SEQ_LEN_ANNEAL_ITERS == 0:
            if curr_seq_len < SEQ_LEN:
                print "Doubling curr_seq_len to {}".format(curr_seq_len*2)
                curr_seq_len *= 2
                break

        if (TRAIN_MODE=='iters' and total_iters == STOP_ITERS) or \
            (TRAIN_MODE=='time' and total_time >= STOP_TIME):

            print "Done!"

            try: # This only matters on Ishaan's computer
                import experiment_tools
                experiment_tools.send_sms("done!")
            except ImportError:
                pass

            sys.exit()