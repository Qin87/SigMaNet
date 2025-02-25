from platform import node
from typing import Optional
import time
import torch
from torch_scatter import scatter_add
from torch_sparse import coalesce
from torch_geometric.utils import add_self_loops, remove_self_loops, to_scipy_sparse_matrix
from torch_geometric.utils.num_nodes import maybe_num_nodes
import numpy as np
from scipy.sparse.linalg import eigsh
from scipy.sparse import coo_matrix
from . import antiparallel as anti
import scipy
from torch_geometric.typing import OptTensor


def get_specific(vector, device):
    vector = vector.tocoo()
    row = torch.from_numpy(vector.row).to(torch.long)
    col = torch.from_numpy(vector.col).to(torch.long)
    edge_index = torch.stack([row, col], dim=0).to(device)
    edge_weight = torch.from_numpy(vector.data).to(device)
    return edge_index, edge_weight

def get_Quaternion_Laplacian(edge_index: torch.LongTensor, edge_weight: Optional[torch.Tensor] = None,
                  normalization: Optional[str] = 'sym',
                  dtype: Optional[int] = None,
                  num_nodes: Optional[int] = None,
                  return_lambda_max: bool = False):
    r""" Computes our Sign Magnetic Laplacian of the graph given by :obj:`edge_index`
    and optional :obj:`edge_weight` from the
    
    Arg types:
        * **edge_index** (PyTorch LongTensor) - The edge indices.
        * **edge_weight** (PyTorch Tensor, optional) - One-dimensional edge weights. (default: :obj:`None`)
        * **normalization** (str, optional) - The normalization scheme for the magnetic Laplacian (default: :obj:`sym`) -
            1. :obj:`None`: No normalization :math:`\mathbf{L} = \mathbf{D} - \mathbf{A} Hadamard \exp(i \Theta^{(q)})`
            
            2. :obj:`"sym"`: Symmetric normalization :math:`\mathbf{L} = \mathbf{I} - \mathbf{D}^{-1/2} \mathbf{A}
            \mathbf{D}^{-1/2} Hadamard \exp(i \Theta^{(q)})`
        
        * **dtype** (torch.dtype, optional) - The desired data type of returned tensor in case :obj:`edge_weight=None`. (default: :obj:`None`)
        * **num_nodes** (int, optional) - The number of nodes, *i.e.* :obj:`max_val + 1` of :attr:`edge_index`. (default: :obj:`None`)
        * **return_lambda_max** (bool, optional) - Whether to return the maximum eigenvalue. (default: :obj:`False`)
    Return types:
        * **edge_index** (PyTorch LongTensor) - The edge indices of the magnetic Laplacian.
        * **edge_weight.real, edge_weight.imag** (PyTorch Tensor) - Real and imaginary parts of the one-dimensional edge weights for the magnetic Laplacian.
        * **lambda_max** (float, optional) - The maximum eigenvalue of the magnetic Laplacian, only returns this when required by setting return_lambda_max as True.
    """

    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    if normalization is not None:
        assert normalization in ['sym'], 'Invalid normalization'

    edge_index, edge_weight = remove_self_loops(edge_index, edge_weight)

    if edge_weight is None:
        edge_weight = torch.ones(edge_index.size(1), dtype=dtype,
                                 device=edge_index.device)

    num_nodes = maybe_num_nodes(edge_index, num_nodes)
    row, col = edge_index.cpu()
    size = num_nodes

    A = coo_matrix((edge_weight.cpu(), (row, col)), shape=(size, size), dtype=np.float32)
    
    diag = coo_matrix( (np.ones(size), (np.arange(size), np.arange(size))), shape=(size, size), dtype=np.float32)


    # Adding the indentity matrix
    A += diag

    # extraction of undirected connection (direcetd with same weights)
    A_undirected = anti.antiparalell(A)

    A_sym = 0.5*(A + A.T) # symmetrized adjacency

    # retrieval undirected connection with same weights
    operation_undirected = diag + A_undirected

    # retrieval directed edges with no antiparallel
    operation = (scipy.sparse.csr_matrix.sign(np.abs(A) - np.abs(A.T)))*1j
    
    # degree 0.5*(|A| + |A.T|)
    #deg = np.array((0.5*(np.abs(A) + np.abs(A.T))).sum(axis=0))[0] 
    deg = np.array(np.abs(A_sym).sum(axis=0))[0] # out degree
    if normalization is None:
        D = coo_matrix((deg, (np.arange(size), np.arange(size))), shape=(size, size), dtype=np.float32)
        L = D - A_sym.multiply(operation) #element-wise
    elif normalization == 'sym':
        deg[deg == 0]= 1
        deg_inv_sqrt = np.power(deg, -0.5)
        deg_inv_sqrt[deg_inv_sqrt == float('inf')]= 0
        D = coo_matrix((deg_inv_sqrt, (np.arange(size), np.arange(size))), shape=(size, size), dtype=np.float32)

        # Encoding real component
        A_sym = D.dot(A_sym).dot(D)
        L_real = diag - A_sym.multiply(operation_undirected) # Extraction of the real component of the laplacian

        # Encoding i component
        A_sym_2 = 0.5*((A - anti.antiparalell_different_weights(A)) + (A - anti.antiparalell_different_weights(A)).T)
        A_sym_2 = D.dot(A_sym_2).dot(D)
        L_imag_i = - A_sym_2.multiply(operation)

        # Encoding together the two imaginary components (j and k) 
        # where: 
        # 1) j --> is the real component
        # 2) k --> is the imaginary component
        
        L_imag_jk = - D.dot(0.5*(scipy.sparse.triu(anti.antiparalell_different_weights(A)))).dot(D) + D.dot(0.5*(scipy.sparse.triu(anti.antiparalell_different_weights(A)).T)).dot(D) \
        - D.dot(0.5*(scipy.sparse.tril(anti.antiparalell_different_weights(A)))).dot(D)*1j + \
        D.dot(0.5*(scipy.sparse.tril(anti.antiparalell_different_weights(anti.antiparalell_different_weights(A))).T)).dot(D)*1j   # Extraction of the i/j-imag component 
        #print(L_imag.toarray())
        
    edge_index_real, edge_weight_real = get_specific(L_real, device)
    
    edge_index_imag_i, edge_weight_imag_i = get_specific(L_imag_i, device)

    edge_index_imag_jk, edge_weight_imag_jk = get_specific(L_imag_jk, device)
    
    # Combino tutti gli edges delle diverse componenti
    edge_index = torch.cat((edge_index_real, edge_index_imag_i, edge_index_imag_jk), dim=1)
    #print('dimensioni edge', edge_index.shape)
    
    # Combino i pesi reali e complessi
    #print(edge_weight_real)
    edge_weight_real_1 = torch.cat((edge_weight_real, (edge_weight_imag_i - edge_weight_imag_i), (edge_weight_imag_jk - edge_weight_imag_jk)), dim=0)
    #print('dimensioni pesi reali', edge_weight_real_1.shape)
    #print(edge_weight_real_1)
    
    edge_weight_imag_i_1 = torch.cat(((edge_weight_real - edge_weight_real), edge_weight_imag_i, (edge_weight_imag_jk - edge_weight_imag_jk) ), dim=0)
    #print('dimensioni pesi imag_i', edge_weight_imag_i_1.shape)
    #print(edge_weight_imag_i_1)
    
    edge_weight_imag_jk_1 = torch.cat(((edge_weight_real - edge_weight_real), (edge_weight_imag_i - edge_weight_imag_i), edge_weight_imag_jk), dim=0)
    #print('dimensioni pesi imag jk', edge_weight_imag_jk_1.shape)
    #print(edge_weight_imag_jk_1)

    return edge_index, edge_weight_real_1.real, edge_weight_imag_i_1.imag,  edge_weight_imag_jk_1.real, edge_weight_imag_jk_1.imag
    




def __norm_quaternion_(
        edge_index,
        num_nodes: Optional[int],
        edge_weight: OptTensor,
        normalization: Optional[str],
        lambda_max,
        dtype: Optional[int] = None
    ):
        """
        Get  Sign-Magnetic Laplacian.
        
        Arg types:
            * edge_index (PyTorch Long Tensor) - Edge indices.
            * num_nodes (int, Optional) - Node features.
            * edge_weight (PyTorch Float Tensor, optional) - Edge weights corresponding to edge indices.
            * lambda_max (optional, but mandatory if normalization is None) - Largest eigenvalue of Laplacian.
        Return types:
            * edge_index, edge_weight_real, edge_weight_imag (PyTorch Float Tensor) - Magnetic laplacian tensor: edge index, real weights and imaginary weights.
        """
        edge_index, edge_weight = remove_self_loops(edge_index, edge_weight)
        edge_index, edge_weight_real, edge_weight_imag_i, edge_weight_imag_j, edge_weight_imag_k = \
        get_Quaternion_Laplacian(edge_index, edge_weight, normalization, dtype, num_nodes)
        lambda_max.to(edge_weight_real.device)

        edge_weight_real = (2.0 * edge_weight_real) / lambda_max
        edge_weight_real.masked_fill_(edge_weight_real == float("inf"), 0)
        
        _ , edge_weight_real = add_self_loops(
            edge_index, edge_weight_real, fill_value=-1.0, num_nodes=num_nodes
        )
        assert edge_weight_real is not None

        edge_weight_imag_i = (2.0 * edge_weight_imag_i) / lambda_max
        edge_weight_imag_i.masked_fill_(edge_weight_imag_i == float("inf"), 0)

        _ , edge_weight_imag_i = add_self_loops(
            edge_index, edge_weight_imag_i, fill_value=0, num_nodes=num_nodes )
        assert edge_weight_imag_i is not None
 
        edge_weight_imag_j = (2.0 * edge_weight_imag_j) / lambda_max
        edge_weight_imag_j.masked_fill_(edge_weight_imag_j == float("inf"), 0)

        _, edge_weight_imag_j = add_self_loops(
            edge_index, edge_weight_imag_j, fill_value=0, num_nodes=num_nodes )
        assert edge_weight_imag_j is not None

        edge_weight_imag_k = (2.0 * edge_weight_imag_k) / lambda_max
        edge_weight_imag_k.masked_fill_(edge_weight_imag_k == float("inf"), 0)

        edge_index, edge_weight_imag_k = add_self_loops(
            edge_index, edge_weight_imag_k, fill_value=0, num_nodes=num_nodes )
        assert edge_weight_imag_k is not None
        
        return edge_index, edge_weight_real, edge_weight_imag_i, edge_weight_imag_j, edge_weight_imag_k
        

def process_quaternion_laplacian(edge_index: torch.LongTensor, x_real: Optional[torch.Tensor] = None, edge_weight: Optional[torch.Tensor] = None,
                  normalization: Optional[str] = 'sym',
                  num_nodes: Optional[int] = None,
                  lambda_max=None,
                  return_lambda_max: bool = False,
):
  
    
    lambda_max = torch.tensor(2.0, dtype=x_real.dtype, device=x_real.device)

    assert lambda_max is not None
    node_dim = -2
    edge_index, norm_real, norm_imag_i, norm_imag_j, norm_imag_k  = __norm_quaternion_(edge_index,  x_real.size(node_dim),
                                         edge_weight, normalization,
                                         lambda_max, dtype=x_real.dtype)
    
    return edge_index, norm_real, norm_imag_i, norm_imag_j, norm_imag_k 