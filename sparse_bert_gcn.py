import argparse
import os, sys, time
import pickle as pkl
from sklearn.cluster import SpectralClustering, MiniBatchKMeans
import scipy.sparse as smat
from xbert.rf_util import smat_util
import numpy as np
from tqdm import tqdm, trange
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import Parameter
from pytorch_pretrained_bert import BertTokenizer, BertModel
from pytorch_pretrained_bert.modeling import BertPreTrainedModel, BertConfig, WEIGHTS_NAME, CONFIG_NAME
from pytorch_pretrained_bert.optimization import BertAdam, WarmupLinearSchedule
from gensim.models import KeyedVectors as KV
from sklearn.neighbors import NearestNeighbors as NN
import random
# from mather.bert import *
import xbert.data_utils as data_utils
import xbert.rf_linear as rf_linear
import xbert.rf_util as rf_util
from Hyperparameters import Hyperparameters
from GCN import GraphConvolution, gen_A, gen_adj

class GraphUtil():
    def __init__(self, Y, num_labels):
        self.Y = Y
        self.nums = np.zeros(num_labels)
        self.adj = np.zeros([num_labels, num_labels])

    def gen_graph(self):
        for label_list in tqdm(self.Y):
            for i in range(len(label_list)):
                for j in range(i+1, len(label_list)):
                    self.adj[label_list[i]][label_list[j]] = 1
                    self.adj[label_list[j]][label_list[i]] = 1

        return self.adj

    def cal_degree(self):
        self.nums = np.sum(self.adj, axis=1)

class LabelCluster():
    def __init__(self, adj, k):
        self.adj = adj
        self.k = k
        self.C = np.zeros(([self.adj.shape[0], k]))
        self.c_adj = np.zeros(([k,k]))

    def spec_clustering(self, Y, clus_path):
        clus_path += 'spec'
        if os.path.isfile(clus_path+'-C'):
            with open(clus_path+'-C', 'rb') as g:
                self.C = np.load(g)
            with open(clus_path+'-c_adj', 'rb') as g:
                self.c_adj = np.load(g)
            return self.C, self.c_adj
        else:
            clustering = SpectralClustering(n_clusters = self.k,
                assign_labels="discretize", affinity='precomputed', random_state=0).fit(self.adj)
            clusters = clustering.labels_
            return self.get_cluster_graph(Y, clusters, clus_path)

    def kmeans_clustering(self, Y, label_space, clus_path):
        clus_path += 'kmeans'
        if os.path.isfile(clus_path+'-C'):
            with open(clus_path+'-C', 'rb') as g:
                self.C = np.load(g)
            with open(clus_path+'-c_adj', 'rb') as g:
                self.c_adj = np.load(g)
            return self.C, self.c_adj
        else:
            clustering = MiniBatchKMeans(n_clusters = self.k, random_state=0).fit(label_space)
            clusters = clustering.labels_
            return self.get_cluster_graph(Y, clusters, clus_path)

    def get_cluster_graph(self, Y, clusters, clus_path):
            for idx, c in enumerate(clusters):
                self.C[idx, c] = 1
            for label_list in tqdm(Y):
                for i in range(len(label_list)):
                    for j in range(i+1, len(label_list)):
                        self.c_adj[np.argmax(self.C[label_list[i]]), np.argmax(self.C[label_list[j]])] = 1
                        self.c_adj[np.argmax(self.C[label_list[j]]), np.argmax(self.C[label_list[i]])] = 1

            self.c_adj[np.where(np.eye(self.k))] = 1
            with open(clus_path+'-C', 'wb') as g:
                np.save(g, self.C)
            with open(clus_path+'-c_adj', 'wb') as g:
                np.save(g, self.c_adj)

            return self.C, self.c_adj

def get_tensor(M, dv):
    return torch.tensor(M).float().to(dv)

def csr_to_sparse_tensor(X):
    coo = X.tocoo()
    i = np.mat([coo.row, coo.col]).transpose()
    i = torch.LongTensor(i)
    v = torch.FloatTensor(res.data)
    return torch.SparseTensor(i.t(), v, torch.Size(coo.shape))

class BertGCN(BertModel):
    def __init__(self, config, ft, num_labels, res, H, device_num):
        super(BertGCN, self).__init__(config)
        self.device = torch.device('cuda:' + device_num)
        self.bert = BertModel.from_pretrained('bert-base-uncased')
        self.dropout = nn.Dropout(0.5)
        self.H = get_tensor(H, self.device)
        self.ft = ft
        self.num_labels = num_labels
        self.FCN = nn.Linear(768, num_labels)
        self.softmax = nn.Softmax(dim=1)
        self.apply(self.init_bert_weights)
        self.gcn_weight1 = Parameter(torch.Tensor(H.shape[1], 1500))
        self.gcn_weight2 = Parameter(torch.Tensor(1500, 768))
        self.lkrelu = nn.LeakyReLU(0.2)
        self.A = torch.tensor(gen_A(num_labels, res)).float().to(self.device)
        # self.A = Parameter(torch.from_numpy(gen_A(num_labels, res)).float()).to(self.device)
        self.adj = gen_adj(self.A).detach()
        # self.FCN2 = nn.Linear(num_labels, num_labels)

    def forward(self, input_ids, gcn_limit=False, token_type_ids=None, attention_mask=None):
        if self.ft:
            with torch.no_grad():
                _, pooled_output = self.bert(input_ids, output_all_encoded_layers=False)
        else:
            _, pooled_output = self.bert(input_ids, output_all_encoded_layers=False)

        bert_logits = self.dropout(pooled_output)
        skip = self.lkrelu(self.FCN(bert_logits))
        adj = self.adj
        # adj = gen_adj(self.A).detach()
        x = torch.matmul(adj, self.dropout(torch.matmul(self.H, self.gcn_weight1)))
        x = self.lkrelu(x)
        x = torch.matmul(adj, torch.matmul(x, self.gcn_weight2))
        x = self.lkrelu(x)
        x = x.transpose(1, 0)
        x = torch.matmul(bert_logits, x)
        logits = x + skip
        # logits = x
        # logits = self.FCN2(logits)
        return self.softmax(logits)
        # return logits

def get_binary_vec(label_list, output_dim):
    res = np.zeros([len(label_list), output_dim])
    for i, label in enumerate(label_list):
        res[i][label]=1
    return smat.lil_matrix(res)

def get_score(logits, truth, k=5):
    num_corrects = np.zeros(k)
    preds = np.argsort(-logits.cpu().detach().numpy())[:k]
    truth = np.where(truth)[0]

    precision=np.zeros(k)
    recall=np.zeros(k)
    for i, pred in enumerate(preds):
        if pred in truth:
            num_corrects[i:] += 1

    for i in range(k):
        precision[i] = num_corrects[i]/(i+1)
        recall[i] = num_corrects[i]/len(truth)
    return precision, recall


class BertGCN_ClusterClassifier():
    def __init__(self, hypes, device_num, ft, epochs, label_space, C, c_adj, alpha, max_seq_len=512):
        self.max_seq_len = max_seq_len
        self.ds_path = '../datasets/' + hypes.dataset
        self.hypes = hypes
        self.epochs = epochs
        self.num_clusters = c_adj.shape[0]
        self.H = label_space.todense()
        self.tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
        # self.criterion =  nn.MultiLabelSoftMarginLoss()
        self.criterion =  nn.BCELoss()
        self.device = torch.device('cuda:' + device_num)
        self.model = BertGCN_Cluster.from_pretrained('bert-base-uncased', ft, self.H.shape[0], self.H, device_num, C, c_adj, alpha)

    def train(self, X, Y, val_X, val_Y, model_path=None, ft_from=0):
        if model_path: # fine tuning
            self.model.load_state_dict(torch.load(model_path))
            epohcs_range = range(ft_from+1, ft_from+self.epochs+1)
        else:
            epohcs_range = range(1, self.epochs+1)

        all_input_ids = torch.tensor(X)
        # all_Ys = get_binary_vec(Y, self.H.shape[0])
        # all_Ys = torch.tensor(all_Ys)
        bs = 12
        self.model.train()
        self.model.to(self.device)
        total_run_time = 0.0

        param_optimizer = list(self.model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
            ]

        optimizer = BertAdam(optimizer_grouped_parameters,
                             lr=self.hypes.learning_rate,
                             warmup=self.hypes.warmup_rate)

        nb_tr_examples = 0
        for epoch in tqdm(epohcs_range):
            eval_t = 0
            tr_loss = 0
            nb_tr_steps = 0
            num_corrects = 0
            total = 0
            start_time = time.time()
            precisions=np.zeros(5)
            recalls=np.zeros(5)
            for step in range(int(len(X)/bs)-1):
                # if random.randint(1, 5) != 2:
                #     continue
                input_ids = all_input_ids[step*bs:(step+1)*bs].to(self.device)
                labels = get_binary_vec(Y[step*bs:(step+1)*bs], self.H.shape[0])
                labels = get_tensor(labels.toarray(), self.device)
                c_pred = self.model(input_ids)
                loss = self.criterion(c_pred, labels)

                tr_loss += loss.item()
                nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                if step % self.hypes.log_interval == 0:
                    for i in range(len(c_pred)):
                        eval_t += 1
                        truth = labels.cpu().detach().numpy().astype(int)[i]
                        logit = c_pred[i]
                        precision, recall = get_score(logit, truth)
                        precisions += precision
                        recalls += recall
                    elapsed = time.time() - start_time
                    start_time = time.time()
                    cur_loss = tr_loss / nb_tr_steps
                    print("| {:4d}/{:4d} batches | ms/batch {:5.2f}".format(step, int(len(X)/bs), elapsed * 1000))
                    print('Precision:', np.round(precisions/eval_t, 4))
                    print('Recall:', np.round(recalls/eval_t, 4))

            # if epoch % 20 == 0:
            output_dir = '/mnt/sdb/yss/xbert_save_models/gcn_classifier/'+self.hypes.dataset+'/ep-'+str(epoch)+'/k-'+str(self.num_clusters)+'/'
            # output_dir = '../save_models/gcn_classifier/'+self.hypes.dataset+'/ep-'+str(epoch)+'/k-'+str(self.num_clusters)+'/'
            self.save(output_dir)

            val_inputs = np.array(val_X)
            val_labels = np.array(val_Y)
            acc = self.evaluate(val_inputs, val_labels)
            self.model.train()


    def evaluate(self, X, Y, model_path=''):
        if model_path:
            self.model.load_state_dict(torch.load(model_path))
        all_input_ids = torch.tensor(X)
        bs = self.hypes.eval_batch_size
        self.model.eval()
        self.model.to(self.device)

        num_corrects = 0
        start_time = time.time()
        precisions = np.zeros(5)
        recalls = np.zeros(5)
        eval_t=0
        print('Inferencing...')
        for step in trange(int(len(X)/bs)-1):
            input_ids = all_input_ids[step*bs:(step+1)*bs].to(self.device)
            labels = get_binary_vec(Y[step*bs:(step+1)*bs], self.H.shape[0])
            labels = get_tensor(labels.toarray(), self.device)
            with torch.no_grad():
                c_pred = self.model(input_ids)

            for i in range(len(c_pred)):
                eval_t += 1
                truth = labels.cpu().detach().numpy().astype(int)[i]
                logit = c_pred[i]
                precision, recall = get_score(logit, truth)
                precisions += precision
                recalls += recall

        print('Test Precision:', np.round(precisions/eval_t, 4))
        print('Test Recall:', np.round(recalls/eval_t, 4))

    def get_bert_token(self, trn_text, only_CLS=False):
        X = []
        # self.model.cuda(1)
        print('========================================================')
        print('getting sentence embedding...')
        for text in tqdm(trn_text):
            marked_text = "[CLS] " + text + " [SEP]"
            tokenized_text = self.tokenizer.tokenize(marked_text)
            if len(tokenized_text) > self.max_seq_len:
                tokenized_text = tokenized_text[:self.max_seq_len-1] + ['[SEP]']

            indexed_tokens = self.tokenizer.convert_tokens_to_ids(tokenized_text)
            if len(indexed_tokens) < self.max_seq_len:
                padding = [0] * (self.max_seq_len - len(indexed_tokens))
                indexed_tokens += padding
            else:
                indexed_tokens = indexed_tokens[:self.max_seq_len]

            X.append(indexed_tokens)
        print('========================================================')
        return X

    def save(self, output_dir):
        model_to_save = self.model.module if hasattr(self.model, 'module') else self.model
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        output_model_file = os.path.join(output_dir, WEIGHTS_NAME)
        output_config_file = os.path.join(output_dir, CONFIG_NAME)
        torch.save(model_to_save.state_dict(), output_model_file)
        model_to_save.config.to_json_file(output_config_file)

def load_data(X_path, X, bert):
    if os.path.isfile(X_path):
        with open(X_path, 'rb') as g:
            X = pkl.load(g)
    else:
        X = bert.get_bert_token(X)
        with open(X_path, 'wb') as g:
            pkl.dump(X, g)

    return X

def main():
    parser = argparse.ArgumentParser(description='')
    parser.add_argument("-ds", "--dataset", default="AmazonCat-13K", type=str, required=True)
    # parser.add_argument("-t", "--head_threshold", default=10000, type=int)
    parser.add_argument("-k", "--k", default=256, type=int)
    parser.add_argument("-gpu", "--device_num", default='0', type=str)
    parser.add_argument("-train", "--is_train", default=1, type=int)
    parser.add_argument("-ep", "--epochs", default=8, type=int)
    parser.add_argument("-ft", "--fine_tune", default=0, type=int)
    parser.add_argument("-clu", "--clustering", default='spec', type=str)
    parser.add_argument("-from", "--ft_from", default=0, type=int)
    parser.add_argument("-al", "--alpha", default=1.0, type=float)
    args = parser.parse_args()

    ft = (args.fine_tune == 1)
    ft_from = args.ft_from
    num_clusters = args.k
    alpha = args.alpha
    hypes = Hyperparameters(args.dataset)
    ds_path = '../datasets/' + args.dataset
    device_num = args.device_num

    with open(ds_path+'/mlc2seq/train_clus_X', 'rb') as g:
        trn_clus_X = pkl.load(g)
    with open(ds_path+'/mlc2seq/train_clus_Y', 'rb') as g:
        trn_clus_Y = pkl.load(g)
    with open(ds_path+'/mlc2seq/test_clus_X', 'rb') as g:
        test_clus_X = pkl.load(g)
    with open(ds_path+'/mlc2seq/test_clus_Y', 'rb') as g:
        test_clus_Y = pkl.load(g)
    answer = smat.load_npz(ds_path+'/Y.tst.npz')
    label_space = smat.load_npz(ds_path+'/L.elmo.npz')
    label_space = smat.lil_matrix(label_space)
    output_dim = label_space.shape[0]
    gutil = GraphUtil(trn_clus_Y, output_dim)
    adj = gutil.gen_graph()
    # gutil.cal_degree()
    lc = LabelCluster(adj, num_clusters)
    # c_adj: k*k, C: m*k
    clus_path = ds_path+'/clus_data/k-' + str(num_clusters)
    if args.clustering == 'kmeans':
        C, c_adj = lc.kmeans_clustering(trn_clus_Y, label_space, clus_path)
    elif args.clustering == 'spec':
        C, c_adj = lc.spec_clustering(trn_clus_Y, clus_path)
    else:
        C, c_adj = lc.spec_clustering(trn_clus_Y, clus_path)

    bert = BertGCN_ClusterClassifier(hypes, device_num, ft, args.epochs, label_space, C, c_adj, alpha, max_seq_len=256)
    trn_X_path = ds_path+'/clus_data/trn_X'
    test_X_path = ds_path+'/clus_data/test_X'
    trn_X = load_data(trn_X_path, trn_clus_X, bert)
    test_X = load_data(test_X_path, test_clus_X, bert)
    print('Number of labels:', output_dim)
    print('Number of trn instances:', len(trn_X))

    if args.is_train:
        if ft:
            print('======================Start Fine-Tuning======================')
            model_path = '/mnt/sdb/yss/xbert_save_models/gcn_classifier/'+hypes.dataset+'/ep-'+str(ft_from)+'/k-'+str(num_clusters)+'/pytorch_model.bin'
            # model_path = '../save_models/gcn_clus_classifier/'+hypes.dataset+'/ep-' + str(ft_from) + '/k-' +  str(num_clusters) + '/pytorch_model.bin'
            bert.train(trn_X, trn_clus_Y, test_X, test_clus_Y, model_path, ft_from)
        else:
            print('======================Start Training======================')
            bert.train(trn_X, trn_clus_Y, test_X, test_clus_Y)
        bert.evaluate(test_X, test_clus_Y)
        output_dir = '../save_models/gcn_clus_classifier/'+hypes.dataset+'/ep-' + str(args.epochs + ft_from) + '/k-' + str(num_clusters) + '/'
        bert.save(output_dir)

    else:
        model_path = '../save_models/gcn_clus_classifier/'+args.dataset+'/ep-'+str(ft_from)+ '/k-' +str(num_clusters) + '/pytorch_model.bin'
        print('======================Start Testing======================')
        bert.evaluate(test_X, test_clus_Y, model_path)



if __name__ == '__main__':
    main()

#
