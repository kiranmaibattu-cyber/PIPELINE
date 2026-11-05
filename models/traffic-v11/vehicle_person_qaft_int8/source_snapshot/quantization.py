"""Train-time INT8 simulation. Hardware kernels and export are intentionally deferred."""
import copy
import torch
from torch import nn
from torch.ao.quantization import FakeQuantize,MovingAverageMinMaxObserver,MovingAveragePerChannelMinMaxObserver
from ultralytics.nn.modules.conv import Conv
class QATConv2d(nn.Conv2d):
    @classmethod
    def from_conv(cls,conv):
        obj=cls(conv.in_channels,conv.out_channels,conv.kernel_size,conv.stride,conv.padding,conv.dilation,conv.groups,conv.bias is not None,conv.padding_mode,device=conv.weight.device,dtype=conv.weight.dtype)
        obj.weight=conv.weight;obj.bias=conv.bias
        obj.weight_quant=FakeQuantize(observer=MovingAveragePerChannelMinMaxObserver,quant_min=-128,quant_max=127,dtype=torch.qint8,qscheme=torch.per_channel_symmetric,ch_axis=0).to(conv.weight.device)
        obj.input_quant=FakeQuantize(observer=MovingAverageMinMaxObserver,quant_min=0,quant_max=255,dtype=torch.quint8,qscheme=torch.per_tensor_affine).to(conv.weight.device)
        return obj
    def forward(self,x):return self._conv_forward(self.input_quant(x),self.weight_quant(self.weight),self.bias)
def fold_bn(model):
    """Fold only Conv/BN pairs; keep YOLO26 one2many and one2one heads for training."""
    model.eval();folded=[]
    for name,m in list(model.named_modules()):
        if isinstance(m,Conv) and isinstance(getattr(m,"bn",None),nn.BatchNorm2d):
            m.conv=nn.utils.fuse_conv_bn_eval(m.conv,m.bn);m.bn=nn.Identity();folded.append(name)
    return folded
def prepare(model):
    fold=fold_bn(model);selected=[];excluded=[];output_ids=set()
    head=model.model[-1]
    for branch in [head.one2many,head.one2one]:
        for towers in branch.values():
            if towers is None:continue
            for tower in towers:
                for layer in reversed(list(tower.modules())):
                    if isinstance(layer,nn.Conv2d):output_ids.add(id(layer));break
    for name,m in list(model.named_modules()):
        if not isinstance(m,nn.Conv2d) or isinstance(m,QATConv2d):continue
        # Preserve raw detection regressors/class logits at float precision.
        if id(m) in output_ids:
            excluded.append(name);continue
        parent_path,_,leaf=name.rpartition(".")
        parent=model.get_submodule(parent_path) if parent_path else model
        setattr(parent,leaf,QATConv2d.from_conv(m));selected.append(name)
    return {"backend":"pytorch_fake_quant","weights":"int8_per_output_channel_symmetric","activations":"uint8_per_tensor_affine","quantized_convs":selected,"float_output_convs":excluded,"bn_folded":fold,"float_operations":"Detection outputs, activation/non-convolution ops and attention remain float; mixed precision simulation, not a compiled all-INT8 artifact.","deployment_validation":"deferred"}
def mode(model,enabled,observe=False):
    for m in model.modules():
        if isinstance(m,FakeQuantize):
            m.enable_fake_quant(enabled);m.enable_observer(observe)
def floating_copy(model):
    model=copy.deepcopy(model)
    for name,m in list(model.named_modules()):
        if not isinstance(m,QATConv2d):continue
        conv=nn.Conv2d(m.in_channels,m.out_channels,m.kernel_size,m.stride,m.padding,m.dilation,m.groups,m.bias is not None,m.padding_mode,device=m.weight.device,dtype=m.weight.dtype)
        conv.weight=nn.Parameter(m.weight.detach().clone(),requires_grad=m.weight.requires_grad)
        if m.bias is not None:conv.bias=nn.Parameter(m.bias.detach().clone(),requires_grad=m.bias.requires_grad)
        parent,_,leaf=name.rpartition(".");setattr(model.get_submodule(parent),leaf,conv)
    return model
def observer_state(model):
    result={}
    for name,m in model.named_modules():
        if isinstance(m,FakeQuantize):
            result[name]={"scale_min":float(m.scale.min()),"scale_max":float(m.scale.max()),"zero_point_min":int(m.zero_point.min()),"zero_point_max":int(m.zero_point.max()),"observer_enabled":bool(m.observer_enabled[0]),"fake_quant_enabled":bool(m.fake_quant_enabled[0])}
    return result
