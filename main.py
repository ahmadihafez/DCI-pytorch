import argparse
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
from sklearn import metrics
import random

from util import load_data_cluster
from models.model_test import ModelTest
from models.dci import DCI
from sklearn.cluster import KMeans

sig = torch.nn.Sigmoid()

def setup_seed(seed):
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True  

def preprocess_neighbors_sumavepool(edge_index, nb_nodes, device):
    Adj_idx = edge_index
        
    Adj_idx_2 = torch.cat([torch.unsqueeze(Adj_idx[1], 0), torch.unsqueeze(Adj_idx[0], 0)], 0)
    Adj_idx = torch.cat([Adj_idx, Adj_idx_2], 1)

    self_loop_edge = torch.LongTensor([range(nb_nodes), range(nb_nodes)])
    Adj_idx = torch.cat([Adj_idx, self_loop_edge], 1)
        
    Adj_elem = torch.ones(Adj_idx.shape[1])

    Adj = torch.sparse.FloatTensor(Adj_idx, Adj_elem, torch.Size([nb_nodes, nb_nodes]))

    return Adj.to(device)

def eval(args, model, test_graph):
    output = model(test_graph[1], test_graph[0])
    pred = sig(output.detach().cpu())
    test_idx = test_graph[3]
    
    labels = test_graph[-1]
    pred = pred[labels[test_idx, 0].astype('int')].numpy().tolist()
    target = labels[test_idx, 1]
    
    false_positive_rate, true_positive_rate, _ = metrics.roc_curve(target, pred, pos_label=1)
    auc = metrics.auc(false_positive_rate, true_positive_rate)

    return auc

def finetune(args, model_pretrain, device, test_graph, feats_num, num_user, num_object):
    # initialize the joint model
    model = ModelTest(args.num_layers, args.num_mlp_layers, feats_num, args.hidden_dim, args.final_dropout, args.neighbor_pooling_type, num_user, num_object, device).to(device)
    
    # replace the encoder in joint model with the pre-trained encoder
    pretrained_dict = model_pretrain.state_dict()
    model_dict = model.state_dict()
    pretrained_dict =  {k: v for k, v in pretrained_dict.items() if k in model_dict}
    model_dict.update(pretrained_dict)
    model.load_state_dict(model_dict)

    optimizer = optim.Adam(model.parameters(), lr=args.lr)
    
    criterion_tune = nn.BCEWithLogitsLoss()

    res = []
    train_idx = test_graph[2]
    node_train = test_graph[-1][train_idx, 0].astype('int')
    label_train = torch.FloatTensor(test_graph[-1][train_idx, 1]).to(device)
    for epoch in range(1, args.finetune_epochs+1):
        model.train()
        output = model(test_graph[1], test_graph[0])
        loss = criterion_tune(output[node_train], torch.reshape(label_train, (-1, 1)))
        
        #backprop
        if optimizer is not None:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        
        # testing
        model.eval()
        auc = eval(args, model, test_graph)
        res.append(auc)
    res = np.array(res)

    return np.max(res)

def main():
    parser = argparse.ArgumentParser(description='PyTorch graph convolutional neurasl net')
    parser.add_argument('--dataset', type=str, default="wiki",
                        help='name of dataset (default: wiki)')
    parser.add_argument('--device', type=int, default=0,
                        help='which gpu to use if any (default: 0)')
    parser.add_argument('--epochs', type=int, default=50,
                        help='number of epochs to train (default: 350)')
    parser.add_argument('--num_layers', type=int, default=3,
                        help='number of layers INCLUDING the input one (default: 5)')
    parser.add_argument('--num_mlp_layers', type=int, default=2,
                        help='number of layers for MLP EXCLUDING the input one (default: 2). 1 means linear model.')
    parser.add_argument('--hidden_dim', type=int, default=128,
                        help='number of hidden units (default: 128)')
    parser.add_argument('--finetune_epochs', type=int, default=100,
                        help='number of finetune epochs (default: 20)')
    parser.add_argument('--num_folds', type=int, default=10,
                        help='number of folds (default: 10)')
    parser.add_argument('--lr', type=float, default=0.01,
                        help='learning rate (default: 0.001)')
    parser.add_argument('--num_cluster', type=int, default=2,
                        help='number of clusters (default: 2)')
    parser.add_argument('--final_dropout', type=float, default=0.5,
                        help='final layer dropout (default: 0.5)')
    parser.add_argument('--neighbor_pooling_type', type=str, default="sum", choices=["sum", "average"],
                        help='Pooling for over neighboring nodes: sum or average')
    args = parser.parse_args()

    setup_seed(0)
    
    device = torch.device("cuda:" + str(args.device)) if torch.cuda.is_available() else torch.device("cpu")

    # Data loading
    edge_index, feats, split_idx, label, num_user, num_object, loc = load_data_cluster(args.dataset, args.num_cluster, args.num_folds)
    input_dim = feats.shape[1]

    # store userID in each clusters
    cluster_info = [list(np.where(loc[:, i]==1)[0]) for i in range(args.num_cluster)]

    # the shuffled features are used to contruct the negative samples
    idx = np.random.permutation(num_user+num_object)
    shuf_feats = feats[idx, :]

    # pre-training process
    model_pretrain = DCI(args.num_layers, args.num_mlp_layers, input_dim, args.hidden_dim, args.final_dropout, args.neighbor_pooling_type, num_user, num_object, device).to(device)
    optimizer_train = optim.Adam(model_pretrain.parameters(), lr=args.lr)

    Adj = preprocess_neighbors_sumavepool(torch.LongTensor(edge_index), num_user + num_object, device)
    feats = torch.FloatTensor(feats).to(device)
    shuf_feats = torch.FloatTensor(shuf_feats).to(device)

    
    for epoch in range(1, args.epochs + 1):
		model_pretrain.train()
        loss_pretrain = model_pretrain(feats, shuf_feats, Adj, None, None, None, cluster_info, args.num_cluster)
        if optimizer_train is not None:
            optimizer_train.zero_grad()
            loss_pretrain.backward()         
            optimizer_train.step()

        # re-clustering
        if epoch % 20 == 0 and epoch < args.epochs:
			model_pretrain.eval()
            emb = model_pretrain.get_emb(feats, Adj)
            kmeans = KMeans(n_clusters=args.num_cluster, random_state=0).fit(emb.detach().cpu().numpy())
            ss_label = kmeans.labels_
            loc = np.zeros((nb_nodes, args.num_cluster))
            for i in range(nb_nodes):
                loc[i, ss_label[i]] = 1
            cluster_info = [list(np.where(loc[:, i]==1)[0]) for i in range(args.num_cluster)]
    
    print('Pre-training Down!')
            
    #fine-tuning process
    fold_idx = 1
    every_fold_auc = []
    for (train_idx, test_idx) in split_idx:
        test_graph = (Adj, feats, train_idx, test_idx, label)
        tmp_auc = finetune(args, model_pretrain, device, test_graph, input_dim, num_user, num_object)
        every_fold_auc.append(tmp_auc)
        fold_idx += 1
        print('AUC on the Fold'+str(fold_idx)+': ', tmp_auc)
    
    print('The averaged AUC score: ', np.mean(every_fold_auc))


if __name__ == '__main__':
    main()