import torch
from model.base_model import BaseModel
from model.networks import base_function, external_function
import model.networks as network
from util import task, util
import itertools
import data as Dataset
import numpy as np
from itertools import islice
import random
import os



class ShapeNetFlow(BaseModel):
    def name(self):
        return "ShapeNetFlow Pre-train Model"

    @staticmethod
    def modify_options(parser, is_train=True):
        parser.add_argument('--netG', type=str, default='shapenetflownet', help='The name of net Generator')
        parser.add_argument('--init_type', type=str, default='orthogonal', help='Initial type')

        parser.add_argument('--attn_layer', action=util.StoreList, metavar="VAL1,VAL2...")
        parser.add_argument('--kernel_size', action=util.StoreDictKeyPair, metavar="KEY1=VAL1,KEY2=VAL2...") 

        parser.add_argument('--lambda_correct', type=float, default=20.0, help='weight for generation loss')
        parser.add_argument('--lambda_regularization', type=float, default=0.01, help='weight for generation loss')
        parser.add_argument('--use_spect_g', action='store_false')

        parser.set_defaults(use_spect_g=False)

        return parser


    def __init__(self, opt):
        BaseModel.__init__(self, opt)
        self.loss_names = ['correctness','regularization']
        self.visual_names = ['input_P1','input_P2', 'warp', 'flow_fields',
                            'masks']
        self.model_names = ['G']

        self.FloatTensor = torch.cuda.FloatTensor if len(self.gpu_ids)>0 \
            else torch.FloatTensor
        self.ByteTensor = torch.cuda.ByteTensor if len(self.gpu_ids)>0 \
            else torch.ByteTensor
        self.net_G = network.define_g(opt, structure_nc=opt.structure_nc, ngf=32, img_f=256, 
                                       encoder_layer=5, norm='instance', activation='LeakyReLU', 
                                       attn_layer=self.opt.attn_layer, use_spect=opt.use_spect_g,
                                       )
        self.L1loss = torch.nn.L1Loss()
        self.flow2color = util.flow2color()

        if self.isTrain:
            self.Correctness = external_function.PerceptualCorrectness().to(opt.device)
            self.Regularization = external_function.MultiAffineRegularizationLoss(kz_dic=opt.kernel_size).to(opt.device)
            self.optimizer_G = torch.optim.Adam(itertools.chain(filter(lambda p: p.requires_grad, self.net_G.parameters())),
                                                lr=opt.lr, betas=(0.0, 0.999))
            self.optimizers.append(self.optimizer_G)
        self.setup(opt)


    def set_input(self, input):
        # move to GPU and change data types
        self.input = input
        input_P1, input_BP1 = input['P1'], input['BP1']
        input_P2, input_BP2 = input['P2'], input['BP2']

        if len(self.gpu_ids) > 0:
            self.input_P1 = input_P1.cuda(self.gpu_ids[0], async=True)
            self.input_BP1 = input_BP1.cuda(self.gpu_ids[0], async=True)
            self.input_P2 = input_P2.cuda(self.gpu_ids[0], async=True)
            self.input_BP2 = input_BP2.cuda(self.gpu_ids[0], async=True)        

        self.input_BP1 = self.obtain_shape_net_semantic(self.input_BP1)
        self.input_BP2 = self.obtain_shape_net_semantic(self.input_BP2)

    def obtain_shape_net_semantic(self, inputs):
        inputs_h = inputs[:,0,:,:].unsqueeze(1)/2
        inputs_v = inputs[:,1,:,:].unsqueeze(1)/10
        semanctic_h = self.label2semantic(inputs_h, self.opt.label_nc_h)
        semanctic_v = self.label2semantic(inputs_v, self.opt.label_nc_v)
        return torch.cat((semanctic_h, semanctic_v), 1)

    def label2semantic(self, label, nc):
        bs, _, h, w = label.size()
        input_label = self.FloatTensor(bs, nc, h, w).zero_()
        semantics = input_label.scatter_(1, label, 1.0)
        return semantics

    def forward(self):
        """Run forward processing to get the inputs"""
        self.flow_fields, self.masks = self.net_G(self.input_P1, self.input_BP1, self.input_BP2)
        self.warp  = self.visi(self.flow_fields[-1])

    def visi(self, flow_field):
        [b,_,h,w] = flow_field.size()
        flow = flow_field
        source_copy = torch.nn.functional.interpolate(self.input_P1, (w,h))
        x = torch.arange(w).view(1, -1).expand(h, -1)
        y = torch.arange(h).view(-1, 1).expand(-1, w)
        grid = torch.stack([x,y], dim=0).float().cuda()
        grid = grid.unsqueeze(0).expand(b, -1, -1, -1)
        grid = 2*grid/(w-1) - 1
        flow = 2*flow/(w-1)
        grid = (grid+flow).permute(0, 2, 3, 1)
        warp = torch.nn.functional.grid_sample(source_copy, grid)
        return  warp



    def backward_G(self):
        """Calculate training loss for the generator"""
        loss_correctness = self.Correctness(self.input_P2, self.input_P1, self.flow_fields, self.opt.attn_layer)
        self.loss_correctness = loss_correctness * self.opt.lambda_correct

        loss_regularization = self.Regularization(self.flow_fields)
        self.loss_regularization = loss_regularization * self.opt.lambda_regularization

        total_loss = 0
        for name in self.loss_names:
            if name != 'dis_img_rec' and name != 'dis_img_gen':
                total_loss += getattr(self, "loss_" + name)

        total_loss.backward()

    def optimize_parameters(self):
        self.forward()

        self.optimizer_G.zero_grad()
        self.backward_G()
        self.optimizer_G.step()


