import argparse
import scipy
from scipy import ndimage
import cv2
import numpy as np
import sys
from collections import OrderedDict
import os
from packaging import version
import os.path

import torch
import torch.nn as nn
from torch.autograd import Variable
import torchvision.models as models
import torch.nn.functional as F
from torch.utils import data, model_zoo

from model.deeplab import Res_Deeplab
from model import deeplabv3plus
from dataset.voc_dataset import VOCDataSet
import scipy.misc as sm
import imageio

import glob
from PIL import Image
from dataset.dataset import *
#from hrnet import *
from hrnet import *

import matplotlib.pyplot as plt

IMG_MEAN = np.array((104.00698793,116.66876762,122.67891434), dtype=np.float32)

MODEL = 'DeepLab'
DATA_DIRECTORY = './dataset/VOC2012'
DATA_LIST_PATH = './dataset/voc_list/val.txt'
IGNORE_LABEL = 255
NUM_CLASSES = 1
NUM_STEPS = 1449 # Number of images in the validation set.
RESTORE_FROM = 'http://vllab1.ucmerced.edu/~whung/adv-semi-seg/AdvSemiSegVOC0.125-8d75b3f1.pth'
PRETRAINED_MODEL = None
SAVE_DIRECTORY = 'results'


pretrianed_models_dict ={'semi0.125': 'http://vllab1.ucmerced.edu/~whung/adv-semi-seg/AdvSemiSegVOC0.125-03c6f81c.pth',
                         'semi0.25': 'http://vllab1.ucmerced.edu/~whung/adv-semi-seg/AdvSemiSegVOC0.25-473f8a14.pth',
                         'semi0.5': 'http://vllab1.ucmerced.edu/~whung/adv-semi-seg/AdvSemiSegVOC0.5-acf6a654.pth',
                         'advFull': 'http://vllab1.ucmerced.edu/~whung/adv-semi-seg/AdvSegVOCFull-92fbc7ee.pth'}

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
torch.cuda.set_device(0)

def get_arguments():
    """Parse all the arguments provided from the CLI.

    Returns:
      A list of parsed arguments.
    """
    parser = argparse.ArgumentParser(description="VOC evaluation script")
    parser.add_argument("--model", type=str, default=MODEL,
                        help="available options : DeepLab/DRN")
    parser.add_argument("--data-dir", type=str, default=DATA_DIRECTORY,
                        help="Path to the directory containing the PASCAL VOC dataset.")
    parser.add_argument("--data-list", type=str, default=DATA_LIST_PATH,
                        help="Path to the file listing the images in the dataset.")
    parser.add_argument("--ignore-label", type=int, default=IGNORE_LABEL,
                        help="The index of the label to ignore during the training.")
    parser.add_argument("--num-classes", type=int, default=NUM_CLASSES,
                        help="Number of classes to predict (including background).")
    parser.add_argument("--restore-from", type=str, default=RESTORE_FROM,
                        help="Where restore model parameters from.")
    parser.add_argument("--pretrained-model", type=str, default=PRETRAINED_MODEL,
                        help="Where restore model parameters from.")
    parser.add_argument("--save-dir", type=str, default=SAVE_DIRECTORY,
                        help="Directory to store results")
    parser.add_argument("--gpu", type=int, default=1,
                        help="choose gpu device.")
    return parser.parse_args()

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

def iou_score(output, target):
    smooth = 1e-5

    if torch.is_tensor(output):
        output = torch.sigmoid(output).data.cpu().numpy()
    if torch.is_tensor(target):
        target = target.data.cpu().numpy()
    output_ = output > 0.8
    target_ = target > 0.5
    intersection = (output_ & target_).sum()
    union = (output_ | target_).sum()

    return (intersection + smooth) / (union + smooth)

def dice_coef(output, target):
    smooth = 1e-5

    output = torch.sigmoid(output).view(-1).data.cpu().numpy()
    target = target.view(-1).data.cpu().numpy()
    intersection = (output * target).sum()

    return (2. * intersection + smooth) / \
        (output.sum() + target.sum() + smooth)


def get_TP_FP_FN(PR,GT):
    if torch.is_tensor(PR):
        PR = torch.sigmoid(PR).data.cpu().numpy()
    if torch.is_tensor(GT):
        GT = GT.data.cpu().numpy()
    PR = PR > 0.1
    GT = GT > 0.5
    #print(PR)
    #print(GT)
    #PR = PR > threshold
    #GT = GT == torch.max(GT)
    TP = (PR & GT).sum()
    FP = (PR | GT).sum() - GT.sum()
    FN = (PR | GT).sum() - PR.sum()
    #Precision = float(torch.sum(TP))/(float(torch.sum(TP)+torch.sum(FP)) + 1e-6)
    return TP, FP, FN

class VOCColorize(object):
    def __init__(self, n=1):
        self.cmap = color_map(1)
        self.cmap = torch.from_numpy(self.cmap[:n])

    def __call__(self, gray_image):
        size = gray_image.shape
        color_image = np.zeros((3, size[0], size[1]), dtype=np.uint8)

        for label in range(0, len(self.cmap)):
            mask = (label == gray_image)
            color_image[0][mask] = self.cmap[label][0]
            color_image[1][mask] = self.cmap[label][1]
            color_image[2][mask] = self.cmap[label][2]

        # handle void
        mask = (255 == gray_image)
        color_image[0][mask] = color_image[1][mask] = color_image[2][mask] = 255

        return color_image

def color_map(N=256, normalized=False):
    def bitget(byteval, idx):
        return ((byteval & (1 << idx)) != 0)

    dtype = 'float32' if normalized else 'uint8'
    cmap = np.zeros((N, 3), dtype=dtype)
    for i in range(N):
        r = g = b = 0
        c = i
        for j in range(8):
            r = r | (bitget(c, 0) << 7-j)
            g = g | (bitget(c, 1) << 7-j)
            b = b | (bitget(c, 2) << 7-j)
            c = c >> 3

        cmap[i] = np.array([r, g, b])

    cmap = cmap/255 if normalized else cmap
    return cmap

def decode_segmap(pred):

    pred = np.squeeze(pred)
    R = pred.copy()
    G = pred.copy()
    B = pred.copy()
    R[R>0.2] = 255
    img = np.zeros((pred.shape[0], pred.shape[1],3))
    img[:,:,0] = R
    img[:,:,1] = 0
    img[:,:,2] = 0

    return img

def get_iou(data_list, class_num, save_path=None):
    from multiprocessing import Pool
    from utils.metric import ConfusionMatrix

    ConfM = ConfusionMatrix(class_num)
    f = ConfM.generateM
    pool = Pool()
    m_list = pool.map(f, data_list)
    pool.close()
    pool.join()

    for m in m_list:
        ConfM.addM(m)

    aveJ, j_list, M = ConfM.jaccard()
    print(j_list)
    '''
    classes = np.array(('background',  # always index 0
               'aeroplane', 'bicycle', 'bird', 'boat',
               'bottle', 'bus', 'car', 'cat', 'chair',
                         'cow', 'diningtable', 'dog', 'horse',
                         'motorbike', 'person', 'pottedplant',
                         'sheep', 'sofa', 'train', 'tvmonitor'))
    '''
    classes = np.array(('PE'))

    for i, iou in enumerate(j_list):
        print('class {:2d} {:12} IU {:.2f}'.format(i, classes[i], j_list[i]))


    print('meanIOU: ' + str(aveJ) + '\n')
    if save_path:
        with open(save_path, 'w') as f:
            for i, iou in enumerate(j_list):
                f.write('class {:2d} {:12} IU {:.2f}'.format(i, classes[i], j_list[i]) + '\n')
            f.write('meanIOU: ' + str(aveJ) + '\n')

def show_all(gt, pred):
    import matplotlib.pyplot as plt
    from matplotlib import colors
    from mpl_toolkits.axes_grid1 import make_axes_locatable

    fig, axes = plt.subplots(1, 2)
    ax1, ax2 = axes
    '''
    classes = np.array(('background',  # always index 0
               'aeroplane', 'bicycle', 'bird', 'boat',
               'bottle', 'bus', 'car', 'cat', 'chair',
                         'cow', 'diningtable', 'dog', 'horse',
                         'motorbike', 'person', 'pottedplant',
                         'sheep', 'sofa', 'train', 'tvmonitor'))
    '''
    classes = np.array(('PE'))
    '''
    colormap = [(0,0,0),(0.5,0,0),(0,0.5,0),(0.5,0.5,0),(0,0,0.5),(0.5,0,0.5),(0,0.5,0.5),
                    (0.5,0.5,0.5),(0.25,0,0),(0.75,0,0),(0.25,0.5,0),(0.75,0.5,0),(0.25,0,0.5),
                    (0.75,0,0.5),(0.25,0.5,0.5),(0.75,0.5,0.5),(0,0.25,0),(0.5,0.25,0),(0,0.75,0),
                    (0.5,0.75,0),(0,0.25,0.5)]
    '''
    colormap = [(0,0,0)]
    cmap = colors.ListedColormap(colormap)
    #bounds=[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21]
    bounds=[0]
    norm = colors.BoundaryNorm(bounds, cmap.N)

    ax1.set_title('gt')
    ax1.imshow(gt, cmap=cmap, norm=norm)

    ax2.set_title('pred')
    ax2.imshow(pred, cmap=cmap, norm=norm)

    plt.show()

def main():
    """Create the model and start the evaluation process."""
    args = get_arguments()

    #gpu0 = args.gpu

    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)


    #model = deeplabv3plus.DeepLabv3_plus(nInputChannels=1, n_classes=1, os=16, pretrained=False)
    model = hrnetv2(False, n_class=1, decoder='Original', use_softmax=False)
    '''
    if args.pretrained_model != None:
        args.restore_from = pretrianed_models_dict[args.pretrained_model]

    if args.restore_from[:4] == 'http' :
        saved_state_dict = model_zoo.load_url(args.restore_from)
    else:
        saved_state_dict = torch.load(args.restore_from)
    '''
    #model.load_state_dict(torch.load("/home/mel/tingwei/PE_seg/AdvSemiSeg/snapshots/g_deeplabv3plus_every650.pth"))
    model.load_state_dict(torch.load("/home/mel/tingwei/PE_seg/AdvSemiSeg/snapshots/0308g_hr_hu_wos4150.pth"))

    model.eval()
    model.to(device)

    # load data
    #val_folder = glob.glob('/home/mel/tingwei/PE_seg/open_dataset_PE/selected_t/val/*')
    val_folder = glob.glob('/home/mel/tingwei/PE_seg/CMUH_valid/*')
    #val_folder = glob.glob("/home/mel/tingwei/PE_seg/val_ncku/*")

    val_img_path = []
    val_label_path = []

    for x in val_folder:
        for i in list(glob.glob(x + '/*dcm')):
        #for i in list(glob.glob(x + '/*dcm')):
            val_img_path.append(i)
            name = i.split('.')[0].split('/')[-1]
            val_label_path.append(x +'/'+ name +'.png')
            
    valset = train_labeled(val_img_path, val_label_path)
    #valset = train_unlabeled(val_img_path)

    testloader = DataLoader(valset,batch_size=1)

    if version.parse(torch.__version__) >= version.parse('0.4.0'):
        interp = nn.Upsample(size=(400, 400), mode='bilinear', align_corners=True)
    else:
        interp = nn.Upsample(size=(400, 400), mode='bilinear')
    data_list = []

    colorize = VOCColorize()
    avg_meter = AverageMeter()
    avg_meter2 = AverageMeter()
    avg_meter3 = AverageMeter()
    smooth = 1e-5

    for index, batch in enumerate(testloader):
        if index % 100 == 0:
            print('%d processd'%(index))
        image, target_n, target, name = batch
        #image, name = batch
        #target = target.to(device)
        #size = size[0].numpy()
        output, fmap = model(Variable(image, volatile=True).to(device))
        output_mask = torch.sigmoid(output).cpu().data.numpy()


        iou = iou_score(output, target)
        avg_meter.update(iou, image.size(0))
        ######################################################## evaluation
        dice_co = dice_coef(output, target)
        tp, fp, fn = get_TP_FP_FN(output, target)
        #print(tp, fp, fn)
        SE = tp/(tp + fn + smooth)
        SE = SE.mean()

        avg_meter2.update(dice_co, image.size(0))
        avg_meter3.update(SE, image.size(0))

        output = interp(output).cpu().data.numpy()
        target = target.cpu().numpy()
        #print('check1', output.shape)

        output = output[0]
        #gt = np.asarray(label.numpy(), dtype=np.int)
        #print('check2', gt.shape)

        output = output.transpose(1,2,0)
        output = np.asarray(np.argmax(output, axis=2), dtype=np.int)

        input_d = image.cpu().numpy()
        pmax = np.max(input_d)
        pmin = np.min(input_d)

        # 轉換為 0 ~ 255 的像素值
        rescale = (input_d - pmin) / (pmax - pmin)*255
        image_uint8 = rescale.astype(np.uint8)
        
        for i in range(len(output_mask)):
            for c in range(1):
                #print(name[i].split('/')[7])
                #filename = os.path.join(args.save_dir, '{}.png'.format(name[i].split('/')[6] +'_'+ name[i].split('/')[7] +'_'+ name[i].split('/')[8]))
                #filename_t = os.path.join(args.save_dir, '{}.png'.format(name[i].split('/')[8] +'_'+ name[i].split('/')[9] +'_'+ name[i].split('/')[10]+'t'))
                #filename_img = os.path.join(args.save_dir, '{}.png'.format(name[i].split('/')[6] +'_'+ name[i].split('/')[7] +'_'+ name[i].split('/')[8]+'_maskgt'))
                #filename_mask = os.path.join(args.save_dir, '{}.png'.format(name[i].split('/')[6] +'_'+ name[i].split('/')[7] +'_'+ name[i].split('/')[8]+'_mask'))

                filename = os.path.join(args.save_dir, '{}.png'.format(name[i].split('/')[6] +'_'+ name[i].split('/')[7]))
                #filename_t = os.path.join(args.save_dir, '{}.png'.format(name[i].split('/')[7]+'t'))
                #filename_img = os.path.join(args.save_dir, '{}.png'.format(name[i].split('/')[6] +'_'+ name[i].split('/')[7] +'_maskgt'))
                filename_mask = os.path.join(args.save_dir, '{}.png'.format(name[i].split('/')[6] +'_'+ name[i].split('/')[7]+'_mask'))
                
                out = output_mask[i,c]
                target_gt = target[i,c]
                #print(out.shape)
                w, h = out.shape
                for element in out[272:h, 0:w]:
                    for k in range(len(element)):
                        element[k] = 0.0
                    #print(element)
                out[272:h, 0:w] = out[272:h, 0:w]*element
            
                out_b = decode_segmap(out)
                target_gt = decode_segmap(target_gt)


                mask = image_uint8[i].transpose(1,2,0) + out_b*0.6
                mask_gt = image_uint8[i].transpose(1,2,0) + target_gt*0.6

                #cv2.imwrite(filename_t, out_b.astype('uint8'))
                #cv2.imwrite(filename, (output_mask[i, c] * 255).astype('uint8'))
                #sm.imsave(filename_mask, mask)
                #result = np.hstack([image_uint8[i].transpose(1,2,0), mask])
                #imageio.imsave(filename_img, mask_gt)
                imageio.imsave(filename_mask, mask)
        #color_file = Image.fromarray(colorize(output).transpose(1, 2, 0), 'RGB')
        #color_file.save(filename)

        # show_all(gt, output)
        #data_list.append([gt.flatten(), output.flatten()])

    filename = os.path.join(args.save_dir, 'result.txt')
    print('Dice coefficient: %.4f' % avg_meter2.avg)
    print('Sensitivity: %.4f' % avg_meter3.avg)
    #get_iou(data_list, args.num_classes, filename)
    print('IoU: %.4f' % avg_meter.avg)


if __name__ == '__main__':
    main()
