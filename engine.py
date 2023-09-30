import os
import pickle
from collections import defaultdict, namedtuple
from pathlib import Path
from typing import List, Optional, Tuple, Union

import onnx
import tensorrt as trt
import torch

os.environ['CUDA_MODULE_LOADING'] = 'LAZY'


class EngineBuilder:
    seg = False

    def __init__(
            self,
            checkpoint: Union[str, Path],
            device: Optional[Union[str, int, torch.device]] = None) -> None:
        checkpoint = Path(checkpoint) if isinstance(checkpoint,
                                                    str) else checkpoint
        assert checkpoint.exists() and checkpoint.suffix in ('.onnx', '.pkl')
        self.api = checkpoint.suffix == '.pkl'
        if isinstance(device, str):
            device = torch.device(device)
        elif isinstance(device, int):
            device = torch.device(f'cuda:{device}')

        self.checkpoint = checkpoint
        self.device = device

    def __build_engine(self,
                       fp16: bool = True,
                       input_shape: Union[List, Tuple] = (1, 3, 640, 640),
                       iou_thres: float = 0.65,
                       conf_thres: float = 0.25,
                       topk: int = 100,
                       with_profiling: bool = True) -> None:
        logger = trt.Logger(trt.Logger.WARNING)
        trt.init_libnvinfer_plugins(logger, namespace='')
        builder = trt.Builder(logger)
        config = builder.create_builder_config()
        config.max_workspace_size = torch.cuda.get_device_properties(
            self.device).total_memory
        flag = (1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
        network = builder.create_network(flag)

        self.logger = logger
        self.builder = builder
        self.network = network
        if self.api:
            self.build_from_api(fp16, input_shape, iou_thres, conf_thres, topk)
        else:
            self.build_from_onnx(iou_thres, conf_thres, topk)
        if fp16 and self.builder.platform_has_fast_fp16:
            config.set_flag(trt.BuilderFlag.FP16)
        self.weight = self.checkpoint.with_suffix('.engine')

        if with_profiling:
            config.profiling_verbosity = trt.ProfilingVerbosity.DETAILED
        with self.builder.build_engine(self.network, config) as engine:
            self.weight.write_bytes(engine.serialize())
        self.logger.log(
            trt.Logger.WARNING, f'Build tensorrt engine finish.\n'
            f'Save in {str(self.weight.absolute())}')

    def build(self,
              fp16: bool = True,
              input_shape: Union[List, Tuple] = (1, 3, 640, 640),
              iou_thres: float = 0.65,
              conf_thres: float = 0.25,
              topk: int = 100,
              with_profiling=True) -> None:
        self.__build_engine(fp16, input_shape, iou_thres, conf_thres, topk,
                            with_profiling)

    def build_from_onnx(self,
                        iou_thres: float = 0.65,
                        conf_thres: float = 0.25,
                        topk: int = 100):
        parser = trt.OnnxParser(self.network, self.logger)
        onnx_model = onnx.load(str(self.checkpoint))
        if not self.seg:
            onnx_model.graph.node[-1].attribute[2].i = topk
            onnx_model.graph.node[-1].attribute[3].f = conf_thres
            onnx_model.graph.node[-1].attribute[4].f = iou_thres

        if not parser.parse(onnx_model.SerializeToString()):
            raise RuntimeError(
                f'failed to load ONNX file: {str(self.checkpoint)}')
        inputs = [
            self.network.get_input(i) for i in range(self.network.num_inputs)
        ]
        outputs = [
            self.network.get_output(i) for i in range(self.network.num_outputs)
        ]

        for inp in inputs:
            self.logger.log(
                trt.Logger.WARNING,
                f'input "{inp.name}" with shape: {inp.shape} '
                f'dtype: {inp.dtype}')
        for out in outputs:
            self.logger.log(
                trt.Logger.WARNING,
                f'output "{out.name}" with shape: {out.shape} '
                f'dtype: {out.dtype}')

    def build_from_api(
        self,
        fp16: bool = True,
        input_shape: Union[List, Tuple] = (1, 3, 640, 640),
        iou_thres: float = 0.65,
        conf_thres: float = 0.25,
        topk: int = 100,
    ):
        assert not self.seg
        from .api import SPPF, C2f, Conv, Detect, get_depth, get_width

        with open(self.checkpoint, 'rb') as f:
            state_dict = pickle.load(f)
        mapping = {0.25: 1024, 0.5: 1024, 0.75: 768, 1.0: 512, 1.25: 512}

        GW = state_dict['GW']
        GD = state_dict['GD']
        width_64 = get_width(64, GW)
        width_128 = get_width(128, GW)
        width_256 = get_width(256, GW)
        width_512 = get_width(512, GW)
        width_1024 = get_width(mapping[GW], GW)
        depth_3 = get_depth(3, GD)
        depth_6 = get_depth(6, GD)
        strides = state_dict['strides']
        reg_max = state_dict['reg_max']
        images = self.network.add_input(name='images',
                                        dtype=trt.float32,
                                        shape=trt.Dims4(input_shape))
        assert images, 'Add input failed'

        Conv_0 = Conv(self.network, state_dict, images, width_64, 3, 2, 1,
                      'Conv.0')
        Conv_1 = Conv(self.network, state_dict, Conv_0.get_output(0),
                      width_128, 3, 2, 1, 'Conv.1')
        C2f_2 = C2f(self.network, state_dict, Conv_1.get_output(0), width_128,
                    depth_3, True, 1, 0.5, 'C2f.2')
        Conv_3 = Conv(self.network, state_dict, C2f_2.get_output(0), width_256,
                      3, 2, 1, 'Conv.3')
        C2f_4 = C2f(self.network, state_dict, Conv_3.get_output(0), width_256,
                    depth_6, True, 1, 0.5, 'C2f.4')
        Conv_5 = Conv(self.network, state_dict, C2f_4.get_output(0), width_512,
                      3, 2, 1, 'Conv.5')
        C2f_6 = C2f(self.network, state_dict, Conv_5.get_output(0), width_512,
                    depth_6, True, 1, 0.5, 'C2f.6')
        Conv_7 = Conv(self.network, state_dict, C2f_6.get_output(0),
                      width_1024, 3, 2, 1, 'Conv.7')
        C2f_8 = C2f(self.network, state_dict, Conv_7.get_output(0), width_1024,
                    depth_3, True, 1, 0.5, 'C2f.8')
        SPPF_9 = SPPF(self.network, state_dict, C2f_8.get_output(0),
                      width_1024, width_1024, 5, 'SPPF.9')
        Upsample_10 = self.network.add_resize(SPPF_9.get_output(0))
        assert Upsample_10, 'Add Upsample_10 failed'
        Upsample_10.resize_mode = trt.ResizeMode.NEAREST
        Upsample_10.shape = Upsample_10.get_output(
            0).shape[:2] + C2f_6.get_output(0).shape[2:]
        input_tensors11 = [Upsample_10.get_output(0), C2f_6.get_output(0)]
        Cat_11 = self.network.add_concatenation(input_tensors11)
        C2f_12 = C2f(self.network, state_dict, Cat_11.get_output(0), width_512,
                     depth_3, False, 1, 0.5, 'C2f.12')
        Upsample13 = self.network.add_resize(C2f_12.get_output(0))
        assert Upsample13, 'Add Upsample13 failed'
        Upsample13.resize_mode = trt.ResizeMode.NEAREST
        Upsample13.shape = Upsample13.get_output(
            0).shape[:2] + C2f_4.get_output(0).shape[2:]
        input_tensors14 = [Upsample13.get_output(0), C2f_4.get_output(0)]
        Cat_14 = self.network.add_concatenation(input_tensors14)
        C2f_15 = C2f(self.network, state_dict, Cat_14.get_output(0), width_256,
                     depth_3, False, 1, 0.5, 'C2f.15')
        Conv_16 = Conv(self.network, state_dict, C2f_15.get_output(0),
                       width_256, 3, 2, 1, 'Conv.16')
        input_tensors17 = [Conv_16.get_output(0), C2f_12.get_output(0)]
        Cat_17 = self.network.add_concatenation(input_tensors17)
        C2f_18 = C2f(self.network, state_dict, Cat_17.get_output(0), width_512,
                     depth_3, False, 1, 0.5, 'C2f.18')
        Conv_19 = Conv(self.network, state_dict, C2f_18.get_output(0),
                       width_512, 3, 2, 1, 'Conv.19')
        input_tensors20 = [Conv_19.get_output(0), SPPF_9.get_output(0)]
        Cat_20 = self.network.add_concatenation(input_tensors20)
        C2f_21 = C2f(self.network, state_dict, Cat_20.get_output(0),
                     width_1024, depth_3, False, 1, 0.5, 'C2f.21')
        input_tensors22 = [
            C2f_15.get_output(0),
            C2f_18.get_output(0),
            C2f_21.get_output(0)
        ]
        batched_nms = Detect(self.network, state_dict, input_tensors22,
                             strides, 'Detect.22', reg_max, fp16, iou_thres,
                             conf_thres, topk)
        for o in range(batched_nms.num_outputs):
            self.network.mark_output(batched_nms.get_output(o))


