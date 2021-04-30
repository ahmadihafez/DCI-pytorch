import torch
import torch.nn as nn
from layers import GraphCNN, MLP
import torch.nn.functional as F
import sys
sys.path.append("models/")

class ModelTest(nn.Module):
    def __init__(self, num_layers, num_mlp_layers, input_dim, hidden_dim, final_dropout, neighbor_pooling_type, num_user, num_object, device):
        super(ModelTest, self).__init__()
        self.gin = GraphCNN(num_layers, num_mlp_layers, input_dim, hidden_dim, final_dropout, neighbor_pooling_type, num_user, num_object, device)
        self.linear_prediction = nn.Linear(hidden_dim, 1)
        self.final_dropout = final_dropout
        
    def forward(self, seq1, adj):
        h_1 = self.gin(adj, seq1)
        score_final_layer = F.dropout(self.linear_prediction(h_1), self.final_dropout, training = self.training)
        return score_final_layer