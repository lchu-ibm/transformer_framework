# from Ross Wightmam with mods:
# https://github.com/huggingface/pytorch-image-models/blob/main/timm/layers/pos_embed_rel.py


import math
from typing import Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from mlp.base_mlp import LinearMLP
from models.smart_vit.weight_init import trunc_normal_


def gen_relative_position_index(
    q_size: Tuple[int, int], k_size: Tuple[int, int] = None, class_token: bool = False
) -> torch.Tensor:
    # Adapted with significant modifications from Swin / BeiT codebases
    # get pair-wise relative position index for each token inside the window
    q_coords = torch.stack(
        torch.meshgrid([torch.arange(q_size[0]), torch.arange(q_size[1])])
    ).flatten(
        1
    )  # 2, Wh, Ww
    if k_size is None:
        k_coords = q_coords
        k_size = q_size
    else:
        # different q vs k sizes is a WIP
        k_coords = torch.stack(
            torch.meshgrid([torch.arange(k_size[0]), torch.arange(k_size[1])])
        ).flatten(1)
    relative_coords = q_coords[:, :, None] - k_coords[:, None, :]  # 2, Wh*Ww, Wh*Ww
    relative_coords = relative_coords.permute(1, 2, 0)  # Wh*Ww, Wh*Ww, 2
    _, relative_position_index = torch.unique(
        relative_coords.view(-1, 2), return_inverse=True, dim=0
    )

    if class_token:
        # handle cls to token & token 2 cls & cls to cls as per beit for rel pos bias
        # NOTE not intended or tested with MLP log-coords
        max_size = (max(q_size[0], k_size[0]), max(q_size[1], k_size[1]))
        num_relative_distance = (2 * max_size[0] - 1) * (2 * max_size[1] - 1) + 3
        relative_position_index = F.pad(relative_position_index, [1, 0, 1, 0])
        relative_position_index[0, 0:] = num_relative_distance - 3
        relative_position_index[0:, 0] = num_relative_distance - 2
        relative_position_index[0, 0] = num_relative_distance - 1

    return relative_position_index.contiguous()


class RelPosBias(nn.Module):
    """Relative Position Bias
    Adapted from Swin-V1 relative position bias impl, modularized.
    """

    def __init__(self, window_size, num_heads, prefix_tokens=0):
        super().__init__()
        assert prefix_tokens <= 1
        self.window_size = window_size
        self.window_area = window_size[0] * window_size[1]
        self.bias_shape = (self.window_area + prefix_tokens,) * 2 + (num_heads,)

        num_relative_distance = (2 * window_size[0] - 1) * (
            2 * window_size[1] - 1
        ) + 3 * prefix_tokens
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros(num_relative_distance, num_heads)
        )
        self.register_buffer(
            "relative_position_index",
            gen_relative_position_index(
                self.window_size, class_token=prefix_tokens > 0
            ),
            persistent=False,
        )

        self.init_weights()

    def init_weights(self):
        trunc_normal_(self.relative_position_bias_table, std=0.02)

    def get_bias(self) -> torch.Tensor:
        relative_position_bias = self.relative_position_bias_table[
            self.relative_position_index.view(-1)
        ]
        # win_h * win_w, win_h * win_w, num_heads
        relative_position_bias = relative_position_bias.view(self.bias_shape).permute(
            2, 0, 1
        )
        return relative_position_bias.unsqueeze(0).contiguous()

    def forward(self, attn, shared_rel_pos: Optional[torch.Tensor] = None):
        return attn + self.get_bias()


def gen_relative_log_coords(
    win_size: Tuple[int, int],
    pretrained_win_size: Tuple[int, int] = (0, 0),
    mode="swin",
):
    # print(f"relpos mode = {mode=}, and {pretrained_win_size=}")
    assert mode in ("swin", "cr")
    # as per official swin-v2 impl, supporting timm specific 'cr' log coords as well
    relative_coords_h = torch.arange(
        -(win_size[0] - 1), win_size[0], dtype=torch.float32
    )
    relative_coords_w = torch.arange(
        -(win_size[1] - 1), win_size[1], dtype=torch.float32
    )
    relative_coords_table = torch.stack(
        torch.meshgrid([relative_coords_h, relative_coords_w])
    )
    relative_coords_table = relative_coords_table.permute(
        1, 2, 0
    ).contiguous()  # 2*Wh-1, 2*Ww-1, 2
    if mode == "swin":
        if pretrained_win_size[0] > 0:
            relative_coords_table[:, :, 0] /= pretrained_win_size[0] - 1
            relative_coords_table[:, :, 1] /= pretrained_win_size[1] - 1
        else:
            relative_coords_table[:, :, 0] /= win_size[0] - 1
            relative_coords_table[:, :, 1] /= win_size[1] - 1
        relative_coords_table *= 8  # normalize to -8, 8
        relative_coords_table = (
            torch.sign(relative_coords_table)
            * torch.log2(1.0 + relative_coords_table.abs())
            / math.log2(8)
        )
    else:
        # mode == 'cr'
        relative_coords_table = torch.sign(relative_coords_table) * torch.log(
            1.0 + relative_coords_table.abs()
        )
    # print(f"{relative_coords_table.shape=}")
    return relative_coords_table


class RelPosMlp(nn.Module):
    """Log-Coordinate Relative Position MLP
    Based on ideas presented in Swin-V2 paper (https://arxiv.org/abs/2111.09883)

    This impl covers the 'swin' implementation as well as two timm specific modes ('cr', and 'rw')
    """

    def __init__(
        self,
        window_size,
        num_heads=8,
        hidden_dim=256,  # up default from 128 to 256
        prefix_tokens=0,
        mode="cr",
        pretrained_window_size=(0, 0),
    ):
        super().__init__()
        print(f"{hidden_dim=}")
        self.window_size = window_size
        self.window_area = self.window_size[0] * self.window_size[1]
        self.prefix_tokens = prefix_tokens
        self.num_heads = num_heads
        self.bias_shape = (self.window_area,) * 2 + (num_heads,)
        if mode == "swin":
            self.bias_act = nn.Sigmoid()
            self.bias_gain = 16
            mlp_bias = (True, False)
        else:
            self.bias_act = nn.Identity()
            self.bias_gain = None
            mlp_bias = True

        self.mlp = LinearMLP(
            2,  # x, y
            hidden_features=hidden_dim,
            out_features=num_heads,
            act_layer=nn.ReLU,
            bias=mlp_bias,
            drop=(0.125, 0.0),
        )

        self.register_buffer(
            "relative_position_index",
            gen_relative_position_index(window_size),
            persistent=False,
        )

        # get relative_coords_table
        self.register_buffer(
            "rel_coords_log",
            gen_relative_log_coords(window_size, pretrained_window_size, mode=mode),
            persistent=False,
        )

    def get_bias(self) -> torch.Tensor:
        relative_position_bias = self.mlp(self.rel_coords_log)
        if self.relative_position_index is not None:
            relative_position_bias = relative_position_bias.view(-1, self.num_heads)[
                self.relative_position_index.view(-1)
            ]  # Wh*Ww,Wh*Ww,nH
            relative_position_bias = relative_position_bias.view(self.bias_shape)
        relative_position_bias = relative_position_bias.permute(2, 0, 1)
        relative_position_bias = self.bias_act(relative_position_bias)
        if self.bias_gain is not None:
            relative_position_bias = self.bias_gain * relative_position_bias
        if self.prefix_tokens:
            relative_position_bias = F.pad(
                relative_position_bias, [self.prefix_tokens, 0, self.prefix_tokens, 0]
            )
        return relative_position_bias.unsqueeze(0).contiguous()

    def forward(self, attn, shared_rel_pos: Optional[torch.Tensor] = None):
        return attn + self.get_bias()


def generate_lookup_tensor(
    length: int,
    max_relative_position: Optional[int] = None,
):
    """Generate a one_hot lookup tensor to reindex embeddings along one dimension.

    Args:
        length: the length to reindex to.
        max_relative_position: the maximum relative position to consider.
            Relative position embeddings for distances above this threshold
            are zeroed out.
    Returns:
        a lookup Tensor of size [length, length, vocab_size] that satisfies
            ret[n,m,v] = 1{m - n + max_relative_position = v}.
    """
    if max_relative_position is None:
        max_relative_position = length - 1
    # Return the cached lookup tensor, otherwise compute it and cache it.
    vocab_size = 2 * max_relative_position + 1
    ret = torch.zeros(length, length, vocab_size)
    for i in range(length):
        for x in range(length):
            v = x - i + max_relative_position
            if abs(x - i) > max_relative_position:
                continue
            ret[i, x, v] = 1
    return ret
