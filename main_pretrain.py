# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# DeiT: https://github.com/facebookresearch/deit
# BEiT: https://github.com/microsoft/unilm/tree/master/beit
# --------------------------------------------------------
from util.datasets import build_transform, get_loaders
from engine_pretrain import train_one_epoch, evaluate_knn_accuracy
import models_mae
from util.misc import NativeScalerWithGradNormCount as NativeScaler
import util.misc as misc
import timm.optim.optim_factory as optim_factory
import argparse
import datetime
import json
import numpy as np
import os
import time
from pathlib import Path

import torch
import torch.backends.cudnn as cudnn
from torch.utils.tensorboard import SummaryWriter

import timm

assert timm.__version__ == "0.3.2"  # version check
os.environ["TORCH_DISTRIBUTED_DEBUG"] = "INFO"

def get_args_parser():
	parser = argparse.ArgumentParser('MAE pre-training', add_help=False)
	parser.add_argument('--batch_size', default=64, type=int,
						help='Batch size per GPU (effective batch size is batch_size * accum_iter * # gpus')
	parser.add_argument('--epochs', default=400, type=int)
	parser.add_argument('--accum_iter', default=1, type=int,
						help='Accumulate gradient iterations (for increasing the effective batch size under memory constraints)')

	# Model parameters
	parser.add_argument('--model', default='mae_vit_large_patch16', type=str, metavar='MODEL',
						help='Name of model to train')

	parser.add_argument('--input_size', default=224, type=int,
						help='images input size')

	parser.add_argument('--mask_ratio', default=0.75, type=float,
						help='Masking ratio (percentage of removed patches).')

	parser.add_argument('--norm_pix_loss', action='store_true',
						help='Use (per-patch) normalized pixels as targets for computing loss')
	parser.set_defaults(norm_pix_loss=False)

	# Optimizer parameters
	parser.add_argument('--weight_decay', type=float, default=0.05,
						help='weight decay (default: 0.05)')

	parser.add_argument('--lr', type=float, default=None, metavar='LR',
						help='learning rate (absolute lr)')
	parser.add_argument('--blr', type=float, default=1e-3, metavar='LR',
						help='base learning rate: absolute_lr = base_lr * total_batch_size / 256')
	parser.add_argument('--min_lr', type=float, default=0., metavar='LR',
						help='lower lr bound for cyclic schedulers that hit 0')

	parser.add_argument('--warmup_epochs', type=int, default=40, metavar='N',
						help='epochs to warmup LR')

	# Dataset parameters
	parser.add_argument('--data_path', default='/datasets01/imagenet_full_size/061417/', type=str,
						help='dataset path')

	parser.add_argument('--output_dir', default='./output_dir',
						help='path where to save, empty for no saving')
	parser.add_argument('--log_dir', default='./output_dir',
						help='path where to tensorboard log')
	parser.add_argument('--img_log_freq', default=100,
						type=int, help='step frequency of logging reconstructed images')
	parser.add_argument('--device', default='cuda',
						help='device to use for training / testing')
	parser.add_argument('--seed', default=0, type=int)
	parser.add_argument('--resume', default='',
						help='resume from checkpoint')
	parser.add_argument('--ckpt_save_freq', default=50,
						type=int, help='epoch frequency of saving ckpts')

	parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
						help='start epoch')
	parser.add_argument('--num_workers', default=6, type=int)
	parser.add_argument('--pin_mem', action='store_true',
						help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')
	parser.add_argument('--no_pin_mem', action='store_false', dest='pin_mem')
	parser.set_defaults(pin_mem=True)

	# distributed training parameters
	parser.add_argument('--world_size', default=1, type=int,
						help='number of distributed processes')
	parser.add_argument('--local_rank', default=-1, type=int)
	parser.add_argument('--dist_on_itp', action='store_true')
	parser.add_argument('--dist_url', default='env://',
						help='url used to set up distributed training')

	parser.add_argument('--domains', nargs="+", required=True)
	parser.add_argument('--target_domains', nargs="+", default=None)

	# for monitoring knn accuracies
	parser.add_argument('--source_eval_domains', nargs="+", default=None)
	parser.add_argument('--target_eval_domains', nargs="+", default=None)
	parser.add_argument('--rand_augs', type=int, default=0,
						help='number of augmentations transforms to apply')
	parser.add_argument('--rand_aug_severity', type=float,
						default=1, help='the severity of RandAug transforms')
	return parser


def main(args):
	misc.init_distributed_mode(args)
	if args.world_size == 1: args.distributed = False
	misc.log_arguments(args)

	print('job dir: {}'.format(os.path.dirname(os.path.realpath(__file__))))
	print("{}".format(args).replace(', ', ',\n'))

	device = torch.device(args.device)

	# fix the seed for reproducibility
	seed = args.seed + misc.get_rank()
	torch.manual_seed(seed)
	np.random.seed(seed)

	cudnn.benchmark = True
	
	transform_train = build_transform(is_train=True, args=args)
	transform_val = build_transform(is_train=False, args=args)

	args.dist_eval = False
	((data_loader_train, _), _), target_loaders = get_loaders(
		args, misc, args.domains, transform_train, target_domains=args.target_domains)

	if args.target_domains is None:
		target_data_loader_train = None
	else:
		((target_data_loader_train, _), _) = target_loaders

	if args.target_domains is not None:
		backup = args.distributed
		args.distributed = False
		(_, (data_loader_val_nodist, _)), (_, (target_data_loader_val_nodist, _)) = get_loaders(
			args, misc, args.domains, transform_train, target_domains=args.target_domains,
			val_transforms=transform_val, source_eval_domains=args.source_eval_domains,
			target_eval_domains=args.target_eval_domains)
		
		args.distributed = backup

	global_rank = misc.get_rank()
	if global_rank == 0 and args.log_dir is not None:
		os.makedirs(args.log_dir, exist_ok=True)
		log_writer = SummaryWriter(log_dir=args.log_dir)
	else:
		log_writer = None

	model = models_mae.__dict__[args.model](
		norm_pix_loss=args.norm_pix_loss)

	model.to(device)

	model_without_ddp = model
	print("Model = %s" % str(model_without_ddp))

	eff_batch_size = args.batch_size * args.accum_iter * misc.get_world_size()

	if args.lr is None:  # only base_lr is specified
		args.lr = args.blr * eff_batch_size / 256

	print("base lr: %.2e" % (args.lr * 256 / eff_batch_size))
	print("actual lr: %.2e" % args.lr)

	print("accumulate grad iterations: %d" % args.accum_iter)
	print("effective batch size: %d" % eff_batch_size)

	model_without_ddp = model
	if args.distributed:
		model = torch.nn.parallel.DistributedDataParallel(
			model, device_ids=[args.gpu])
		model_without_ddp = model.module

	# following timm: set wd as 0 for bias and norm layers
	param_groups = optim_factory.add_weight_decay(model_without_ddp, args.weight_decay)

	optimizer = torch.optim.AdamW(param_groups, lr=args.lr, betas=(0.9, 0.95))
	print(optimizer)
	loss_scaler = NativeScaler()

	misc.load_model(args=args, model_without_ddp=model_without_ddp,
					optimizer=optimizer, loss_scaler=loss_scaler, strict=False, load_optim_from_ckpt=False)
	

	print(f"Start training for {args.epochs} epochs")
	start_time = time.time()
	
	for epoch in range(args.start_epoch, args.epochs):
		if args.target_domains is not None and (epoch + 1 % 10) == 0:
			top1_acc = evaluate_knn_accuracy(model, data_loader_val_nodist, \
													   target_data_loader_val_nodist, \
													   device, args)
			if log_writer is not None:
				log_writer.add_scalar('cd_knn_tgt_acc', top1_acc, epoch)

		if args.distributed:
			data_loader_train.sampler.set_epoch(epoch)
			if target_data_loader_train is not None:
				target_data_loader_train.sampler.set_epoch(epoch)

		train_stats = train_one_epoch(
			model, data_loader_train,
			optimizer, device, epoch, loss_scaler,
			target_data_loader=target_data_loader_train,
			log_writer=log_writer,
			args=args
		)
		if args.output_dir and ((epoch + 1) % args.ckpt_save_freq == 0 or epoch + 1 == args.epochs):
			misc.save_model(
				args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
				loss_scaler=loss_scaler, epoch=epoch)

		log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
					 'epoch': epoch, }

		if args.output_dir and misc.is_main_process():
			if log_writer is not None:
				log_writer.flush()
			with open(os.path.join(args.output_dir, "log.txt"), mode="a", encoding="utf-8") as f:
				f.write(json.dumps(log_stats) + "\n")

	total_time = time.time() - start_time
	total_time_str = str(datetime.timedelta(seconds=int(total_time)))
	print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
	args = get_args_parser()
	args = args.parse_args()
	if args.output_dir:
		Path(args.output_dir).mkdir(parents=True, exist_ok=True)
	main(args)
