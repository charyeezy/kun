
#!/usr/bin/env python3
# -*- coding:utf-8 -*-
###
# Created Date: Thursday, August 22nd 2019, 11:50:30 am
# Author: Charlene Leong leongchar@myvuw.ac.nz
# Last Modified: Tue Oct 15 2019
###

import os
from datetime import datetime
import json

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision.utils import save_image, make_grid
from torch.utils.tensorboard import SummaryWriter

from sklearn.preprocessing import MinMaxScaler
from sklearn.manifold import TSNE
from umap import UMAP

from .utils.plt import plt_scatter
from .utils.early_stopping import EarlyStopping

MODEL = os.path.basename(__file__).split('.')[0]
SEED = 489


class AutoEncoder(nn.Module):
    def __init__(self, tb=None, job=None):
        super(AutoEncoder, self).__init__() 
        self.encoder = nn.Sequential(
            nn.Linear(28*28, 500),
            nn.ReLU(inplace=True),  # modify input directly     
            nn.Linear(500, 500),
            nn.ReLU(inplace=True),
            nn.Linear(500, 2000),
            nn.ReLU(inplace=True),
            nn.Linear(2000, 10),   # compress to 10 features, can use further method to vis
        )
        self.decoder = nn.Sequential(
            nn.Linear(10, 2000),
            nn.ReLU(inplace=True),
            nn.Linear(2000, 500),
            nn.ReLU(inplace=True),
            nn.Linear(500, 500),
            nn.ReLU(inplace=True),
            nn.Linear(500, 28*28),
            nn.Sigmoid(),           # compress to a range (0, 1)
        )

        # Init the weights and biases in the layers
        self.apply(self._init_weights)

        # Shifting to GPU if available
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.to(self.device)

        # Tensorboard SummaryWriter event log
        self.tb = tb

        # RQ Job
        self.job = job

    def __repr__(self):
        return '<{}> \n{} \n{} \n\n{}'.format(__class__.__name__, self.encoder, self.decoder, self.device)
        
    def forward(self, batch):                   # batch=x
        encoded = self.encoder(batch)           # z
        decoded = self.decoder(encoded)         # recon_x (x_hat)
        return encoded, decoded

    def gen_tb(self, output_dir, lr, batch_size, ):
        base_output_dir = os.path.join(output_dir, '..')    # output folder
        output_dir = os.path.basename(os.path.normpath(output_dir))
        comment ='{}_lr={}_bs={}'.format(output_dir, lr, batch_size)
        log_dir_name = os.path.join(base_output_dir, 'tb_runs', comment)
        return SummaryWriter(log_dir=log_dir_name)    # Tensorboard
        
    def fit(self, dataset, batch_size, max_epochs, lr, opt='Adam', loss='BCE', patience=0,  
                eval=True, plt_imgs=None, scatter_plt=None, pltshow=False, output_dir='', save_model=False):

        self.EPOCH = 1
        self.BATCH_SIZE = batch_size
        self.LR = lr
        self.OUTPUT_DIR = output_dir
        if self.tb==None:  
            self.tb = self.gen_tb(output_dir, lr, batch_size)

        # Data Loader for easy mini-batch return in training, the image batch shape will be (BATCH_SIZE, 1, 28, 28)
        train_loader = DataLoader(dataset=dataset.train, batch_size=self.BATCH_SIZE, shuffle=True, num_workers=4)

        if opt=='Adam':
            self.optimizer = torch.optim.Adam(self.parameters(), lr=self.LR)
        elif opt=='SGD':
            self.optimizer = torch.optim.SGD(self.parameters(), lr=self.LR, momentum=0.9)
          
        if loss=='BCE':     # TODO: Investigate BCE or MSE loss
            self.loss_fn = nn.BCELoss()
        elif loss=='MSE':
            self.loss_fn = nn.MSELoss()

        # =================== TENSORBOARD ===================== #
        images, _ = next(iter(train_loader))    # first batch
        self.tb.add_image('batch_images_{}'.format(images.numpy().shape), make_grid(images))
        if plt_imgs!=None:
            view_data = images.to(self.device)  # Decode to show training
            row = 2
            decoded_plt = view_data[row*plt_imgs[0]:row*plt_imgs[0]+plt_imgs[0]].cpu()

        # =================== TRAIN ===================== #
        es = EarlyStopping(tol = 0.001, patience=patience)
        self.train()        # Set to train mode
        start_epoch = self.EPOCH    # To continue training 
        for epoch in range(start_epoch, start_epoch+max_epochs):  # start epoch is 1
            train_feat = []
            train_labels = []
            train_imgs = []
            train_loss = 0      # printing intermediary loss
            self.EPOCH = epoch
            for batch_idx, (batch_train, batch_train_label) in enumerate(train_loader):
                n_iter = (self.EPOCH * len(train_loader)) + batch_idx
                batch_train = batch_train.to(self.device)               # moving batch to GPU if available
                # Flatten inputs
                batch_x = batch_train.view(batch_train.size(0), -1)     # (batch, 28*28)
                batch_y = batch_train.view(batch_train.size(0), -1)     # (batch, 28*28)
                
                # =================== forward ===================== #
                encoded, decoded = self.forward(batch_x)
                self.loss = self.loss_fn(decoded, batch_y)     # Loss between Label
                MSE_loss = nn.MSELoss()(decoded, batch_y)   # mean square error
                # =================== backward ==================== #
                self.optimizer.zero_grad()               # clear gradients for this training step
                self.loss.backward()                     # backpropagation, compute gradients
                self.optimizer.step()                    # apply gradients

                train_loss += self.loss.item()*batch_train.size(0)
                
                train_feat.append(encoded.data.cpu().view(batch_train.size(0), -1))
                train_labels.append(batch_train_label)
                train_imgs.append(batch_train.cpu())

                 # =================== Report progress ==================== #
                if batch_idx % 10 == 0:
                    print('Train Epoch: {} [{}/{} ({:.0f}%)] \t Batch Loss:{:.6f} \t MSE Loss:{:.6f} '.format(
                        self.EPOCH, batch_idx * len(batch_train), len(train_loader.dataset),
                            100.0 * batch_idx / len(train_loader),
                            self.loss.item() / len(batch_train),
                            MSE_loss.data / len(batch_train)
                    ))
                    
                    # Report for rq worker
                    if self.job != None:
                        self.job.meta['epoch'] = self.EPOCH
                        self.job.meta['epoch_progress'] = '{}/{}'.format(batch_idx * len(batch_train), len(train_loader.dataset))
                        self.job.meta['progress'] ='{:.0f}'.format(100.0 * batch_idx / len(train_loader))
                        self.job.save_meta()

            train_loss /= len(train_loader.dataset)
            print('\n====> Epoch: {} Average loss: {:.4f}'.format(self.EPOCH, train_loss))

            # Report for rq worker
            if self.job != None:
                self.job.meta['EPOCH'] = self.EPOCH
                self.job.meta['epoch_progress'] = '{}/{}'.format(len(train_loader.dataset), len(train_loader.dataset))
                self.job.meta['progress'] = 100
                self.job.meta['train_loss'] = '{:.4f}'.format(train_loss)
                self.job.meta['NUM_BAD_EPOCHS'] = es.num_bad_epochs+1
                self.job.save_meta()
           
            # =================== TENSORBOARD ===================== #
            self.tb.add_scalar('Train Loss', train_loss, self.EPOCH)
            for name, weight in self.named_parameters():
                self.tb.add_histogram(name, weight, self.EPOCH)
                self.tb.add_histogram(f'{name}.grad', weight.grad, self.EPOCH)
        
            # =================== EVAL MODEL ==================== #
            ## Plot decoded img
            if plt_imgs!=None and self.EPOCH % plt_imgs[1] == 0:
                view_data = view_data.view(self.BATCH_SIZE, -1)
                encoded, decoded = self.forward(view_data) 
                decoded = decoded[row*plt_imgs[0]:row*plt_imgs[0]+plt_imgs[0]].view(-1, 1, 28, 28).cpu()
                decoded_plt = torch.cat((decoded_plt, decoded), dim=0)

            if eval:
                test_loss, _, _, _ = self.eval_model(dataset, plt_imgs, scatter_plt, pltshow, self.OUTPUT_DIR)
                if es.step(test_loss):  # Early Stopping
                    # Check whether to plt last epoch
                    if (plt_imgs != None and self.EPOCH % plt_imgs[1] !=0): 
                        plt_imgs = (plt_imgs[0], self.EPOCH)        # (N_TEST_IMGS, plt_interval)
                    elif (scatter_plt != None and self.EPOCH % scatter_plt[1] !=0):
                        scatter_plt = (scatter_plt[0], self.EPOCH)  # ('method', plt_interval)
                    else:
                        break
                    self.eval_model(dataset,           # Plot last epoch
                                    plt_imgs=plt_imgs,         
                                    scatter_plt=scatter_plt,   
                                    pltshow=pltshow, output_dir=self.OUTPUT_DIR)
                    break
                
        train_feat = torch.cat(train_feat, dim=0)
        train_labels = torch.cat(train_labels, dim=0)
        train_imgs = torch.cat(train_imgs, dim=0)

        # =================== SAVE MODEL AND DATA ==================== #
        self.tb.add_embedding(train_feat, metadata=train_labels, label_img=train_imgs, global_step=n_iter)
        if plt_imgs!=None:
            self.tb.add_images('decoded_row_{}_epochs_{}'.format(row, plt_imgs[1]), decoded_plt, self.EPOCH)   
        if save_model: 
            self.save_model(dataset, self.OUTPUT_DIR)

            
    def eval_model(self, dataset, plt_imgs=None, scatter_plt=None, pltshow=False, output_dir=''):
            test_loader = DataLoader(dataset=dataset.test, batch_size=self.BATCH_SIZE, shuffle=True, num_workers=4)
            self.eval()  
            test_loss = 0   
            test_feat = []
            test_labels = []
            test_imgs = []
            with torch.no_grad():      # turn autograd off for memory efficiency
                for batch_idx, (batch_test, batch_test_label) in enumerate(test_loader):
                    n_iter = (self.EPOCH * len(test_loader)) + batch_idx
                    batch_test = batch_test.to(self.device)
                    batch_test = batch_test.view(batch_test.size(0), -1)  # Flatten
                    encoded, decoded = self.forward(batch_test)
                
                    loss = self.loss_fn(decoded, batch_test) 
                    test_loss += loss.item()*batch_test.size(0)

                    test_feat.append(encoded.data.cpu().view(batch_test.size(0), -1)) # Flatten
                    test_labels.append(batch_test_label)
                    test_imgs.append(batch_test.cpu())

                test_loss /= len(test_loader.dataset)
                print('====> Test set loss: {:.4f}\n'.format(test_loss))

                if self.job != None:
                    self.job.meta['test_loss'] = '{:.4f}'.format(test_loss)
                    self.job.save_meta()

                self.tb.add_scalar('Test Loss', test_loss, self.EPOCH)
                
                test_feat = torch.cat(test_feat, dim=0)
                test_labels = torch.cat(test_labels, dim=0)
                test_imgs = torch.cat(test_imgs, dim=0)

            # =================== PLOT COMPARISON ===================== #
            if plt_imgs!=None and self.EPOCH % plt_imgs[1] == 0:         # (N_TEST_IMGS, plt_interval)
                batch_test = batch_test.view(-1, 1, 28, 28)              # (N_TEST_IMG, 1, 28, 28)
                decoded = decoded.view(-1, 1, 28, 28)
                comparison = torch.cat([batch_test[:plt_imgs[0]], decoded[:plt_imgs[0]]])
                output_dir = self._mkdirs(output_dir)
                filename = 'x_recon_{}_{}.png'.format(MODEL, self.EPOCH)
                print(filename)
                print('Saving ', filename)
                save_image(comparison.data.cpu(), output_dir+'/'+filename, nrow=plt_imgs[0])

            # =================== PLOT SCATTER ===================== #
            if scatter_plt!=None and self.EPOCH % scatter_plt[1] == 0:       # ('method', plt_interval)
                output_dir = self._mkdirs(output_dir)
                feat = test_feat.numpy()
                labels = test_labels.numpy()

                if feat.shape[1] > 2:            # Reduce to 2 dim
                    if feat.shape[0] > 5000:     # Plot only first 5000 pts   
                        feat = feat[:5000, :]
                        labels = labels[:5000]
                    if scatter_plt[0]=='tsne':
                        tsne = TSNE(perplexity=30, n_components=2, init='pca', n_iter=1000, random_state=SEED)
                        feat = tsne.fit_transform(feat)
                    elif scatter_plt[0]=='umap':
                        MIN_CLUSTER_SIZE = 10
                        if len(feat) > 3000:
                            MIN_CLUSTER_SIZE = 15
                        umap = UMAP(n_components=2, n_neighbors = MIN_CLUSTER_SIZE, min_dist=0.1,
                                        random_state=SEED, transform_seed=SEED)
                        feat = umap.fit_transform(feat) 
                                            
                feat = MinMaxScaler().fit_transform(feat) 
                plt_name = '{}_{}.png'.format(scatter_plt[0], self.EPOCH)
                img_plt = plt_scatter(feat=feat, labels=labels, output_dir=output_dir, 
                                                plt_name=plt_name, pltshow=pltshow)
                self.tb.add_image(plt_name, img_plt, self.EPOCH, dataformats='HWC')
       
            return test_loss, test_feat, test_labels, test_imgs
    
                
    def save_model(self, dataset, output_dir):
        output_dir = self._mkdirs(output_dir)  
        dataset.save_dataset(output_dir)
        
        model_name = '{}.pth'.format(os.path.basename(os.path.normpath(output_dir)))
        save_path = os.path.join(output_dir, model_name)
        torch.save({        # Saving checkpt for inference and/or resuming training
            'model_name': model_name,
            'model_type': MODEL,
            'model_state_dict': self.state_dict(),
            'device': self.device,
            'lr': self.LR,
            'batch_size': self.BATCH_SIZE,
            'optimizer': self.optimizer,
            'optimizer_state_dict': self.optimizer.state_dict(),
            'epoch': self.EPOCH,
            'loss': self.loss,
            'loss_fn': self.loss_fn,
            'tb_log_dir': self.tb.log_dir
            },
            save_path
        )
        config = {          # Save config file
            'model_name': model_name,
            'model_type': MODEL,
            'device': 'cuda' if torch.cuda.is_available() else 'cpu',
            'lr': self.LR,
            'batch_size': self.BATCH_SIZE,
            'optimizer': self.optimizer.__class__.__name__,
            'epoch': self.EPOCH,
            'loss': self.loss.data.item(),
            'loss_fn': self.loss_fn.__class__.__name__,
            'tb_log_dir': self.tb.log_dir
            }
 
        with open(output_dir+'/config.json', 'w') as f:
            json.dump(config, f)

        print('\nAE Model saved to {}\n'.format(save_path))



    def load_model(self, output_dir):
        model_name = '{}.pth'.format(os.path.basename(os.path.normpath(output_dir)))
        model_path = os.path.join(output_dir, model_name)
        model_checkpt = torch.load(model_path, map_location=lambda storage, loc: storage)
        self.model_name = model_checkpt['model_name'],
        self.LR = model_checkpt['lr'],
        self.BATCH_SIZE = model_checkpt['batch_size'],
        self.load_state_dict(model_checkpt['model_state_dict'])
        self.optimizer = model_checkpt['optimizer']
        self.optimizer.load_state_dict(model_checkpt['optimizer_state_dict'])
        self.EPOCH = model_checkpt['epoch']
        self.loss = model_checkpt['loss']
        self.loss_fn = model_checkpt['loss_fn']
        tb_log_dir =model_checkpt['tb_log_dir']
        
        # Converting from tuples
        self.model_name = str(''.join(self.model_name))
        self.LR = float(self.LR[0])
        self.BATCH_SIZE = int(self.BATCH_SIZE[0])
        self.loss = float(self.loss)
        self.tb = SummaryWriter(log_dir=str(''.join(tb_log_dir)))
   
        print('Loading model ...\n{}\n'.format(self))
        print('Loaded model\t{}\n'.format(self.model_name))
        print('Batch size: {} LR: {} Optimiser: {}\n'
               .format(self.BATCH_SIZE, self.LR, self.optimizer.__class__.__name__))
        print('Epoch: {}\tLoss: {}\n'      
                .format(self.EPOCH, self.loss))
    

    def _mkdirs(self, dir_name):
        if not os.path.exists(dir_name):
            os.makedirs(dir_name)
        return dir_name

    def _init_weights(self, layer):
        if type(layer) == nn.Linear:
            # aka Glorot initialisation (weight, gain('relu'))
            nn.init.xavier_uniform_(layer.weight, nn.init.calculate_gain('relu'))
            layer.bias.data.fill_(0.01)
