# pylint: disable=arguments-differ
"""VGG atrous network for object detection. support fusion layers
"""
from __future__ import division
import os
import mxnet as mx
from mxnet import gluon
from mxnet.gluon import nn
from mxnet.initializer import Xavier

__all__ = ['VGGAtrousExtractor', 'get_vgg_atrous_extractor', 'vgg16_atrous_320',
           'vgg16_atrous_512']

def _upsample(x, stride=2):
    """Simple upsampling layer by stack pixel alongside horizontal and vertical directions.

    Parameters
    ----------
    x : mxnet.nd.NDArray or mxnet.symbol.Symbol
        The input array.
    stride : int, default is 2
        Upsampling stride

    """
    return x.repeat(axis=-1, repeats=stride).repeat(axis=-2, repeats=stride)

class Normalize(gluon.HybridBlock):
    """Normalize layer described in https://arxiv.org/abs/1512.02325.

    Parameters
    ----------
    n_channel : int
        Number of channels of input.
    initial : float
        Initial value for the rescaling factor.
    eps : float
        Small value to avoid division by zero.

    """
    def __init__(self, n_channel, initial=1, eps=1e-5):
        super(Normalize, self).__init__()
        self.eps = eps
        with self.name_scope():
            self.scale = self.params.get('normalize_scale', shape=(1, n_channel, 1, 1),
                                         init=mx.init.Constant(initial))

    def hybrid_forward(self, F, x, scale):
        x = F.L2Normalization(x, mode='channel', eps=self.eps)
        return F.broadcast_mul(x, scale)

class VGGAtrousBase(gluon.HybridBlock):
    """VGG Atrous multi layer base network. You must inherit from it to define
    how the features are computed.

    Parameters
    ----------
    layers : list of int
        Number of layer for vgg base network.
    filters : list of int
        Number of convolution filters for each layer.
    batch_norm : bool, default is False
        If `True`, will use BatchNorm layers.

    """
    def __init__(self, layers, filters, batch_norm=False, **kwargs):
        super(VGGAtrousBase, self).__init__(**kwargs)
        assert len(layers) == len(filters)
        self.init = {
            'weight_initializer': Xavier(
                rnd_type='gaussian', factor_type='out', magnitude=2),
            'bias_initializer': 'zeros'
        }
        with self.name_scope():
            # we use pre-trained weights from caffe, initial scale must change
            init_scale = mx.nd.array([0.229, 0.224, 0.225]).reshape((1, 3, 1, 1)) * 255
            self.init_scale = self.params.get_constant('init_scale', init_scale)
            self.stages = nn.HybridSequential()
            for l, f in zip(layers, filters):
                stage = nn.HybridSequential(prefix='')
                with stage.name_scope():
                    for _ in range(l):
                        stage.add(nn.Conv2D(f, kernel_size=3, padding=1, **self.init))
                        if batch_norm:
                            stage.add(nn.BatchNorm())
                        stage.add(nn.Activation('relu'))
                self.stages.add(stage)
            # self.stages have [stage1, stage2, stage3, stage4, stage5] now
            # the stride are   [1     , 2     , 4     , 8     , 16] before max pool.

            # use dilated convolution instead of dense layers
            stage = nn.HybridSequential(prefix='dilated_')
            with stage.name_scope():
                stage.add(nn.Conv2D(1024, kernel_size=3, padding=6, dilation=6, **self.init))
                if batch_norm:
                    stage.add(nn.BatchNorm())
                stage.add(nn.Activation('relu'))
                stage.add(nn.Conv2D(1024, kernel_size=1, **self.init))
                if batch_norm:
                    stage.add(nn.BatchNorm())
                stage.add(nn.Activation('relu'))
            self.stages.add(stage)

            # normalize layer for 4-th
            self.norm4 = Normalize(filters[3], 20)
            self.norm5 = Normalize(1024, 8)

    def hybrid_forward(self, F, x, init_scale):
        raise NotImplementedError

class VGGAtrousExtractor(VGGAtrousBase):
    """VGG Atrous multi layer feature extractor which produces multiple output
    feauture maps.

    Parameters
    ----------
    layers : list of int
        Number of layer for vgg base network.
    filters : list of int
        Number of convolution filters for each layer.
    extras : list of list
        Extra layers configurations.
    batch_norm : bool
        If `True`, will use BatchNorm layers.

    """
    def __init__(self, layers, filters, extras, batch_norm=False, **kwargs):
        super(VGGAtrousExtractor, self).__init__(layers, filters, batch_norm, **kwargs)
        with self.name_scope():
            self.extras = nn.HybridSequential()
            for i, config in enumerate(extras):
                extra = nn.HybridSequential(prefix='extra%d_'%(i))
                with extra.name_scope():
                    for f, k, s, p in config:
                        extra.add(nn.Conv2D(f, k, s, p, **self.init))
                        if batch_norm:
                            extra.add(nn.BatchNorm())
                        extra.add(nn.Activation('relu'))
                self.extras.add(extra)

            # ------------------------------------------- feature trans -----------------------------------------------

            self.last_layer_trans = nn.HybridSequential("trans_last_layer")  # trans for the last layer
            for _ in range(3):
                self.last_layer_trans.add(
                    nn.Conv2D(256, kernel_size=(3, 3,), strides=(1, 1), padding=1, **self.init))
                if batch_norm:
                    self.last_layer_trans.add(nn.BatchNorm())
                self.last_layer_trans.add(nn.Activation('relu'))  # Don't add relu performs better. (maybe)

            self.transitions = nn.HybridSequential()  # trans for other 3 layers
            for _ in range(3):
                transition = nn.HybridSequential(prefix="translayer%d_" % _)
                transition.add(nn.Conv2D(256, kernel_size=(3, 3,), strides=(1, 1), padding=1, **self.init))
                if batch_norm:
                    transition.add(nn.BatchNorm())
                transition.add(nn.Activation('relu'))
                transition.add(nn.Conv2D(256, kernel_size=(3, 3,), strides=(1, 1), padding=1, **self.init))
                if batch_norm:
                    transition.add(nn.BatchNorm())

                self.transitions.add(transition)

            # upsample module
            # self.upsamples = nn.HybridSequential()
            # for _ in range(3):
            #     upsample = nn.HybridSequential(prefix="upsample%d_" % _)
            #     upsample.add(nn.Conv2DTranspose(channels=256, kernel_size=(4, 4), strides=(2, 2), **self.init))
            #     if batch_norm:
            #         upsample.add(nn.BatchNorm())
            #     self.upsamples.add(upsample)

            self.fusions = nn.HybridSequential()  # for fusion
            for _ in range(3):
                fusion = nn.HybridSequential("fuse%d_" % _)
                fusion.add(nn.Conv2D(256, kernel_size=(3, 3,), strides=(1, 1), padding=1, **self.init))
                if batch_norm:
                    fusion.add(nn.BatchNorm())
                fusion.add(nn.Activation('relu'))
                self.fusions.add(fusion)

    def hybrid_forward(self, F, x, init_scale):
        x = F.broadcast_mul(x, init_scale)
        assert len(self.stages) == 6
        outputs = {"ARM_features": [],
                   "ODM_features": []}
        # diff from the paper which use [conv4_3, conv5_3, conv_fc7, conv6_2]
        # outputs["ARM_features] ==>[conv4_3, conv_fc7, conv6_2, conv7_2]
        # strid are ==>             [8      , 16     , 32      , 64     ]

        for stage in self.stages[:3]:
            x = stage(x)
            x = F.Pooling(x, pool_type='max', kernel=(2, 2), stride=(2, 2),
                          pooling_convention='full')
        x = self.stages[3](x)
        norm4 = self.norm4(x)  # norm for conv4_3
        outputs["ARM_features"].append(norm4)  # conv4_3
        x = F.Pooling(x, pool_type='max', kernel=(2, 2), stride=(2, 2),
                      pooling_convention='full')
        x = self.stages[4](x)
        x = F.Pooling(x, pool_type='max', kernel=(3, 3), stride=(1, 1), pad=(1, 1),
                      pooling_convention='full')
        x = self.stages[5](x)
        norm5 = self.norm5(x)
        outputs["ARM_features"].append(norm5)  # conv_fc7.

        for extra in self.extras:
            x = extra(x)
            outputs["ARM_features"].append(x)  # conv6_2 and conv7_2

        # ---------------------------------------------- features fusion ----------------------------------------------
        # transitions module
        # [conv3_1_(bn)relu, conv3_1_(bn), conv3_1_(bn)relu]
        outputs["ODM_features"] = [None] * 4

        # for conv6_2. do not upsample.
        outputs["ODM_features"][-1] = self.last_layer_trans((outputs['ARM_features'][-1]))  # P6

        for i in range(3, 0, -1):  # build P5, P4, P3
            deep_fet, shallow_fet = outputs["ODM_features"][i], outputs["ARM_features"][i-1]
            upsampled_fet = _upsample(deep_fet)
            transition_fet = self.transitions[i-1](shallow_fet)
            ele_sum = F.Activation(upsampled_fet+transition_fet, act_type='relu')
            outputs["ODM_features"][i-1] = ele_sum

        for i in range(3):
            outputs["ODM_features"][i] = self.fusions[i](outputs["ODM_features"][i])

        return outputs

vgg_spec = {
    11: ([1, 1, 2, 2, 2], [64, 128, 256, 512, 512]),
    13: ([2, 2, 2, 2, 2], [64, 128, 256, 512, 512]),
    16: ([2, 2, 3, 3, 3], [64, 128, 256, 512, 512]),
    19: ([2, 2, 4, 4, 4], [64, 128, 256, 512, 512])
}

extra_spec = {
    320: [((256, 1, 1, 0), (512, 3, 2, 1)),
          ((128, 1, 1, 0), (256, 3, 2, 1))],  # only four extra conv layers
    512: [((256, 1, 1, 0), (512, 3, 2, 1)),
          ((128, 1, 1, 0), (256, 3, 2, 1))],
}

def get_vgg_atrous_extractor(num_layers, im_size, pretrained=False, ctx=mx.cpu(),
                             root=os.path.join('~', '.mxnet', 'models'), **kwargs):
    """Get VGG atrous feature extractor networks.

    Parameters
    ----------
    num_layers : int
        VGG types, can be 11,13,16,19.
    im_size : int
        VGG detection input size, can be 320, 512.
    pretrained : bool or str
        Boolean value controls whether to load the default pretrained weights for model.
        String value represents the hashtag for a certain version of pretrained weights.
    ctx : mx.Context
        Context such as mx.cpu(), mx.gpu(0).
    root : str
        Model weights storing path.

    Returns
    -------
    mxnet.gluon.HybridBlock
        The returned network.

    """
    layers, filters = vgg_spec[num_layers]
    extras = extra_spec[im_size]
    net = VGGAtrousExtractor(layers, filters, extras, **kwargs)
    if pretrained:
        from ..model_store import get_model_file
        batch_norm_suffix = '_bn' if kwargs.get('batch_norm') else ''
        net.initialize(ctx=ctx)
        net.load_parameters(get_model_file('vgg%d_atrous%s' % (num_layers, batch_norm_suffix),
                                           tag=pretrained, root=root), ctx=ctx, allow_missing=True)
    return net

def vgg16_atrous_320(**kwargs):
    """Get VGG atrous 16 layer 320 in_size feature extractor networks."""
    return get_vgg_atrous_extractor(16, 320, **kwargs)

def vgg16_atrous_512(**kwargs):
    """Get VGG atrous 16 layer 512 in_size feature extractor networks."""
    return get_vgg_atrous_extractor(16, 512, **kwargs)
