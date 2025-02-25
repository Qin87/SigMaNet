# external files
import numpy as np
import pickle as pk

# import scipy as sp
import scipy.sparse as sp
import torch.optim as optim
from datetime import datetime
import os, time, argparse
import torch.nn.functional as F
import random

from scipy.sparse import coo_matrix
from torch_geometric.utils import remove_self_loops
from torch_geometric_signed_directed.data import load_directed_real_data
from torch_geometric_signed_directed import node_class_split
import networkx as nx
import pickle as pk
from torch_geometric.utils.num_nodes import maybe_num_nodes


# internal files
from layer.cheb import *
# from utils.Citation import *
from layer.quaternion_baseline import QGNN_node
from utils.edge_data import to_undirected
from utils.save_settings import write_log
from utils.edge_data import in_out_degree


# select cuda device if available
cuda_device = 0
device = torch.device("cuda:%d" % cuda_device if torch.cuda.is_available() else "cpu")

def parse_args():
    parser = argparse.ArgumentParser(description="baseline--QGNN")

    parser.add_argument('--log_root', type=str, default='../logs/', help='the path saving model.t7 and the training process')
    parser.add_argument('--log_path', type=str, default='test', help='the path saving model.t7 and the training process, the name of folder will be log/(current time)')
    parser.add_argument('--data_path', type=str, default='../dataset/data/tmp/', help='data set folder, for default format see dataset/cora/cora.edges and cora.node_labels')
    parser.add_argument('--dataset', type=str, default='WebKB/Cornell', help='data set selection')

    parser.add_argument('--epochs', type=int, default=1500, help='training epochs')
    parser.add_argument('--num_filter', type=int, default=2, help='num of filters')
    parser.add_argument('--p_q', type=float, default=0.95, help='direction strength, from 0.5 to 1.')
    parser.add_argument('--p_inter', type=float, default=0.1, help='inter_cluster edge probabilities.')
    parser.add_argument('--method_name', type=str, default='QGNN', help='method name')
    parser.add_argument('--seed', type=int, default=0, help='random seed for training testing split/random graph generation')

    parser.add_argument('--dropout', type=float, default=0.0, help='dropout prob')

    parser.add_argument('--debug', '-D', action='store_true', help='debug mode')
    parser.add_argument('--new_setting', '-NS', action='store_true', help='whether not to load best settings')

    parser.add_argument('--layer', type=int, default=2, help='number of layers (2 or 3), default: 2')
    parser.add_argument('--lr', type=float, default=5e-3, help='learning rate')
    parser.add_argument('--l2', type=float, default=5e-4, help='l2 regularizer')

    parser.add_argument('--randomseed', type=int, default=0, help='if set random seed in training')
    return parser.parse_args()


def acc(pred, label, mask):
    correct = int(pred[mask].eq(label[mask]).sum().item())
    acc = correct / int(mask.sum())
    return acc

"""Convert a scipy sparse matrix to a torch sparse tensor."""
def sparse_mx_to_torch_sparse_tensor(sparse_mx):
    sparse_mx = sparse_mx.tocoo().astype(np.float32)
    indices = torch.from_numpy(
        np.vstack((sparse_mx.row, sparse_mx.col)).astype(np.int64))
    values = torch.from_numpy(sparse_mx.data)
    shape = torch.Size(sparse_mx.shape)
    return torch.sparse.FloatTensor(indices, values, shape).to(device)

""" quaternion preprocess for feature vectors """
def quaternion_preprocess_features(features):
    """Row-normalize feature matrix"""
    #rowsum = np.array(features.sum(1))
    #r_inv = np.power(rowsum, -1).flatten()
    #r_inv[np.isinf(r_inv)] = 0.
    #r_mat_inv = sp.diags(r_inv)
    #features = r_mat_inv.dot(features)
    #features = features.todense()
    features = np.tile(features, 4) # A + Ai + Aj + Ak
    return torch.from_numpy(features).to(device)

def normalize_adj(edge_index, edge_weight, x_real):
    """Symmetrically normalize adjacency matrix."""
    edge_index, edge_weight = remove_self_loops(edge_index, edge_weight)
    num_nodes = maybe_num_nodes(edge_index, x_real.size(-2))
    row, col = edge_index.cpu()
    size = num_nodes

    # adj = sp.coo_matrix((edge_weight.cpu(), (row, col)), shape=(size, size), dtype=np.float32) + sp.eye(size)
    adj = coo_matrix((edge_weight.cpu(), (row, col)), shape=(size, size), dtype=np.float32) + np.eye(size)       # Qin
    rowsum = np.array(adj.sum(1))
    d_inv_sqrt = np.power(rowsum, -0.5).flatten()
    d_inv_sqrt[np.isinf(d_inv_sqrt)] = 0.
    d_mat_inv_sqrt = sp.diags(d_inv_sqrt)
    return adj.dot(d_mat_inv_sqrt).transpose().dot(d_mat_inv_sqrt).tocoo()


def main(args):

    random.seed(args.randomseed)
    torch.manual_seed(args.randomseed)
    np.random.seed(args.randomseed)

    date_time = datetime.now().strftime('%m-%d-%H:%M:%S')
    log_path = os.path.join(args.log_root, args.log_path, args.save_name, date_time)

    dataset_name = args.dataset.split('/')
    if len(dataset_name) == 1:
        data = load_directed_real_data(dataset=dataset_name[0], name=dataset_name[0])
    else:
        data = load_directed_real_data(dataset=dataset_name[0], name=dataset_name[1])
    # if len(dataset_name) == 1:
    #     try:
    #         data = pk.load(open(f'./data/fake/{args.dataset}.pk','rb'))
    #     except:
    #         data = pk.load(open(f'./data/fake_for_quaternion_new/{args.dataset}.pk','rb'))
    #     data = node_class_split(data, train_size_per_class=0.6, val_size_per_class=0.2)
    # else:
    #     load_func, subset = args.dataset.split('/')[0], args.dataset.split('/')[1]
    #  #save_name = args.method_name + '_' + 'Layer' + str(args.layer) + '_' + 'lr' + str(args.lr) + 'num_filters' + str(int(args.num_filter))+ '_' + 'task' + str((args.task))
    #     data = load_directed_real_data(dataset=dataset_name[0], name=dataset_name[1])#.to(device)

    if os.path.isdir(log_path) == False:
        os.makedirs(log_path)


    if not data.__contains__('edge_weight'):
        data.edge_weight = None
    data.y = data.y.long()
    num_classes = (data.y.max() - data.y.min() + 1).detach().numpy()
    if data.edge_weight is not None:
        data.edge_weight = torch.FloatTensor(data.edge_weight)#.to(device)
    data.edge_index, data.edge_weight = to_undirected(data.edge_index, data.edge_weight)
    size = data.y.size(-1)
    if data.x is None:
        data.x = in_out_degree(data.edge_index, size, data.edge_weight)

    #data = data.to(device)
    # normalize label, the minimum should be 0 as class index
    splits = data.train_mask.shape[1]

    features = quaternion_preprocess_features(data.x).to(device)
    adj = sparse_mx_to_torch_sparse_tensor(normalize_adj(data.edge_index, data.edge_weight, data.x).tocoo()).to(device)
    # adj = normalize_adj(data.edge_index, data.edge_weight, data.x)     # Qin
    # # adj = sparse_mx_to_torch_sparse_tensor(adj).tocoo().to(device)
    # adj_sparse = sp.csr_matrix(adj)  # Convert adj to a sparse CSR matrix
    # adj = adj_sparse.tocoo()  # Convert to COO format

    data = data.to(device)

    if len(data.test_mask.shape) == 1:
        data.test_mask = data.test_mask.unsqueeze(1).repeat(1, splits)

    results = np.zeros((splits, 4))
    for split in range(splits):
        log_str_full = ''
        graphmodel = QGNN_node(nfeat=data.x.size(-1)*4, nhid=args.num_filter, nclass=num_classes, dropout=args.dropout).to(device)
        model = graphmodel # nn.DataParallel(graphmodel)
        opt = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.l2)

        #################################
        # Train/Validation/Test
        #################################
        best_test_err = 1000.0
        early_stopping = 0
        for epoch in range(args.epochs):
            start_time = time.time()
            ####################
            # Train
            ####################
            train_loss, train_acc = 0.0, 0.0

            # for loop for batch loading
            model.train()
            out = model(features, adj)

            train_loss = F.nll_loss(out[data.train_mask[:,split]], data.y[data.train_mask[:,split]])
            pred_label = out.max(dim = 1)[1]

            train_acc = acc(pred_label, data.y, data.train_mask[:,split])

            opt.zero_grad()
            train_loss.backward()
            opt.step()

            outstrtrain = 'Train loss:, %.6f, acc:, %.3f,' % (train_loss.detach().item(), train_acc)
            #scheduler.step()

            ####################
            # Validation
            ####################
            model.eval()
            test_loss, test_acc = 0.0, 0.0

            out = model(features, adj)
            pred_label = out.max(dim = 1)[1]

            test_loss = F.nll_loss(out[data.val_mask[:,split]], data.y[data.val_mask[:,split]])
            test_acc = acc(pred_label, data.y, data.val_mask[:,split])

            outstrval = ' Test loss:, %.6f, acc: ,%.3f,' % (test_loss.detach().item(), test_acc)

            duration = "---, %.4f, seconds ---" % (time.time() - start_time)
            log_str = ("%d, / ,%d, epoch," % (epoch, args.epochs))+outstrtrain+outstrval+duration
            log_str_full += log_str + '\n'
            #print(log_str)


            ####################
            # Save weights
            ####################
            save_perform = test_loss.detach().item()
            if save_perform <= best_test_err:
                early_stopping = 0
                best_test_err = save_perform
                torch.save(model.state_dict(), log_path + '/model'+str(split)+'.t7')
            else:
                early_stopping += 1
            if early_stopping > 500 or epoch == (args.epochs-1):
                torch.save(model.state_dict(), log_path + '/model_latest'+str(split)+'.t7')
                break

        write_log(vars(args), log_path)

        ####################
        # Testing
        ####################
        model.load_state_dict(torch.load(log_path + '/model'+str(split)+'.t7'))
        model.eval()
        preds = model(features, adj)
        pred_label = preds.max(dim = 1)[1]
        np.save(log_path + '/pred' + str(split), pred_label.to('cpu'))

        acc_train = acc(pred_label, data.y, data.val_mask[:,split])
        acc_test = acc(pred_label, data.y, data.test_mask[:,split])

        model.load_state_dict(torch.load(log_path + '/model_latest'+str(split)+'.t7'))
        model.eval()
        preds = model(features, adj)
        pred_label = preds.max(dim = 1)[1]
        np.save(log_path + '/pred_latest' + str(split), pred_label.to('cpu'))

        acc_train_latest = acc(pred_label, data.y, data.val_mask[:,split])
        acc_test_latest = acc(pred_label, data.y, data.test_mask[:,split])

        ####################
        # Save testing results
        ####################
        logstr = 'val_acc: '+str(np.round(acc_train, 3))+' test_acc: '+str(np.round(acc_test,3))+' val_acc_latest: '+str(np.round(acc_train_latest,3))+' test_acc_latest: '+str(np.round(acc_test_latest,3))
        print(logstr)
        results[split] = [acc_train, acc_test, acc_train_latest, acc_test_latest]
        log_str_full += logstr
        with open(log_path + '/log'+str(split)+'.csv', 'w') as file:
            file.write(log_str_full)
            file.write('\n')
        torch.cuda.empty_cache()
    return results


if __name__ == "__main__":
    args = parse_args()
    if args.debug:
        args.epochs = 1
    if args.dataset[:3] == 'syn':
        if args.dataset[4:7] == 'syn':
            if args.p_q not in [-0.08, -0.05]:
                args.dataset = 'syn/syn'+str(int(100*args.p_q))+'Seed'+str(args.seed)
            elif args.p_q == -0.08:
                args.p_inter = -args.p_q
                args.dataset = 'syn/syn2Seed'+str(args.seed)
            elif args.p_q == -0.05:
                args.p_inter = -args.p_q
                args.dataset = 'syn/syn3Seed'+str(args.seed)
        elif args.dataset[4:10] == 'cyclic':
            args.dataset = 'syn/cyclic'+str(int(100*args.p_q))+'Seed'+str(args.seed)
        else:
            args.dataset = 'syn/fill'+str(int(100*args.p_q))+'Seed'+str(args.seed)
    dir_name = os.path.join(os.path.dirname(os.path.realpath(
            __file__)), '../result_arrays',args.log_path,args.dataset+'/')
    args.log_path = os.path.join(args.log_path,args.method_name, args.dataset)
    if not args.new_setting:
        if args.dataset[:3] == 'syn':
            if args.dataset[4:7] == 'syn':
                setting_dict = pk.load(open('./syn_settings.pk','rb'))
                dataset_name_dict = {
                    0.95:1, 0.9:4,0.85:5,0.8:6,0.75:7,0.7:8,0.65:9,0.6:10
                }
                if args.p_inter == 0.1:
                    dataset = 'syn/syn' + str(dataset_name_dict[args.p_q])
                elif args.p_inter == 0.08:
                    dataset = 'syn/syn2'
                elif args.p_inter == 0.05:
                    dataset = 'syn/syn3'
                else:
                    raise ValueError('Please input the correct p_q and p_inter values!')
            elif args.dataset[4:10] == 'cyclic':
                setting_dict = pk.load(open('./Cyclic_setting_dict.pk','rb'))
                dataset_name_dict = {
                    0.95:0, 0.9:1,0.85:2,0.8:3,0.75:4,0.7:5,0.65:6
                }
                dataset = 'syn/syn_tri_' + str(dataset_name_dict[args.p_q])
            else:
                setting_dict = pk.load(open('./Cyclic_fill_setting_dict.pk','rb'))
                dataset_name_dict = {
                    0.95:0, 0.9:1,0.85:2,0.8:3
                }
                dataset = 'syn/syn_tri_' + str(dataset_name_dict[args.p_q]) + '_fill'
            setting_dict_curr = setting_dict[dataset][args.method_name].split(',')
            args.to_undirected = (setting_dict_curr[setting_dict_curr.index('to_undirected')+1]=='True')
            try:
                args.num_filter = int(setting_dict_curr[setting_dict_curr.index('num_filter')+1])
            except ValueError:
                pass
            try:
                args.layer = int(setting_dict_curr[setting_dict_curr.index('layer')+1])
            except ValueError:
                pass
            args.lr = float(setting_dict_curr[setting_dict_curr.index('lr')+1])
    if os.path.isdir(dir_name) == False:
        try:
            os.makedirs(dir_name)
        except FileExistsError:
            print('Folder exists!')
    save_name = args.method_name + 'lr' + str(int(args.lr*1000)) + 'num_filters' + str(int(args.num_filter)) + 'layer' + str(int(args.layer))
    args.save_name = save_name
    results = main(args)
    np.save(dir_name+save_name, results)
