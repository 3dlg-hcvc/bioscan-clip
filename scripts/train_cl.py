import copy
import datetime
import json
import os
import hydra
import torch
import torch.multiprocessing as mp
import torch.nn as nn
import torch.optim as optim

import wandb
from omegaconf import DictConfig, OmegaConf
import torch.distributed as dist

from bioscanclip.epoch.train_epoch import train_epoch
from inference_and_eval import get_features_and_label, inference_and_print_result
from bioscanclip.model.loss_func import ContrastiveLoss, ClipLoss
from bioscanclip.model.simple_clip import load_clip_model
from bioscanclip.util.dataset import load_dataloader, load_insect_dataloader
import numpy as np
from omegaconf import OmegaConf, open_dict
import torch.optim.lr_scheduler as lr_scheduler


def print_when_rank_zero(message, rank=0):
    if rank is None or rank == 0:
        print(message)

def broadcast_model(model, rank):
    for param in model.parameters():
        dist.broadcast(param.data, src=0)

def save_prediction(pred_list, gt_list, json_path):
    data = {
        "gt_labels": gt_list,
        "pred_labels": pred_list
    }

    with open(json_path, 'w') as json_file:
        json.dump(data, json_file)

def ddp_setup(rank: int, world_size: int, port):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = str(port)
    torch.distributed.init_process_group("nccl", rank=rank, world_size=world_size)
    torch.cuda.set_device(rank)


def construct_key_dict(list_of_dict):
    key_dict = {}

    for curr_dict in list_of_dict:
        for a_kind_of_feature_or_label in curr_dict.keys():
            if a_kind_of_feature_or_label == "all_key_features" or a_kind_of_feature_or_label == "all_key_features_label":
                key_dict[a_kind_of_feature_or_label] = None
                continue

            if a_kind_of_feature_or_label not in key_dict.keys():
                key_dict[a_kind_of_feature_or_label] = curr_dict[a_kind_of_feature_or_label]
            else:
                if isinstance(curr_dict[a_kind_of_feature_or_label], list):
                    key_dict[a_kind_of_feature_or_label] = key_dict[a_kind_of_feature_or_label] + curr_dict[
                        a_kind_of_feature_or_label]
                else:
                    key_dict[a_kind_of_feature_or_label] = np.concatenate(
                        (key_dict[a_kind_of_feature_or_label], curr_dict[a_kind_of_feature_or_label]), axis=0)

    return key_dict

def eval_phase(model, device, all_keys_dataloader, seen_val_dataloader, unseen_val_dataloader, k_list, args,
               species_to_drop=None, rank=None, for_open_clip=False):
    keys_dict = get_features_and_label(
        all_keys_dataloader, model, device, for_key_set=True, for_open_clip=for_open_clip)

    seen_val_dict = get_features_and_label(
        seen_val_dataloader, model, device, for_open_clip=for_open_clip)

    unseen_val_dict = get_features_and_label(
        unseen_val_dataloader, model, device, for_open_clip=for_open_clip)

    acc_dict, _, pred_dict = inference_and_print_result(keys_dict, seen_val_dict, unseen_val_dict, args=args,
                                                        small_species_list=None, k_list=k_list)
    return acc_dict, pred_dict

def eval_phase_for_insect(model, device, insect_train_dataloader_for_key, insect_val_dataloader, insect_test_seen_dataloader, insect_test_unseen_dataloader, k_list, args,
               species_to_drop=None):
    insect_train_dict = get_features_and_label(
        insect_train_dataloader_for_key, model, device)
    insect_val_dict = get_features_and_label(
        insect_val_dataloader, model, device)
    insect_test_seen_dict = get_features_and_label(
        insect_test_seen_dataloader, model, device)
    insect_test_unseen_dict = get_features_and_label(
        insect_test_unseen_dataloader, model, device)

    keys_dict = construct_key_dict([insect_train_dict, insect_val_dict, insect_test_seen_dict, insect_test_unseen_dict])

    acc_dict, _, pred_dict = inference_and_print_result(keys_dict, insect_test_seen_dict, insect_test_unseen_dict, args=args,
                                                           small_species_list=None, k_list=k_list)

    return acc_dict, pred_dict

def convert_acc_dict_to_wandb_dict(acc_dict):
    dict_for_wandb = {}
    acc_dict = acc_dict['encoded_image_feature']['encoded_image_feature']

    # For now, we just put query: image and key:image acc to wandb

    for split, split_dict in acc_dict.items():
        for type_of_acc, type_of_acc_dict in split_dict.items():
            for k, k_dict in type_of_acc_dict.items():
                for level, curr_acc in split_dict.items():
                    dict_for_wandb[f"{split} {type_of_acc} top-{k} {level} level"] = curr_acc

    return dict_for_wandb

def main_process(rank: int, world_size: int, args):

    if args.debug_flag or rank != 0:
        args.activate_wandb = False
        args.save_inference = False
        args.save_ckpt = False

    current_datetime = datetime.datetime.now()
    formatted_datetime = current_datetime.strftime("%Y-%m-%d_%H%M%S")
    args = copy.deepcopy(args)

    with open_dict(args.model_config):
        if not hasattr(args.model_config, "for_open_clip"):
            args.model_config.for_open_clip = False

    ddp_setup(rank, world_size, str(args.model_config.port))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if rank == 0:
        print("Construct dataloader...")

    if hasattr(args.model_config, 'dataset') and args.model_config.dataset == "INSECT":
        insect_train_dataloader, insect_train_dataloader_for_key, insect_val_dataloader, insect_test_seen_dataloader, insect_test_unseen_dataloader = load_insect_dataloader(
            args, world_size=world_size, rank=rank)
        pre_train_dataloader = insect_train_dataloader
    else:
        pre_train_dataloader, seen_val_dataloader, unseen_val_dataloader, all_keys_dataloader = load_dataloader(
            args, world_size=world_size, rank=rank)

    if rank == 0:
        print("Initialize model...")
    model = load_clip_model(args)
    model = model.to(device)
    broadcast_model(model, rank)

    total_steps = len(pre_train_dataloader) * args.model_config.epochs

    lr = 0.001

    if hasattr(args.model_config, 'lr_config') and hasattr(args.model_config.lr_config, 'lr'):
        lr = args.model_config.lr_config.lr

    optimizer = optim.AdamW(model.parameters(), lr=lr)
    scheduler =None
    if hasattr(args.model_config, 'lr_scheduler'):
        if args.model_config.lr_scheduler == 'one_cycle':
            max_lr = 0.001
            if hasattr(args.model_config.lr_config, 'max_lr'):
                max_lr = args.model_config.lr_config.max_lr
            scheduler = lr_scheduler.OneCycleLR(
                optimizer,
                max_lr=max_lr,
                total_steps=total_steps,
                pct_start=0.3,
                anneal_strategy='cos',
                cycle_momentum=False,
            )
        elif args.model_config.lr_scheduler == 'exponential':
            scheduler = lr_scheduler.ExponentialLR(optimizer, gamma=0.95)
        elif args.model_config.lr_scheduler == 'step':
            scheduler = lr_scheduler.StepLR(optimizer, step_size=10, gamma=0.5)
        elif args.model_config.lr_scheduler == 'cosine':
            min_lr = 1e-9
            if hasattr(args.model_config.lr_config, 'min_lr'):
                min_lr = args.model_config.lr_config.min_lr
            scheduler = lr_scheduler.CosineAnnealingLR(optimizer, T_max=total_steps, eta_min=min_lr)

    for_open_clip = False
    if hasattr(args.model_config, 'for_open_clip') and args.model_config.for_open_clip:
        for_open_clip = True

    if for_open_clip:
        criterion = ClipLoss(criterion=nn.CrossEntropyLoss(), logit_scale=1 / 0.07)
    else:
        criterion = ContrastiveLoss(criterion=nn.CrossEntropyLoss(), logit_scale=1 / 0.07)


    if args.activate_wandb:
        wandb.init(project=args.model_config.wandb_project_name, name=args.model_config.model_output_name)
    if rank == 0:
        print("training...")

    k_list = [1, 3, 5]

    best_epoch = None
    best_overall_acc = None
    folder_path = os.path.join(args.project_root_path, args.model_output_dir,
                               args.model_config.model_output_name, formatted_datetime)
    os.makedirs(folder_path, exist_ok=True)

    OmegaConf.save(args, os.path.join(folder_path, 'config.yaml'))

    for epoch in range(args.model_config.epochs):
        if hasattr(args.model_config, 'dataset') and args.model_config.dataset == "INSECT":
            train_epoch(args.activate_wandb, args.model_config.epochs, epoch, insect_train_dataloader, model, optimizer,
                        criterion, device, rank=rank, scheduler=scheduler, for_open_clip=for_open_clip)
        else:
            train_epoch(args.activate_wandb, args.model_config.epochs, epoch, pre_train_dataloader, model, optimizer,
                        criterion, device, rank=rank, scheduler=scheduler, for_open_clip=for_open_clip)


        if (epoch % args.model_config.evaluation_period == 0 or epoch == args.model_config.epochs - 1) and rank == 0:
            if args.save_ckpt:
                last_ckpt_path = os.path.join(folder_path, f'last.pth')
                torch.save(model.state_dict(), last_ckpt_path)
                print(f'Last ckpt: {last_ckpt_path}')

            if hasattr(args.model_config, 'dataset') and args.model_config.dataset == "INSECT":
                acc_dict, pred_dict = eval_phase(model, device, insect_train_dataloader_for_key, insect_val_dataloader,
                                                 insect_test_seen_dataloader, insect_test_unseen_dataloader, k_list, args=args, for_open_clip=for_open_clip)
            else:
                acc_dict, pred_dict = eval_phase(model, device, all_keys_dataloader, seen_val_dataloader, unseen_val_dataloader, k_list, rank=rank, args=args, for_open_clip=for_open_clip)

            dict_for_wandb = convert_acc_dict_to_wandb_dict(acc_dict)
            dict_for_wandb['epoch'] = epoch
            overall_acc = (acc_dict['encoded_image_feature']['encoded_image_feature']['seen']['micro_acc'][1]['species'] + acc_dict['encoded_image_feature']['encoded_image_feature']['unseen']['micro_acc'][1]['species'])/2
            if best_overall_acc is None or best_overall_acc < overall_acc:
                best_epoch = epoch
                best_overall_acc = overall_acc
                if args.save_ckpt:
                    best_ckpt_path = os.path.join(folder_path, f'best.pth')
                    torch.save(model.state_dict(), best_ckpt_path)
                    print(f'Best ckpt: {best_ckpt_path}')
            dict_for_wandb["overall_acc"] = overall_acc
            dict_for_wandb["best_epoch"] = best_epoch
            if args.activate_wandb:
                wandb.log(dict_for_wandb,
                          commit=True)

@hydra.main(config_path="../bioscanclip/config", config_name="global_config", version_base="1.1")
def main(args: DictConfig) -> None:
    torch.cuda.empty_cache()
    args.project_root_path = os.path.dirname(os.path.dirname(os.path.dirname(os.getcwd())))
    world_size = torch.cuda.device_count()
    print(f'world_size： {world_size}')

    mp.spawn(main_process, args=(world_size, args), nprocs=world_size)


if __name__ == '__main__':
    main()
