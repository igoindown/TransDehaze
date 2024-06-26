import argparse
import os
import random
import time
import warnings
from datetime import datetime
from collections import OrderedDict
import math
import random

import torch
from torch.cuda.amp.autocast_mode import autocast
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.multiprocessing as mp
import torch.cuda.amp as amp
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
from torch.utils.tensorboard import SummaryWriter
from PIL import Image
import torchvision
import torch.utils.data as DT
#import torchvision.models as models
from TD_multi import TD_base

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('-d','--data', metavar='DIR', default='./data',
                    help='path to dataset')
parser.add_argument('--eval-data', metavar='DIR', default='./data',
                    help='path to eval dataset')
parser.add_argument('-s','--save-path', metavar='DIR', default='./ckpt',
                    help='path to save checkpoints')
parser.add_argument('-j', '--workers', default=8, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=1000, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=10, type=int,
                    metavar='N',
                    help='mini-batch size (default: 256), this is the total '
                         'batch size of all GPUs on the current node when '
                         'using Data Parallel or Distributed Data Parallel')
parser.add_argument('--lr', '--learning-rate', default=0.0001, type=float,
                    metavar='LR', help='initial learning rate', dest='lr')
parser.add_argument('--lr-policy', default='naive',
                    help='lr policy')
parser.add_argument('--warmup-epochs', default=0, type=int, metavar='N',
                    help='number of warmup epochs')
parser.add_argument('--warmup-lr-multiplier', default=0.1, type=float, metavar='W',
                    help='warmup lr multiplier')
parser.add_argument('--momentum', default=0.999, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1-4)',
                    dest='weight_decay')
parser.add_argument('--power', default=1.0, type=float,
                    metavar='P', help='power for poly learning-rate decay')
parser.add_argument('-p', '--print-freq', default=16, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('--reset-epoch', action='store_true',
                    help='whether to reset epoch')
parser.add_argument('--eval', action='store_true',
                    help='only do evaluation')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--task', default='', type=str, metavar='string',
                    help='specific a task'
                    '["denoise30", "denoise50", "SRx2", "SRx3", "SRx4", "dehaze"] (default: none)')
parser.add_argument('--world-size', default=-1, type=int,
                    help='number of nodes for distributed training')
parser.add_argument('--rank', default=-1, type=int,
                    help='node rank for distributed training')
parser.add_argument('--dist-url', default='tcp://224.66.41.62:23456', type=str,
                    help='url used to set up distributed training')
parser.add_argument('--dist-backend', default='nccl', type=str,
                    help='distributed backend')
parser.add_argument('--seed', default=None, type=int,
                    help='seed for initializing training. ')
parser.add_argument('--gpu', default=None, type=int,
                    help='GPU id to use.')
parser.add_argument('--multiprocessing-distributed', action='store_true',
                    help='Use multi-processing distributed training to launch '
                         'N processes per node, which has N GPUs. This is the '
                         'fastest way to use PyTorch for either single node or '
                         'multi node data parallel training')
parser.add_argument('--fp16',action='store_true', default=False, help="\
                    use fp16 instead of fp32.")


best_acc1 = 0
# set task sets

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate_naive(optimizer, epoch, args):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = args.lr if epoch < 200 else 2 / 5 * args.lr
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


def adjust_learning_rate(optimizer, epoch, args):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = args.lr * (0.1 ** (epoch // 30))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


def adjust_learning_rate_epoch_poly(optimizer, epoch, args):
    """Sets epoch poly learning rate"""
    lr = args.lr * ((1 - epoch * 1.0 / args.epochs) ** args.power)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


def adjust_learning_rate_poly(optimizer, global_iter, args):
    """Sets iter poly learning rate"""
    lr = args.lr * ((1 - global_iter * 1.0 / (args.epochs * args.epoch_size)) ** args.power)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


def adjust_learning_rate_cosine(optimizer, global_iter, args):
    warmup_lr = args.lr * args.warmup_lr_multiplier
    max_iter = args.epochs * args.epoch_size
    warmup_iter = args.warmup_epochs * args.epoch_size
    if global_iter < warmup_iter:
        slope = (args.lr - warmup_lr) / warmup_iter
        lr = slope * global_iter + warmup_lr
    else:
        lr = 0.5 * args.lr * (1 + math.cos(math.pi * (global_iter - warmup_iter) / (max_iter - warmup_iter)))

    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr

class PSNR:
    """Peak Signal to Noise Ratio
    img1 and img2 have range [0, 255]"""

    def __init__(self):
        self.name = "PSNR"

    @staticmethod
    def __call__(img1, img2):
        mse = torch.mean((img1 - img2) ** 2)
        return 20 * torch.log10(255.0 / torch.sqrt(mse))


task_map = {"denoise30": 0, "denoise50": 1, "SRx2": 2, "SRx3": 3, "SRx4": 4, "dehaze": 5}

def train(train_loader, model, criterion, optimizer, epoch, args, scaler=None):
    # train for one epoch
    epoch_step = 0
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    psnr_out = AverageMeter()


    # switch to train mode
    model.train()

    end = time.time()

    if args.lr_policy == 'naive':
        local_lr = adjust_learning_rate_naive(optimizer, epoch, args)
    elif args.lr_policy == 'step':
        local_lr = adjust_learning_rate(optimizer, epoch, args)
    elif args.lr_policy == 'epoch_poly':
        local_lr = adjust_learning_rate_epoch_poly(optimizer, epoch, args)

    for i, (input_group, target) in enumerate(train_loader):

        # set random task
        task_id = 5
        input = input_group
        model.module.set_task(task_id)

        # print(f"Iter {i}, task_id: {task_id}")
        # for m in model.module.modules():
        # if isinstance(m, )
        # print(m.weight.device)
        global_iter = epoch * args.epoch_size + i

        if args.lr_policy == 'iter_poly':
            local_lr = adjust_learning_rate_poly(optimizer, global_iter, args)
        elif args.lr_policy == 'cosine':
            local_lr = adjust_learning_rate_cosine(optimizer, global_iter, args)

        # measure data loading time
        data_time.update(time.time() - end)

        if args.gpu is not None:
            input = input.cuda(args.gpu, non_blocking=True)
            target = target.cuda(args.gpu, non_blocking=True)

        target = target.cuda()
        if scaler is None:
            # compute output
            output = model(input)
            # print(output.device, target.device)
            loss = criterion(output, target)
            # writer.add_scalar("train_loss", loss.item(), global_step=total_train_step)
        else:
            with autocast():
                # compute output
                output = model(input)
                # print(output.device, target.device)
                loss = criterion(output, target)



        # measure accuracy and record loss
        output = (output * 0.5 + 0.5) * 255.
        target = (target * 0.5 + 0.5) * 255.
        psnr = PSNR()(output, target)


        losses.update(loss.item(), input.size(0))
        psnr_out.update(psnr.item(), input.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()

        if scaler is None:
            # compute gradient and do SGD step
            loss.backward()
            optimizer.step()
        else:
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'PSNR {psnr.val:.3f} ({psnr.avg:.3f})\t'
                  'LR: {lr: .6f}'.format(
                epoch, i, args.epoch_size, batch_time=batch_time,
                data_time=data_time, loss=losses, psnr=psnr_out, lr=local_lr))
            total_train_step = epoch * 24 + epoch_step  #每个epoch中需要运算的次数
            writer.add_scalar("train_loss", losses.val, total_train_step)
            writer.add_scalar("PSNR", psnr_out.val, total_train_step)
            epoch_step += 1
            # writer.close()




def validate(val_loader, model, criterion, args):
    batch_time = AverageMeter()
    losses = AverageMeter()
    psnr_out = AverageMeter()
    psnr_in = AverageMeter()

    # switch to evaluate mode
    model.eval()
    P = PSNR()
    with torch.no_grad():
        end = time.time()
        for i, (input_group, target) in enumerate(val_loader):
            task_id = 5
            input = input_group
            model.module.set_task(task_id)
            if args.gpu is not None:
                input = input.cuda(args.gpu, non_blocking=True)
                target = target.cuda(args.gpu, non_blocking=True)
            target = target.cuda()
            # compute output
            output = model(input)
            loss = criterion(output, target)

            # measure accuracy and record loss
            output = (output * 0.5 + 0.5) * 255.
            target = (target * 0.5 + 0.5) * 255.
            psnr1 = P(output, target)
            # psnr2 = P(input.cuda(), target)
            losses.update(loss.item(), input.size(0))
            psnr_out.update(psnr1.item(), input.size(0))
            # psnr_in.update(psnr2.item(), input.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                print('Test: [{0}/{1}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'PSNR_Out {psnr1.val:.3f} ({psnr1.avg:.3f})\t'
                      'PSNR_In {psnr2.val:.3f} ({psnr2.avg:.3f})'.format(
                    i, len(val_loader), batch_time=batch_time, loss=losses, psnr1=psnr_out, psnr2=psnr_in
                ))

        print(' * PSNR_Out {psnr1.val:.3f} ({psnr1.avg:.3f})\t'
              'PSNR_In {psnr2.val:.3f} ({psnr2.avg:.3f})'.format(psnr1=psnr_out, psnr2=psnr_in))

    return psnr_out.avg

loader = torchvision.transforms.Compose([
	transforms.ToTensor(),
	# transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
	# transforms.Normalize(mean=[0.0, 0.0, 0.0], std=[255, 255, 255]),
])
def image_data():
    enc_inputs, dec_inputs, dec_outputs = [], [], []
    # enc_inputs_o, dec_inputs_o, dec_outputs_o = [], [], []
    path_haze = 'E:\PyFile/data_ipt2_1/input/'
    path_ori = 'E:\PyFile/data_ipt2_1/ref/'
    dirs_haze = os.listdir(path_haze)
    dirs_ori = os.listdir(path_ori)
    for file in dirs_haze:
        haze_image = Image.open('E:\PyFile\\data_ipt2_1\\input\\' + file)
        # print(haze_image)
        # np_haze_image = np.array(haze_image)
        # enc_input_o = np_haze_image.reshape(-1)
        # enc_input = enc_input_o
        haze_image = loader(haze_image)

        enc_inputs.append(haze_image)

    for file in dirs_ori:
        gt_ato_map = Image.open('E:\PyFile\\data_ipt2_1\\ref\\' + file)
        # np_gt_ato_map = np.array(gt_ato_map)
        # dec_output_o = np_gt_ato_map.reshape(-1)
        # dec_input = dec_output = dec_output_o
        gt_ato_map = loader(gt_ato_map)

        dec_inputs.append(gt_ato_map)
        # dec_outputs.append(dec_output)

    return enc_inputs, dec_inputs

haze_images, ori_images = image_data()

def image_data_test():
    enc_inputs, dec_inputs, dec_outputs = [], [], []
    # enc_inputs_o, dec_inputs_o, dec_outputs_o = [], [], []
    path_haze = 'E:\PyFile/data_ipt_2/input/'
    path_ori = 'E:\PyFile/data_ipt_2/ref/'
    dirs_haze = os.listdir(path_haze)
    dirs_ori = os.listdir(path_ori)
    for file in dirs_haze:
        haze_image = Image.open('E:\PyFile\\data_ipt_2\\input\\' + file)
        # print(haze_image)
        # np_haze_image = np.array(haze_image)
        # enc_input_o = np_haze_image.reshape(-1)
        # enc_input = enc_input_o
        haze_image = loader(haze_image)

        enc_inputs.append(haze_image)

    for file in dirs_ori:
        gt_ato_map = Image.open('E:\PyFile\\data_ipt_2\\ref\\' + file)
        # np_gt_ato_map = np.array(gt_ato_map)
        # dec_output_o = np_gt_ato_map.reshape(-1)
        # dec_input = dec_output = dec_output_o
        gt_ato_map = loader(gt_ato_map)

        dec_inputs.append(gt_ato_map)
        # dec_outputs.append(dec_output)

    return enc_inputs, dec_inputs

haze_images_test, ori_images_test = image_data_test()



######################################
class MyDataSet(DT.Dataset):
    def __init__(self, haze_images, ori_images):
        super(MyDataSet, self).__init__()
        self.haze_images = haze_images
        self.ori_images = ori_images


    def __len__(self):
        return len(self.haze_images)

    def __getitem__(self, idx):
        return self.haze_images[idx], self.ori_images[idx]

class MyDataSet_test(DT.Dataset):
    def __init__(self, haze_images_test, ori_images_test):
        super(MyDataSet_test, self).__init__()
        self.haze_images_test = haze_images_test
        self.ori_images_test = ori_images_test


    def __len__(self):
        return len(self.haze_images_test)

    def __getitem__(self, idx):
        return self.haze_images_test[idx], self.ori_images_test[idx]


def main_worker(gpu, ngpus_per_node, args):
    args.gpu = gpu

    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))

    if args.distributed:
        if args.dist_url == "env://" and args.rank == -1:
            args.rank = int(os.environ["RANK"])
        if args.multiprocessing_distributed:
            # For multiprocessing distributed training, rank needs to be the
            # global rank among all the processes
            args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)

    print("=> creating model '{}'".format("ipt_base"))
    model = ipt_base().cuda()

    # define loss function (criterion) and optimizer

    # IPT uses L1 loss function
    # criterion = nn.CrossEntropyLoss().cuda(args.gpu)
    criterion = nn.L1Loss()

    optimizer = torch.optim.Adam(model.parameters(), args.lr,
                                 betas=(0.9, 0.999),
                                 weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            if not args.reset_epoch:
                args.start_epoch = checkpoint['epoch']
            # args.start_epoch = 10
            model.load_state_dict(checkpoint['state_dict'])
            # optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    if args.distributed:
        # For multiprocessing distributed, DistributedDataParallel constructor
        # should always set the single device scope, otherwise,
        # DistributedDataParallel will use all available devices.
        if args.gpu is not None:
            torch.cuda.set_device(args.gpu)
            model.cuda(args.gpu)
            # When using a single GPU per process and per
            # DistributedDataParallel, we need to divide the batch size
            # ourselves based on the total number of GPUs we have
            args.batch_size = int(args.batch_size / ngpus_per_node)
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        else:
            model.cuda()
            # DistributedDataParallel will divide and allocate batch_size to all
            # available GPUs if device_ids are not set
            model = torch.nn.parallel.DistributedDataParallel(model)
    elif args.gpu is not None:
        torch.cuda.set_device(args.gpu)
        model = model.cuda(args.gpu)
    else:
        # DataParallel will divide and allocate batch_size to all available GPUs
        model = torch.nn.DataParallel(model).cuda()
    input_size = 48

    # Data loading code

    trans = transforms.Compose([transforms.ToTensor(),
                                transforms.Normalize(mean=(0.5, 0.5, 0.5), std=(0.5, 0.5, 0.5)),
                                ])
    if args.eval:
        # val_dataset = ImageProcessDataset(args.eval_data, transform=trans)
        # val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, num_workers=16)
        # val_loader = DT.DataLoader(MyDataSet_test(haze_images_test, ori_images_test), batch_size=args.batch_size, shuffle=True, num_workers=16)
        val_loader = DT.DataLoader(MyDataSet_test(haze_images_test, ori_images_test), batch_size=args.batch_size)
        # raise RuntimeError("evaluate dataloader not implemented")
        validate(val_loader, model, criterion, args)
        return

    # train_dataset = ImageProcessDataset(args.data, transform=trans)
    # train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=16)
    # train_loader = DT.DataLoader(MyDataSet(haze_images, ori_images), batch_size=args.batch_size, shuffle=True, num_workers=16)
    train_loader = DT.DataLoader(MyDataSet(haze_images, ori_images), batch_size=args.batch_size)

    args.epoch_size = len(train_loader)
    print(f"Each epoch contains {args.epoch_size} iterations")

    print(f"Using {args.lr_policy} learning rate")

    if args.distributed:
        raise RuntimeError("distributed not implemented")
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    else:
        train_sampler = None

    scaler = amp.GradScaler() if args.fp16 else None
    print(args)
    # num_epoch = 0
    total_train_step = 0
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        # adjust_learning_rate(optimizer, epoch, args)
        # train for one epoch

        train(train_loader, model, criterion, optimizer, epoch, args, scaler)

        # evaluate on validation set
        # validate(val_loader, model, criterion, args)

        if not args.multiprocessing_distributed or (args.multiprocessing_distributed
                                                    and args.rank % ngpus_per_node == 0):
            model_to_save = getattr(model, "module", model)
            save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': model_to_save.state_dict(),
                'optimizer': optimizer.state_dict(),
            }, path=args.save_path)
        # num_epoch += 1
def main():
    args = parser.parse_args()

    now = datetime.now()
    timestr = now.strftime("%m-%d-%H_%M_%S")
    args.save_path = os.path.join(args.save_path, f"{args.task}" if args.task else "train")
    # args.save_path = os.path.join(args.save_path, timestr)
    save_path = args.save_path

    if not os.path.exists(save_path):
        os.makedirs(save_path)

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.gpu is not None:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')

    if args.dist_url == "env://" and args.world_size == -1:
        args.world_size = int(os.environ["WORLD_SIZE"])

    args.distributed = args.world_size > 1 or args.multiprocessing_distributed

    ngpus_per_node = torch.cuda.device_count()
    if args.multiprocessing_distributed:
        # Since we have ngpus_per_node processes per node, the total world_size
        # needs to be adjusted accordingly
        args.world_size = ngpus_per_node * args.world_size
        # Use torch.multiprocessing.spawn to launch distributed processes: the
        # main_worker process function
        mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args))
    else:
        # Simply call main_worker function
        main_worker(args.gpu, ngpus_per_node, args)

def save_checkpoint(state, path='./', filename='checkpoint1'):
    saved_path = os.path.join(path, filename+'.pth.tar')
    torch.save(state, saved_path)
    '''
    if is_best:
        state_dict = state['state_dict']
        new_state_dict = OrderedDict()
        best_path = os.path.join(path, 'model_best.pth')
        for key in state_dict.keys():
            if 'module.' in key:
                new_state_dict[key.replace('module.', '')] = state_dict[key].cpu()
            else:
                new_state_dict[key] = state_dict[key].cpu()
        torch.save(new_state_dict, best_path)
    '''

if __name__ == '__main__':
    writer = SummaryWriter("logs_8_4")
    main()
    writer.close()