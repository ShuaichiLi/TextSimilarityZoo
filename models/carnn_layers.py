# -*- coding: utf-8 -*-
# @Time    : 2018/11/16 19:37
# @Author  : Tianchiyue
# @File    : carnn_layers.py
# @Software: PyCharm Community Edition

from keras.layers import Dot, Permute, Multiply
from keras.layers import Conv2D, MaxPooling2D
import tensorflow as tf
from keras.layers import Dense, Input, Concatenate, Add, Lambda, Dropout, Activation, SpatialDropout1D, Reshape
from keras import initializers
from keras.engine import InputSpec, Layer
from keras.layers.embeddings import Embedding
from keras.layers import TimeDistributed
from keras.initializers import *
from keras.activations import softmax


# -- Compare layer --
COMPARE_LAYER_DROPOUT = 0.2

# -- CARNN --
CARNN_RNN_SIZE = 76
CARNN_AGGREATION_DROPOUT = 0.2
CARNN_COMPARE_LAYER_OUTSIZE = 8
CARNN_COMPARE_LAYER_HIDDEN_SIZE = 288
CARNN_CONCAT_DROPOUT = 0.5
CARNN_MLP_HIDDEN_SIZE = 256

EMBEDDING_DROPOUT = 0.5
CONCAT_DROPOUT = 0.7


class LayerNormalization(Layer):
    def __init__(self, eps=1e-6, **kwargs):
        self.eps = eps
        super().__init__(**kwargs)

    def build(self, input_shape):
        self.gamma = self.add_weight(name='gamma', shape=input_shape[-1:],
                                     initializer=Ones(), trainable=True)
        self.beta = self.add_weight(name='beta', shape=input_shape[-1:],
                                    initializer=Zeros(), trainable=True)
        super().build(input_shape)

    def call(self, x):
        mean = K.mean(x, axis=-1, keepdims=True)
        std = K.std(x, axis=-1, keepdims=True)
        return self.gamma * (x - mean) / (std + self.eps) + self.beta

    def compute_output_shape(self, input_shape):
        return input_shape


class AttentionWeightedAverage(Layer):
    """
    Computes a weighted average of the different channels across timesteps.
    Uses 1 parameter pr. channel to compute the attention value for a single timestep.
    """

    def __init__(self, return_attention=False, **kwargs):
        self.init = initializers.get('uniform')
        self.supports_masking = True
        self.return_attention = return_attention
        super(AttentionWeightedAverage, self).__init__(**kwargs)

    def build(self, input_shape):
        self.input_spec = [InputSpec(ndim=3)]
        assert len(input_shape) == 3

        self.W = self.add_weight(shape=(input_shape[2], 1),
                                 name='{}_W'.format(self.name),
                                 initializer=self.init)
        self.trainable_weights = [self.W]
        super(AttentionWeightedAverage, self).build(input_shape)

    def call(self, x, mask=None):
        # computes a probability distribution over the timesteps
        # uses 'max trick' for numerical stability
        # reshape is done to avoid issue with Tensorflow
        # and 1-dimensional weights
        logits = K.dot(x, self.W)
        x_shape = K.shape(x)
        logits = K.reshape(logits, (x_shape[0], x_shape[1]))
        ai = K.exp(logits - K.max(logits, axis=-1, keepdims=True))

        # masked timesteps have zero weight
        if mask is not None:
            mask = K.cast(mask, K.floatx())
            ai = ai * mask
        att_weights = ai / (K.sum(ai, axis=1, keepdims=True) + K.epsilon())
        weighted_input = x * K.expand_dims(att_weights)
        result = K.sum(weighted_input, axis=1)
        if self.return_attention:
            return [result, att_weights]
        return result

    def get_output_shape_for(self, input_shape):
        return self.compute_output_shape(input_shape)

    def compute_output_shape(self, input_shape):
        output_len = input_shape[2]
        if self.return_attention:
            return [(input_shape[0], output_len), (input_shape[0], input_shape[1])]
        return (input_shape[0], output_len)

    def compute_mask(self, input, input_mask=None):
        if isinstance(input_mask, list):
            return [None] * len(input_mask)
        else:
            return None


class ScaledDotProductAttention():
    def __init__(self, d_model, attn_dropout=0.1):
        self.temper = np.sqrt(d_model)
        self.dropout = Dropout(attn_dropout)

    def __call__(self, q, k, v, mask):
        attn = Lambda(lambda x: K.batch_dot(x[0], x[1], axes=[2, 2]) / self.temper)([q, k])
        if mask is not None:
            mmask = Lambda(lambda x: (-1e+10) * (1 - x))(mask)
            attn = Add()([attn, mmask])
        attn = Activation('softmax')(attn)
        attn = self.dropout(attn)
        output = Lambda(lambda x: K.batch_dot(x[0], x[1]))([attn, v])
        return output, attn


class MultiHeadAttention():
    # mode 0 - big martixes, faster; mode 1 - more clear implementation
    def __init__(self, n_head, d_model, d_k, d_v, dropout, mode=1):
        self.mode = mode
        self.n_head = n_head
        self.d_k = d_k
        self.d_v = d_v
        self.dropout = dropout
        if mode == 0:
            self.qs_layer = Dense(n_head * d_k, use_bias=False)
            self.ks_layer = Dense(n_head * d_k, use_bias=False)
            self.vs_layer = Dense(n_head * d_v, use_bias=False)
        elif mode == 1:
            self.qs_layers = []
            self.ks_layers = []
            self.vs_layers = []
            for _ in range(n_head):
                self.qs_layers.append(TimeDistributed(Dense(d_k, use_bias=False)))
                self.ks_layers.append(TimeDistributed(Dense(d_k, use_bias=False)))
                self.vs_layers.append(TimeDistributed(Dense(d_v, use_bias=False)))
        self.attention = ScaledDotProductAttention(d_model)
        self.layer_norm = LayerNormalization()
        self.w_o = TimeDistributed(Dense(d_model))

    def __call__(self, q, k, v, mask=None):
        d_k, d_v = self.d_k, self.d_v
        n_head = self.n_head

        if self.mode == 0:
            qs = self.qs_layer(q)  # [batch_size, len_q, n_head*d_k]
            ks = self.ks_layer(k)
            vs = self.vs_layer(v)

            def reshape1(x):
                s = tf.shape(x)  # [batch_size, len_q, n_head * d_k]
                x = tf.reshape(x, [s[0], s[1], n_head, d_k])
                x = tf.transpose(x, [2, 0, 1, 3])
                x = tf.reshape(x, [-1, s[1], d_k])  # [n_head * batch_size, len_q, d_k]
                return x

            qs = Lambda(reshape1)(qs)
            ks = Lambda(reshape1)(ks)
            vs = Lambda(reshape1)(vs)

            mask = Lambda(lambda x: K.repeat_elements(x, n_head, 0))(mask)
            head, attn = self.attention(qs, ks, vs, mask=mask)

            def reshape2(x):
                s = tf.shape(x)  # [n_head * batch_size, len_v, d_v]
                x = tf.reshape(x, [n_head, -1, s[1], s[2]])
                x = tf.transpose(x, [1, 2, 0, 3])
                x = tf.reshape(x, [-1, s[1], n_head * d_v])  # [batch_size, len_v, n_head * d_v]
                return x

            head = Lambda(reshape2)(head)
        elif self.mode == 1:
            heads = [];
            attns = []
            for i in range(n_head):
                qs = self.qs_layers[i](q)
                ks = self.ks_layers[i](k)
                vs = self.vs_layers[i](v)
                head, attn = self.attention(qs, ks, vs, mask)
                heads.append(head);
                attns.append(attn)
            head = Concatenate()(heads)
            attn = Concatenate()(attns)
        outputs = self.w_o(head)
        outputs = Dropout(self.dropout)(outputs)
        outputs = Add()([outputs, q])
        return self.layer_norm(outputs), attn


class SelfAttention():
    "Wrapper of self multi-head attention"

    def __init__(self, d_model=300, d_inner_hid=300, n_head=8, d_k=50, d_v=50, dropout=0.1):
        self.self_att_layer = MultiHeadAttention(n_head, d_model, d_k, d_v, dropout=dropout)

    def __call__(self, src_seq, enc_input):
        mask = Lambda(lambda x: get_padding_mask(x, x))(src_seq)
        output, attn_weights = self.self_att_layer(enc_input, enc_input, enc_input, mask=mask)
        return output


def get_padding_mask(q, k):
    ones = K.expand_dims(K.ones_like(q, 'float32'), -1)
    mask = K.cast(K.expand_dims(K.not_equal(k, 0), 1), 'float32')
    mask = K.batch_dot(ones, mask, axes=[2, 1])
    return mask


def unchanged_shape(input_shape):
    "Function for Lambda layer"
    return input_shape


def substract(input_1, input_2):
    "Substract element-wise"
    out_ = Lambda(lambda x: K.abs(x[0] - x[1]))([input_1, input_2])
    return out_


def interaction(input_1, input_2):
    "Get the interaction then concatenate results"
    mult = Multiply()([input_1, input_2])
    add = Add()([input_1, input_2])
    sub = substract(input_1, input_2)
    # distance = el_distance(input_1, input_2)

    out_ = Concatenate()([sub, mult, add, ])
    return out_


def apply_multiple(input_, layers):
    "Apply layers to input then concatenate result"
    if not len(layers) > 1:
        raise ValueError('Layers list should contain more than 1 layer')
    else:
        agg_ = []
        for layer in layers:
            agg_.append(layer(input_))
        out_ = Concatenate()(agg_)
    return out_


def time_distributed(input_, layers):
    "Apply a list of layers in TimeDistributed mode"
    out_ = []
    node_ = input_
    for layer_ in layers:
        node_ = TimeDistributed(layer_)(node_)
    out_ = node_
    return out_


def soft_attention_alignment(input_1, input_2):
    "Align text representation with neural soft attention"
    attention = Dot(axes=-1)([input_1, input_2])
    w_att_1 = Lambda(lambda x: softmax(x, axis=1),
                     output_shape=unchanged_shape)(attention)
    w_att_2 = Permute((2, 1))(Lambda(lambda x: softmax(x, axis=2),
                                     output_shape=unchanged_shape)(attention))
    in1_aligned = Dot(axes=1)([w_att_1, input_1])
    in2_aligned = Dot(axes=1)([w_att_2, input_2])
    return in1_aligned, in2_aligned


def get_char_embeddings(q1_c, q2_c, max_sequence_length, char_embedding_size, feature_map_nums, window_sizes,
                        embedding_dropout):
    chars_embedding = Embedding(CHAR_VOCAB_SIZE, char_embedding_size)
    q1_char = chars_embedding(q1_c)
    q2_char = chars_embedding(q2_c)

    cnns, pools = _get_cnns(max_sequence_length, char_embedding_size, feature_map_nums, window_sizes, )
    q1_char = _char_cnn(cnns, pools, char_embedding_size, max_sequence_length, feature_map_nums, q1_char)
    q2_char = _char_cnn(cnns, pools, char_embedding_size, max_sequence_length, feature_map_nums, q2_char)

    q1_char = SpatialDropout1D(embedding_dropout)(q1_char)
    q2_char = SpatialDropout1D(embedding_dropout)(q2_char)

    return q1_char, q2_char


def _get_cnns(seq_length, length, feature_map_nums, kernels):
    cnns, pools = [], []
    for feature_map_num, kernel in zip(feature_map_nums, kernels):
        reduced_l = length - kernel + 1
        conv = Conv2D(feature_map_num, (1, kernel), activation='relu', data_format="channels_last")
        cnns.append(conv)
        maxp = MaxPooling2D((1, reduced_l), data_format="channels_last")
        pools.append(maxp)
    return cnns, pools


def _char_cnn(cnns, pools, length, seq_length, feature_maps, char_embeddings):
    concat_input = []
    for i in range(len(cnns)):
        conved = cnns[i](char_embeddings)
        pooled = pools[i](conved)
        concat_input.append(pooled)

    x = Concatenate()(concat_input)
    x = Reshape((seq_length, sum(feature_maps)))(x)
    x = Dropout(0.1)(x)
    return x


