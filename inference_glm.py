# -*- encoding: utf-8 -*-
'''
@File    :   inference_glm.py
@Time    :   2021/10/22 19:41:58
@Author  :   Ming Ding
@Contact :   dm18@mail.tsinghua.edu.cn
'''

# here put the import lib
from functools import partial
import os
import sys
import random
import time
from datetime import datetime
import torch
import torch.nn.functional as F
import argparse
import stat
import mpu
from functools import partial
from arguments import get_args
from model.glm_model import GLMModel
from model.cached_autoregressive_model import CachedAutoregressiveMixin
from training import load_checkpoint, initialize_distributed, set_random_seed, prepare_tokenizer
from generation.autoregressive_sampling import filling_sequence
from generation.sampling_strategies import BeamSearchStrategy, BaseStrategy
from generation.utils import timed_name, generate_continually

def get_masks_and_position_ids_glm(seq, mask_position, context_length):
    tokens = seq.unsqueeze(0)

    attention_mask = torch.ones((1, len(seq), len(seq)), device=tokens.device)
    attention_mask.tril_()
    attention_mask[..., :context_length] = 1
    attention_mask.unsqueeze_(1)

    position_ids = torch.zeros(2, len(seq), device=tokens.device, dtype=torch.long)
    torch.arange(0, context_length, out=position_ids[0, :context_length])
    position_ids[0, context_length:] = mask_position
    torch.arange(1, len(seq) - context_length + 1, out=position_ids[1, context_length:])

    position_ids = position_ids.unsqueeze(0)
    return tokens, attention_mask, position_ids


def main(args):
    initialize_distributed(args)
    tokenizer = prepare_tokenizer(args)
    # build model 
    model = GLMModel(args)
    model.add_mixin('auto-regressive', CachedAutoregressiveMixin())
    if args.fp16:
        model = model.half()
    model = model.to(args.device)
    load_checkpoint(model, args)
    set_random_seed(args.seed)
    model.eval()

    end_tokens = [tokenizer.get_command('eop').Id, tokenizer.get_command('eos').Id]
    # define function for each query
    strategy = BaseStrategy(temperature=args.temperature, top_k=args.top_k,end_tokens=end_tokens)
    
    def process(raw_text):
        if args.with_id:
            query_id, raw_text = raw_text.split('\t')
        # add MASK
        generation_mask = '[gMASK]' if args.task_mask else '[MASK]'
        if 'MASK]' not in raw_text:
            raw_text += ' ' + generation_mask
        seq = tokenizer.EncodeAsIds(raw_text).tokenization
        seq = [tokenizer.get_command('ENC').Id] + seq
        if not raw_text.endswith('MASK]'):
            seq = seq + [tokenizer.get_command('eos').Id]
        print('raw text: {}\n'.format(raw_text))
        if len(seq) > args.max_sequence_length:
            raise ValueError('text too long.')
        
        # find mask tokens positions
        mask_tokens = ['MASK', 'sMASK', 'gMASK'] if args.task_mask else ['MASK']
        mask_tokens = [tokenizer.get_command(token).Id for token in mask_tokens]
        mask_positions = []
        context_tokens_tensor = torch.tensor(seq, dtype=torch.long, device=args.device)
        for token in mask_tokens:
            mask_positions += (context_tokens_tensor == token).nonzero(as_tuple=True)[0].tolist()
        mask_positions.sort()
        
        # generation
        mbz = args.max_inference_batch_size
        assert args.batch_size < mbz or args.batch_size % mbz == 0
        output_list = [seq]
        # continually detect the first mark position
        while True:
            seq = output_list[0] # TODO find the best one
            # detect
            mask_tokens = ['MASK', 'sMASK', 'gMASK'] if args.task_mask else ['MASK']
            mask_tokens = [tokenizer.get_command(token).Id for token in mask_tokens]
            mask_position = len(seq)
            for token in mask_tokens:
                try:
                    mask_position = min(mask_position, seq.index(token))
                except ValueError:
                    pass
            if mask_position == len(seq):
                break
            
            get_func = partial(get_masks_and_position_ids_glm, mask_position=mask_position, context_length=len(seq))
            output_list = []
            for tim in range(max(args.batch_size // mbz, 1)):
                input_seq = torch.cuda.LongTensor(seq + [tokenizer.get_command('sop').Id] + [-1] * (args.out_seq_length-len(seq)-1), device=args.device)
                output, _mems = filling_sequence(model, input_seq,
                        batch_size=min(args.batch_size, mbz),
                        strategy=strategy,
                        log_attention_weights=None,
                        get_masks_and_position_ids=get_func
                        ) # we don't use mems, fill back
                if isinstance(output, torch.Tensor): # different strategies
                    output = list(output)
                
                output_list.extend(output)

            # clip -1s and fill back generated things into seq
            for i in range(len(output_list)):
                output = output_list[i].tolist()
                try:
                    unfinished = output.index(-1)
                except ValueError:
                    unfinished = len(output)
                if output[unfinished-1] in end_tokens:
                    unfinished -= 1
                bog = output.index(tokenizer.get_command('sop').Id)
                output_list[i] = output[:mask_position] + output[bog+1:unfinished] + output[mask_position+1:bog]

        # decoding
        txts = []
        for seq in output_list:
            decode_tokens = tokenizer.DecodeIds(seq)
            txts.append(decode_tokens)

        # save
        if args.with_id:
            full_path = os.path.join(args.output_path, query_id + '.txt')
        else:
            prefix = raw_text.replace('/', '')[:20]
            full_path = timed_name(prefix, '.txt', args.output_path)
            print(txts[0]) # print the first.
        with open(full_path, 'w') as fout:
            for txt in txts:
                fout.write(txt + '\n')
        os.chmod(full_path, stat.S_IRWXO+stat.S_IRWXG+stat.S_IRWXU)

    os.makedirs(args.output_path, exist_ok=True)
    generate_continually(process, args.input_source)

if __name__ == "__main__":
    py_parser = argparse.ArgumentParser(add_help=False)

    known, args_list = py_parser.parse_known_args()
    args = get_args(args_list)
    args = argparse.Namespace(**vars(args), **vars(known))
    
    with torch.no_grad():
        main(args)