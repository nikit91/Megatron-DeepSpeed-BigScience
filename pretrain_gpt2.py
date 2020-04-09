# coding=utf-8
# Copyright (c) 2019, NVIDIA CORPORATION.  All rights reserved.
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

"""Pretrain GPT2"""

import os

import torch

from megatron import get_args
from megatron import get_timers
from megatron import get_tokenizer
from megatron import mpu
from megatron import print_rank_0
from megatron.data.gpt2_dataset import build_train_valid_test_datasets
from megatron.model import GPT2Model
from megatron.training import pretrain
from megatron.utils import get_ltor_masks_and_position_ids
from megatron.utils import make_data_loader
from megatron.utils import reduce_losses


def model_provider():
    """Build the model."""
    args = get_args()

    print_rank_0('building GPT2 model ...')
    model = GPT2Model(num_tokentypes=0, parallel_output=True)

    return model


def get_batch(data_iterator):
    """Generate a batch"""
    args = get_args()
    tokenizer = get_tokenizer()

    # Items and their type.
    keys = ['text']
    datatype = torch.int64

    # Broadcast data.
    if data_iterator is not None:
        data = next(data_iterator)
    else:
        data = None
    data_b = mpu.broadcast_data(keys, data, datatype)

    # Unpack.
    tokens_ = data_b['text'].long()
    labels = tokens_[:, 1:].contiguous()
    tokens = tokens_[:, :-1].contiguous()

    # Get the masks and postition ids.
    attention_mask, loss_mask, position_ids = get_ltor_masks_and_position_ids(
        tokens,
        tokenizer.eod,
        args.reset_position_ids,
        args.reset_attention_mask,
        args.eod_mask_loss,
        args.fp16)

    return tokens, labels, loss_mask, attention_mask, position_ids


def forward_step(data_iterator, model):
    """Forward step."""
    timers = get_timers()

    # Get the batch.
    timers('batch generator').start()
    tokens, labels, loss_mask, attention_mask, position_ids = get_batch(
        data_iterator)
    timers('batch generator').stop()

    # Forward model.
    output = model(tokens, position_ids, attention_mask)
    losses = mpu.vocab_parallel_cross_entropy(output.contiguous().float(),
                                              labels)
    loss_mask = loss_mask.view(-1)
    loss = torch.sum(losses.view(-1) * loss_mask) / loss_mask.sum()

    # Reduce loss for logging.
    reduced_loss = reduce_losses([loss])

    return loss, {'lm loss': reduced_loss[0]}


def get_train_val_test_data():
    """Load the data on rank zero and boradcast number of tokens to all GPUS."""
    args = get_args()

    (train_data, valid_data, test_data) = (None, None, None)

    # Data loader only on rank 0 of each model parallel group.
    if mpu.get_model_parallel_rank() == 0:
        print_rank_0('> building train, validation, and test datasets '
                     'for GPT2 ...')

        data_parallel_size = mpu.get_data_parallel_world_size()
        data_parallel_rank = mpu.get_data_parallel_rank()
        global_batch_size = args.batch_size * data_parallel_size

        # Number of train/valid/test samples.
        train_iters = args.train_iters
        eval_iters = (train_iters // args.eval_interval + 1) * args.eval_iters
        test_iters = args.eval_iters
        train_val_test_num_samples = [train_iters * global_batch_size,
                                      eval_iters * global_batch_size,
                                      test_iters * global_batch_size]
        print_rank_0(' > datasets target sizes (minimum size):')
        print_rank_0('    train:      {}'.format(train_val_test_num_samples[0]))
        print_rank_0('    validation: {}'.format(train_val_test_num_samples[1]))
        print_rank_0('    test:       {}'.format(train_val_test_num_samples[2]))

        train_ds, valid_ds, test_ds = build_train_valid_test_datasets(
            data_prefix=args.data_path,
            data_impl=args.data_impl,
            splits_string=args.split,
            train_valid_test_num_samples=train_val_test_num_samples,
            seq_length=args.seq_length,
            seed=args.seed,
            skip_warmup=(not args.mmap_warmup))
        print_rank_0("> finished creating GPT2 datasets ...")

        train_data = make_data_loader(train_ds)
        valid_data = make_data_loader(valid_ds)
        test_data = make_data_loader(test_ds)

        do_train = train_data is not None and args.train_iters > 0
        do_valid = valid_data is not None and args.eval_iters > 0
        do_test = test_data is not None and args.eval_iters > 0
        # Need to broadcast num_tokens and num_type_tokens.
        flags = torch.cuda.LongTensor(
            [int(do_train), int(do_valid), int(do_test)])
    else:
        flags = torch.cuda.LongTensor([0, 0, 0])

    # Broadcast num tokens.
    torch.distributed.broadcast(flags,
                                mpu.get_model_parallel_src_rank(),
                                group=mpu.get_model_parallel_group())
    args.do_train = flags[0].item()
    args.do_valid = flags[1].item()
    args.do_test = flags[2].item()

    return train_data, valid_data, test_data


if __name__ == "__main__":

    pretrain(get_train_val_test_data, model_provider, forward_step,
             args_defaults={'tokenizer_type': 'GPT2BPETokenizer'})
