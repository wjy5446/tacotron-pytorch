import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

from attention import Attention

###########
# Encoder Module
###########

class CBHG(nn.Module):
    def __init__(self, K, input_size, hidden_size):
        super(CBHG_encoder, self).__init__()

        ## conv1d bank
        layers_conv1d_bank = []

        for k in range(1, K+1):
            layers_conv1d_bank += [
                nn.Sequential(
                    nn.Conv1d(input_size, hidden_size, kernel_size=k//2),
                    nn.ReLU(),
                    nn.BatchNorm1d(hidden_size, momentum=0.99, eps=1e-3)
                )
            ]

        self.conv1d_bank = layers_conv1d_bank

        ## maxpooling
        self.max1d = nn.MaxPool1d(2, 1, 1)

        ## conv1d_projection
        self.conv1d_projection = nn.Sequential(
            nn.Conv1d(K * hidden_size, hidden_size, kernel_size=3, stride=1, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_size, momentum=0.99, eps=1e-3),
            nn.Conv1d(hidden_size, hidden_size, kernel_size=3, stride=1, padding=1),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_size, momentum=0.99, eps=1e-3),
        )

        ## highway network
        layers_highway = []
        for k in range(4):
            layers_highway += [HighwayNet(input_size=hidden_size,
                                          output_size=hidden_size)]
        self.highway = nn.Sequential(*layers_highway)

        ## GRU
        self.gru = nn.GRU(input_size=hidden_size,
                          hidden_size=hidden_size,
                          bidirectional=True,
                          batch_first=True)

    def forward(self, input):
        x = input.transpose(1, 2) # (batch_size, dim, time)
        seq_time = x.size(-1)

        ## conv1d_banks
        y = torch.cat([conv1d(x)[:,:,:seq_time] for conv1d in self.conv1d_bank], dim=1) # (batch_size, K * dim, time)

        ## maxpooling
        y = self.max1d(y)[:,:,:seq_time] # (batch_size, K * dim, time)

        ## conv1d projections
        y = self.conv1d_projections(y)  # (batch_size, hidden_dim, time)
        y = y + x

        y = y.transpose(1, 2) # (batch_size, time, hidden_dim)

        ## highway
        y = self.highway(y)  # (batch_size, time, hidden_dim)

        ## gru
        output, _ = self.gru(y) # (batch_size, time, hidden_dim * 2)

        return output


class Embedding(nn.Module):
    def __init__(self, input_size, embedd_size):
        """
        Embedding

        :param input_size:
        :param embedd_size:
        """

        super(Embedding, self).__init__()
        self.embeddingnet = nn.Embedding(input_size, embedd_size,
                                         padding_idx=?)

        def forward(self, input):
            embed = self.embeddingnet(input)
            return embed

class PreNet(nn.Module):
    """Prenet"""

    def __init__(self, input_size, hidden_size, output_size, dropout_rate):
        super(PreNet, self).__init__()

        self.net = nn.Sequential(
            nn.Linear(input_size, hidden_size),
            nn.ReLU(),
            nn.Dropout(dropout_rate),
            nn.Linear(hidden_size, output_size),
            nn.ReLU(),
            nn.Dropout(dropout_rate)
        )

    def forward(self, input):
        return self.net(input)

class HighwayNet(nn.Module):
    """HighwayNet"""
    def __init__(self, input_size, output_size):
        super(HighwayNet, self).__init__()
        self.H = nn.Linear(input_size, output_size)
        self.T = nn.Linear(input_size, output_size)

    def forward(self, input):
        h = F.relu(self.H(input))
        t = torch.sigmoid(self.T(input))

        return h * t + input * (1. - t)

###########
# Decoder Module
###########

def binaryMask(x, length, mask_dim=1):
    '''

    :param x: (batch_size, seq_length, dim)
    :param length:
    :param mask_dim:
    :return:
    '''

    batch_size, seq_length, dim = x.size()
    m = 0
    for b in range(batch_size):
        mb = []
        true_len = length[b]

        for l in range(seq_length):
            if l < true_len:
                mb.append(np.ones((dim, )))
            else:
                mb.append(np.zeros((dim, )))
        m.append(mb)
    m = np.asarray(m)
    m = torch.from_numpy(m).to(x.device).type(x.dtype)

    return m


class AttentionRNN(nn.Module):
    def __init__(self, input_size, hidden_size, text_embed_size):
        super(AttentionRNN, self).__init__()
        self.gru = nn.GRU(input_size=input_size,
                          hidden_size=hidden_size,
                          batch_first=True)
        self.text_embed_size = text_embed_size
        self.attention = Attention(query_size=hidden_size,
                                   context_size=text_embed_size)

    def forward(self, input, memory, text_length):
        frame_len = input.size(1)

        self.gru.flatten_parameters()

        hidden, state = self.gru(input)

        mask_attention = self.makeAttnMask(memory, text_length, frame_len)
        align = self.attention(query=hidden, memory=memory, memory_mask=mask_attention)
        align_tiled = align.unsqueeze(3).repeat(1, 1, 1, self.text_embed_size)
        memory_tiled = memory.unsqueeze(1).repeat(1, frame_len, 1, 1)
        context = torch.sum(align_tiled * memory_tiled, dim=2)
        out = torch.cat([context, hidden], dim=2)

        return out, align, state

    def makeAttnMask(self, memory, encoder_length, frame_len):
        batch_size = memory.size(0)
        seq_len = memory.size(1)
        m = []

        for b in range(batch_size):
            mb = []
            true_len = encoder_length[b]
            for l in range(seq_len):
                if l < true_len:
                    mb.append(np.zeros((frame_len, ), dtype=np.int32))
                else:
                    mb.append(np.ones((frame_len, ), dtype=np.int32))
            m.append(mb)

        m = np.swapaxes(np.asarray(m), 1, 2) # (batch_size, frame_len, seq_len)
        m = torch.from_numpy(m).type(torch.ByteTensor).to(memory.device)

        return m

class DecoderRNN(nn.Module):
    def __init__(self, input_size, output_size, r=5):
        super(DecoderRNN, self).__init__()
        self.output_size = output_size
        self.r = r
        self.gru1 = nn.GRU(input_size=input_size,
                           hidden_size=input_size, batch_first=True)
        self.gru2 = nn.GRU(input_size=input_size,
                           hidden_size=input_size, batch_first=True)
        self.fc = nn.Linear(input_size, r * output_size)

    def forward(self, input):
        '''

        :param input: (batch_size, time, dim)
        :return:
        '''
        batch_size = input.size(0)
        frame_len = input.size(1)

        output1, _ = self.gru1(input) # output (batch_size, time, hidden_dim)
        output2, _ = self.gru2(input + output1) # output (batch_size, time, hidden_dim)

        output = self.fc(output1 + output2 + input) # output (batch_size, time, r * output_size)
        output = output.view(batch_size, frame_len * self.r, self.output_size)

        return output
