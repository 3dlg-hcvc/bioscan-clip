from tqdm import tqdm
import wandb
import psutil
import torch.distributed.nn
import torch.distributed as dist





def train_epoch(activate_wandb, total_epochs, epoch, dataloader, model, optimizer, criterion, device, scheduler=None, for_open_clip=False, rank=None, check_cuda_memory=False):
    torch.autograd.set_detect_anomaly(True)
    if rank == 0:
        pbar = tqdm(enumerate(dataloader), total=len(dataloader))
    else:
        pbar = enumerate(dataloader)
    epoch_loss = 0.0
    total_step = len(dataloader)

    model.train()
    for step, batch in pbar:
        processid_batch, image_input_batch, dna_input_batch, input_ids, token_type_ids, attention_mask, label_for_train_batch = batch
        if for_open_clip:
            language_input = input_ids
        else:
            language_input = {'input_ids': input_ids.to(device), 'token_type_ids': token_type_ids.to(device),
                              'attention_mask': attention_mask.to(device)}
        optimizer.zero_grad()
        image_input_batch = image_input_batch.to(device)
        dna_input_batch = dna_input_batch.to(device)
        logit_scale = None
        logit_bias = None
        image_output, dna_output, language_output = model(image_input_batch, dna_input_batch, language_input)

        label_for_train_batch = label_for_train_batch.to(device)

        loss = criterion(image_output, dna_output, language_output, label_for_train_batch)
        loss.backward()

        optimizer.step()
        if scheduler is not None:
            scheduler.step()

        epoch_loss = epoch_loss + loss.item()

        allocated_memory = torch.cuda.memory_allocated()
        memory_total = torch.cuda.get_device_properties(device).total_memory
        cached_memory = torch.cuda.memory_reserved(device)

        total_used_memory = allocated_memory + cached_memory

        current_lr = optimizer.param_groups[0]['lr']

        if rank == 0:
            pbar.set_description(
                f'Epoch: {epoch}||Step: {step}/{total_step}||Loss: {loss.item()} || Total Used CUDA Memory: {total_used_memory / (1024 ** 3):.2f} GB || Total CUDA Memory: {memory_total / (1024 ** 3):.2f} GB || Current LR: {current_lr}')

        if activate_wandb:
            wandb.log({"loss": loss.item(), "step": step + epoch * len(dataloader), "learning_rate": current_lr})

    print(f'Epoch [{epoch}/{total_epochs}], Loss: {epoch_loss / len(dataloader)}')

