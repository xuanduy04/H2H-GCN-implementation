import logging
import os
import sys
import time
import warnings

import numpy as np
import torch

from config import parser
from models import LPModel, NCModel
from optimizers import Adam, RiemannianSGD
from torch.optim.lr_scheduler import StepLR
from manifolds import StiefelManifold
from utils.pre_utils import set_seed, categorize_params
from utils.data_utils import load_data
from utils.train_utils import format_metrics

warnings.filterwarnings('once')


def train(args):
    # set args params
    if int(args.double_precision):
        torch.set_default_dtype(torch.float64)
    args.device = 'cuda:' + str(args.cuda) if int(args.cuda) >= 0 else 'cpu'
    args.dim = args.dim + 1  # add 1 because lorentz
    args.stie_vars = []
    args.eucl_vars = []
    if not args.lr_reduce_freq:
        args.lr_reduce_freq = args.epochs

    logging.info(f'Using {args.device}')

    # init the data, model and task-specific parameters
    # --data--
    # TODO: fix/ change/ reimplement the 'load_data' function.
    #   currently it doesn't support any dataset other than 'disease'
    data = load_data(args, os.path.join('./data', args.dataset))
    _, data_feature_dim = data['features'].shape
    if data_feature_dim != args.feature_dim:
        warnings.warn(f'feature_dim mismatch between data ({data_feature_dim}) '
                      f'and args ({args.feature_dim})')
    # --model--
    if args.task == 'nc':
        args.n_classes = int(data['labels'].max() + 1)
        model = LPModel(args)
        logging.info(f'Node classification with {args.n_classes} classes')
    elif args.task == 'lp':
        args.n_false_edges = len(data['train_edges_false'])
        args.n_edges = len(data['train_edges'])
        model = NCModel(args)
        logging.info(f'Link prediction with {args.n_edges} "true" edges'
                     f'and {args.n_false_edges} false edges')
    else:
        raise ValueError(
            'Only link prediction (lp) or node classification (nc) tasks are supported.')
    if args.pre_trained:
        raise NotImplementedError(
            'Using pre-trained model not supported.')
    if args.save:
        warnings.warn('No support for saving the model', UserWarning)

    # Optimizer and lr_scheduler
    stie_params, eucl_params = categorize_params(args)
    assert len(stie_params) > 0, 'Error in model initialization, found 0 Stiefel parameters'
    assert len(eucl_params) > 0, 'Error in model initialization, found 0 Euclidean parameters'
    stie_optim = RiemannianSGD(StiefelManifold, stie_params, args.stie_lr)
    eucl_optim = Adam(eucl_params, args.eucl_lr)
    stie_lr_scheduler = StepLR(
        stie_optim,
        step_size=int(args.step_lr_reduce_freq),
        gamma=float(args.step_lr_gamma)
    )
    eucl_lr_scheduler = StepLR(
        eucl_optim,
        step_size=int(args.step_lr_reduce_freq),
        gamma=float(args.step_lr_gamma)
    )

    # Push everything to args.device (copy-pasted)
    if args.cuda is not None and int(args.cuda) >= 0:
        os.environ['CUDA_VISIBLE_DEVICES'] = str(args.cuda)
        model = model.to(args.device)
        for x, val in data.items():
            if torch.is_tensor(data[x]):
                data[x] = data[x].to(args.device)

    # Train model
    t_begin = time.time()
    counter = 0
    best_val_metrics = model.init_metric_dict()
    best_test_metrics = None
    for epoch in range(1, args.epoch + 1):
        t_epoch = time.time()
        model.train()
        stie_optim.zero_grad()
        eucl_optim.zero_grad()

        embeddings = model.encode(data['features'], data['hgnn_adj'], data['hgnn_weight'])
        loss, train_metrics = model.compute_metrics(embeddings, data, 'train')
        loss.backward()

        stie_optim.step()
        eucl_optim.step()
        stie_lr_scheduler.step()
        eucl_lr_scheduler.step()

        if epoch % args.log_freq:  # almost copy-pasted
            logging.info(" ".join(['Epoch: {:04d}'.format(epoch + 1),
                                   'lr: {:04f}, stie_lr: {:04f}'.format(
                                       eucl_lr_scheduler.get_lr()[0],
                                       stie_lr_scheduler.get_lr()[0]
                                   ),
                                   format_metrics(train_metrics, 'train'),
                                   'time: {:.4f}s'.format(time.time() - t_epoch)
                                   ]))
        if epoch % args.eval_freq:  # almost copy-pasted
            model.eval()
            embeddings = model.encode(data['features'], data['hgnn_adj'], data['hgnn_weight'])
            for i in range(embeddings.size(0)):
                if (embeddings[i] != embeddings[i]).sum() > 1:
                    print('PART train  i', i, 'embeddings[i]', embeddings[i])
            val_metrics = model.compute_metrics(embeddings, data, 'val')
            if (epoch + 1) % args.log_freq == 0:
                logging.info(" ".join(
                    ['Epoch: {:04d}'.format(epoch + 1), format_metrics(val_metrics, 'val')]))
            if model.compare_metrics(best_val_metrics, val_metrics):
                best_val_metrics = val_metrics
                counter = 0
            else:
                counter += 1
                if counter == args.patience and epoch > args.min_epochs:
                    logging.info("Early stopping")
                    break

    logging.info("Done")
    logging.info(f"Time elapsed {time.time() - t_begin}")
    if args.task == 'lp':
        return best_test_metrics['roc']
    if args.task == 'nc':
        return best_test_metrics['f1']
    assert False


def get_mean_std(acc):
    if any(a < 0 for a in acc):
        acc = [a * 100 for a in acc]
    return np.mean(acc), np.std(acc)


if __name__ == '__main__':
    args = parser.parse_args()
    logging.getLogger().setLevel(logging.INFO)
    if args.log_to_stdout:
        logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

    set_seed(args, seed=args.seed)
    logging.info(f'Using seed {args.seed} to generate {args.num_runs} seeds')
    seeds = np.random.randint(0, 5000, size=args.num_runs).tolist()
    logging.info(f'Generated seeds: {seeds}')

    result_list = []
    for idx, seed in enumerate(seeds):
        args = parser.parse_args()
        set_seed(args, seed=seed)
        logging.info(f'Run no.{idx}, seed = {seed}')
        result = train(args)
        result_list.append(result)
        print(f'Current result list:\n{result_list}')
    mean, std = get_mean_std(result_list)
    print('mean:', mean, 'std:', std)