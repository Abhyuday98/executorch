# Copyright 2023 Arm Limited and/or its affiliates.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

#
# Main implementation of AoT flow to partition and preprocess for Arm target
# backends. Converts via TOSA as an intermediate form supported by AoT and
# JIT compiler flows.
#

import logging
import operator
import os
import tempfile
import subprocess
from typing import final, List

import numpy as np

import serializer.tosa_serializer as ts

import torch
from executorch.exir.backend.backend_details import BackendDetails, PreprocessResult
from executorch.exir.backend.compile_spec_schema import CompileSpec
from executorch.exir.backend.partitioner import (
    DelegationSpec,
    Partitioner,
    PartitionResult,
)

from executorch.exir.dialects._ops import ops as exir_ops
from serializer.tosa_serializer import TosaOp
from torch._export.exported_program import ExportedProgram
from torch.fx.passes.infra.partitioner import CapabilityBasedPartitioner

from torch.fx.passes.operator_support import OperatorSupportBase

from . import tosa_mapping

# TOSA backend debug functionality
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)
TOSA_DBG_VERBOSE = os.environ.get("TOSA_DBG_VERBOSE") == "1"
if TOSA_DBG_VERBOSE:
    logging.basicConfig(level=logging.INFO)
    logger.setLevel(logging.INFO)


def dbg_node(node):
    # Debug output of node information
    logger.info("OP")
    logger.info(f"  op is {node.op}")
    logger.info(f"  name is {node.name}")
    logger.info(f"  node target is {node.target}")
    logger.info(f"  node args is {node.args}")
    logger.info(f"  node kwargs is {node.kwargs}")
    logger.info("  node.meta = ")
    for k, v in node.meta.items():
        logger.info(f"    '{k}' = {v}")
        if type([]) == type(v):
            for i in v:
                logger.info(f"      {i} ")


class TOSASupportedOperators(OperatorSupportBase):
    def is_node_supported(self, submodules, node: torch.fx.Node) -> bool:
        supported = node.op == "call_function" and node.target in [
            exir_ops.edge.aten.add.Tensor,
            exir_ops.edge.aten.addmm.default,
            exir_ops.edge.aten.permute_copy.default,
            exir_ops.edge.aten.hardtanh.default,
            exir_ops.edge.aten.convolution.default,
            exir_ops.edge.aten.div.Tensor,
            exir_ops.edge.aten._native_batch_norm_legit_no_training.default,
            exir_ops.edge.aten.avg_pool2d.default,
            exir_ops.edge.aten._softmax.default,
            operator.getitem,
        ]
        return supported


def attr_torch_to_tosa(op, node):
    if TosaOp.Op().MATMUL == op:
        attr = ts.TosaSerializerAttribute()
        attr.MatMulAttribute(0, 0)
        return attr
    if TosaOp.Op().MUL == op:
        attr = ts.TosaSerializerAttribute()
        attr.MulAttribute(0)
        return attr
    return None


@final
class ArmPartitioner(Partitioner):
    compile_spec = []

    def __init__(self) -> None:
        self.delegation_spec = DelegationSpec(ArmBackend.__name__, self.compile_spec)

    def partition(self, exported_program: ExportedProgram) -> PartitionResult:
        # Run the CapabilityBasedPartitioner to return the largest possible
        # subgraphs containing the nodes with the tags
        logger.info("ArmPartitioner::partition")
        partition_tags = {}

        capability_partitioner = CapabilityBasedPartitioner(
            exported_program.graph_module,
            TOSASupportedOperators(),
            allows_single_node_partition=True,
        )
        partition_list = capability_partitioner.propose_partitions()
        for partition in partition_list:
            for node in partition.nodes:
                tag = f"tag{partition.id}"
                node.meta["delegation_tag"] = tag
                partition_tags[tag] = self.delegation_spec

        return PartitionResult(
            tagged_exported_program=exported_program, partition_tags=partition_tags
        )


# Output TOSA flatbuffer and test harness file
def dbg_tosa_dump(tosa_fb, path):
    filename = "output.tosa"

    logger.info(f"Emitting debug output to {path}")

    os.makedirs(path, exist_ok=True)

    fb = tosa_fb.serialize()
    js = tosa_fb.writeJson(filename)

    f = open(path + filename, "wb")
    f.write(fb)
    f.close()

    f = open(path + "desc.json", "w")
    f.write(js)
    f.close()

# Output to Vela with current file-based compilation
# WARNING: if this changes, the runtime reader also needs to change
def vela_compile(tosa_fb):
    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"compiling to Vela in {tmpdir}")

        tosaname = "out.tosa"
        flatbuffer = tosa_fb.serialize()
        f = open(os.path.join(tmpdir,tosaname), "wb")
        f.write(flatbuffer)
        f.close()

        # invoke vela
        # TODO target ethos-u55-128
        vela_command = f"cd {tmpdir}; vela --accelerator-config ethos-u55-128 {tosaname}"
        subprocess.run([vela_command], shell=True, check=True)

        np_path = os.path.join(tmpdir,"output","out_sg0_vela.npz")
        blocks = b''
        with np.load(np_path, allow_pickle=False) as data:
            # Emit the NPZ regions as:
            #  - 16 byte block name null terminated string (padded to 16 if name shorter)
            #  - 4 byes of int32 block length and 12 bytes of 0's
            #  - block data (padded to 16 byte alignment at end)
            # Repeat for all blocks
            for key in data.keys():
                block_name = bytes(key,"utf8")[:15]
                block_name = block_name + b'\x00'*(16-len(block_name))
                block_data = data[key].tobytes() 
                # We need the acual unpadded block lengths for hw setup
                block_length = len(block_data).to_bytes(16, 'little')
                # pad block data to multiple of 16 bytes
                block_data = block_data + b'\x00'*(16-len(block_data)%16)

                block = block_name + block_length + block_data
                blocks = blocks + block

            # Add a block for scratch, inputs and outputs
            # scratch shape is a 1 element array giving us size in bytes
            block_name = bytes("scratch_data","utf8")[:15]
            block_name = block_name + b'\x00'*(16-len(block_name))
            block_length = data["scratch_shape"][0].item()
            print(f"scratch length = {block_length}")
            block_length = block_length+(15-(block_length-1)%16)
            block_data = b'\x00'*block_length
            block_length = block_length.to_bytes(16, 'little')
            print(f"lengths {len(block_name)} {len(block_length)} {len(block_data)}")
            block = block_name + block_length + block_data
            blocks = blocks + block
            # TODO are these already in scratch shape? look to be
            #input_shape * input_elem_size
            #output_shape * output_elem_size
            # input_offset and output_offset specify the location these arrays are written from base of scratch

        # return 16 byte VELA bin header + blocks + footer
        header = bytes("vela_bin_stream","utf-8") + b'\x00'
        footer = bytes("vela_end_stream","utf-8") + b'\x00'
        return header + blocks + footer

def dbg_fail(node, tosa_fb, path):
    dbg_tosa_dump(tosa_fb, path)
    logger.warn("Internal error due to poorly handled node:")
    dbg_node(node)
    logger.warn(f"Debug output captured in '{path}'.")
    raise RuntimeError("TOSA Internal Error on node, enable logging for further info")


# Helper function to match TOSA's broadcasting rank requirement
# Ref: TOSA 0.80.0 specification - 1.9.3. Data Layouts from
# https://www.mlplatform.org/tosa/tosa_spec.html
def promote_shape(tosa_fb, arg, promoted_shape, out_dtype):
    assert np.prod(arg.shape) == np.prod(promoted_shape), "Incompatible promoted shape"
    reshape_res = tosa_fb.addIntermediate(promoted_shape, out_dtype)
    attr = ts.TosaSerializerAttribute()
    attr.ReshapeAttribute(promoted_shape)
    tosa_fb.addOperator(TosaOp.Op().RESHAPE, [arg.name], [reshape_res.name], attr)
    return reshape_res


# Helper transpose function to match TOSA's shape requirements
# E.g., TOSA 0.80.0 specification - 2.3.3 CONV2D shapes:
# https://www.mlplatform.org/tosa/tosa_spec.html#_conv2d
def transpose_helper(tosa_fb, input, new_order, out_dtype):
    # Check new_order's length is equal to input rank
    assert len(input.shape) == len(new_order), "Wrong shape order length"

    # Check no duplications
    assert len(set(new_order)) == len(new_order), "Contain duplicated dim numbers"

    # Check all dims are valid
    for idx in new_order:
        if idx < 0:
            assert True, "Negative dim number"
        elif idx >= len(input.shape):
            assert True, "Dim is greater than input rank"

    input_shape_transpoed = [input.shape[i] for i in new_order]
    attr = ts.TosaSerializerAttribute()
    attr.TransposeAttribute(new_order)
    input_transposed = tosa_fb.addIntermediate(input_shape_transpoed, out_dtype)
    tosa_fb.addOperator(
        TosaOp.Op().TRANSPOSE, [input.name], [input_transposed.name], attr
    )
    return input_transposed


@final
class ArmBackend(BackendDetails):
    @staticmethod
    def preprocess(  # noqa: C901
        edge_program: ExportedProgram,
        compile_spec: List[CompileSpec],
    ) -> PreprocessResult:
        logger.info("ArmBackend::preprocess")

        # if a debug/test build capture output files from TOSA stage
        path = None
        debug_output = False
        for spec in compile_spec:
            if spec.key == "debug_tosa_path":
                path = spec.value.decode()
                debug_output = True

        # Converted output for this subgraph, serializer needs path early as it emits
        # const data directly. Path created and data written only in debug builds.
        tosa_fb = ts.TosaSerializer(path)

        for node in edge_program.graph.nodes:
            if node.op == "call_function":
                # Unpack arguments and convert
                inputs = []
                for arg in node.args:
                    inputs.append(tosa_mapping.TosaArg(arg))

                # Convert output (this node itself)
                outp = tosa_mapping.TosaArg(node)

                # All paths have a single output
                tosa_fb.currRegion.currBasicBlock.addTensor(
                    outp.name, outp.shape, outp.dtype
                )

                op = tosa_mapping.op(node.target)
                attr = attr_torch_to_tosa(op, node)

                if op:
                    # a simple 1:1 mapping of operator taking 2 tensor arguments
                    assert len(inputs) == 2
                    assert inputs[0].dtype == outp.dtype
                    assert inputs[1].dtype == outp.dtype
                    tosa_fb.addOperator(
                        op, [inputs[0].name, inputs[1].name], [outp.name], attr
                    )
                else:
                    # A more complex mapping of operator
                    if exir_ops.edge.aten.addmm.default == node.target:
                        bias, input, weight = inputs

                        # Reshape input, weight, bias tensors
                        input_reshape_res = promote_shape(
                            tosa_fb, input, (1,) + input.shape, outp.dtype
                        )
                        weight_reshape_res = promote_shape(
                            tosa_fb, weight, (1,) + weight.shape, outp.dtype
                        )
                        bias_reshape_res = promote_shape(
                            tosa_fb,
                            bias,
                            (
                                1,
                                1,
                            )
                            + bias.shape,
                            outp.dtype,
                        )

                        # Add dummy batch 1 to mm_shape
                        mm_shape = (1, input.shape[0], weight.shape[1])
                        # Define Intermediate tensor for MatMul res
                        mm_res = tosa_fb.addIntermediate(mm_shape, outp.dtype)

                        # Add MatMulOp
                        tosa_fb.addOperator(
                            TosaOp.Op().MATMUL,
                            [input_reshape_res.name, weight_reshape_res.name],
                            [mm_res.name],
                            attr_torch_to_tosa(TosaOp.Op().MATMUL, node),
                        )

                        # Add AddOp
                        add_res = tosa_fb.addIntermediate(mm_shape, outp.dtype)
                        tosa_fb.addOperator(
                            TosaOp.Op().ADD,
                            [bias_reshape_res.name, mm_res.name],
                            [add_res.name],
                            None,
                        )

                        # Reshape final result to original shape
                        attr_out = ts.TosaSerializerAttribute()
                        attr_out.ReshapeAttribute(outp.shape)
                        tosa_fb.addOperator(
                            TosaOp.Op().RESHAPE, [add_res.name], [outp.name], attr_out
                        )
                    elif exir_ops.edge.aten.permute_copy.default == node.target:
                        attr = ts.TosaSerializerAttribute()
                        attr.TransposeAttribute(inputs[1].special)
                        tosa_fb.addOperator(
                            TosaOp.Op().TRANSPOSE, [inputs[0].name], [outp.name], attr
                        )
                    elif exir_ops.edge.aten.hardtanh.default == node.target:
                        attr = ts.TosaSerializerAttribute()
                        attr.ClampAttribute(
                            tosa_fb.builder,
                            int(inputs[1].number),
                            int(inputs[2].number),
                            inputs[1].number,
                            inputs[2].number,
                        )
                        tosa_fb.addOperator(
                            TosaOp.Op().CLAMP, [inputs[0].name], [outp.name], attr
                        )
                    elif exir_ops.edge.aten.convolution.default == node.target:
                        input, weight, bias, stride, pad, dilation, _, _, group = inputs

                        ## Transpose input tensor to NHWC_Order for TOSA
                        NHWC_Order = [0, 2, 3, 1]
                        input_transposed = transpose_helper(
                            tosa_fb, input, NHWC_Order, outp.dtype
                        )

                        ## CONV2DOp
                        attr = ts.TosaSerializerAttribute()
                        # PAD
                        pad_attr = [val for val in pad.special for _ in (0, 1)]
                        # Stride
                        stride_attr = stride.special
                        # Dilation
                        dilation_attr = dilation.special
                        attr.ConvAttribute(pad_attr, stride_attr, dilation_attr, 0, 0)

                        if group.number > 1:
                            # Transpose weight to [KH, KW, C, M]
                            weight_HWCM_Order = [2, 3, 0, 1]
                            weight_transposed = transpose_helper(
                                tosa_fb, weight, weight_HWCM_Order, outp.dtype
                            )

                            ## TOSA output shape is [N, H, W, C*M]
                            NHWO_Order = [0, 2, 3, 1]
                            out_shape_TOSA_Depthwise_CONV2D = [
                                outp.shape[i] for i in NHWO_Order
                            ]

                            conv2d_res = tosa_fb.addIntermediate(
                                out_shape_TOSA_Depthwise_CONV2D, outp.dtype
                            )
                            tosa_fb.addOperator(
                                TosaOp.Op().DEPTHWISE_CONV2D,
                                [
                                    input_transposed.name,
                                    weight_transposed.name,
                                    bias.name,
                                ],
                                [conv2d_res.name],
                                attr,
                            )
                        else:
                            # TODO: Transpose the weight AoT
                            # Transpose weight to [OC, H, W, IC]
                            weight_CHWC_Order = [0, 2, 3, 1]
                            weight_transposed = transpose_helper(
                                tosa_fb, weight, weight_CHWC_Order, outp.dtype
                            )

                            ## TOSA output shape is [NHWO]
                            NHWO_Order = [0, 2, 3, 1]
                            out_shape_TOSA_CONV2D = [outp.shape[i] for i in NHWO_Order]
                            conv2d_res = tosa_fb.addIntermediate(
                                out_shape_TOSA_CONV2D, outp.dtype
                            )
                            tosa_fb.addOperator(
                                TosaOp.Op().CONV2D,
                                [
                                    input_transposed.name,
                                    weight_transposed.name,
                                    bias.name,
                                ],
                                [conv2d_res.name],
                                attr,
                            )

                        ## Torch output shape is [NOHW]
                        NOHW_Order = [0, 3, 1, 2]
                        attr_output_transpose = ts.TosaSerializerAttribute()
                        attr_output_transpose.TransposeAttribute(NOHW_Order)
                        tosa_fb.addOperator(
                            TosaOp.Op().TRANSPOSE,
                            [conv2d_res.name],
                            [outp.name],
                            attr_output_transpose,
                        )
                    elif exir_ops.edge.aten.div.Tensor == node.target:
                        # Div is implemented as x/y = x*1/y
                        recip = tosa_fb.addIntermediate(
                            inputs[1].shape, inputs[1].dtype
                        )
                        tosa_fb.addOperator(
                            TosaOp.Op().RECIPROCAL, [inputs[1].name], [recip.name]
                        )

                        attr = ts.TosaSerializerAttribute()
                        attr.MulAttribute(0)
                        tosa_fb.addOperator(
                            TosaOp.Op().MUL,
                            [inputs[0].name, recip.name],
                            [outp.name],
                            attr,
                        )
                    elif (
                        exir_ops.edge.aten._native_batch_norm_legit_no_training.default
                        == node.target
                    ):
                        # Decompose batch norm into sequence
                        (
                            activations,
                            _,
                            _,
                            running_mean,
                            running_var,
                            momentum,
                            epsilon,
                        ) = inputs

                        input_dtype = activations.dtype
                        input_shape = activations.shape

                        assert (
                            0.1 == momentum.number
                        ), "Expected 0.1 momentum, not currently encoded into TOSA"

                        # %op1 = tosa.SUB(%x, %bmean)
                        # %op2 = tosa.ADD(%variance, %epsilon_const)
                        # %op3 = tosa.RSQRT(%op2)
                        # %op4 = tosa.MUL(%op1, %op3)
                        # %op5 = tosa.MUL(%op4, %weight)
                        # %output = tosa.ADD(%op5, %bias)

                        # Reshape mean to match rank of activations
                        mean_reshaped_res = promote_shape(
                            tosa_fb,
                            running_mean,
                            (1,)
                            + running_mean.shape
                            + (
                                1,
                                1,
                            ),
                            input_dtype,
                        )

                        # Subtract mean
                        int1 = tosa_fb.addIntermediate(input_shape, input_dtype)
                        tosa_fb.addOperator(
                            TosaOp.Op().SUB,
                            [activations.name, mean_reshaped_res.name],
                            [int1.name],
                        )
                        # Adding eplison to variance
                        epsilon_const = tosa_fb.addConst(
                            [1], input_dtype, [epsilon.number]
                        )
                        int2 = tosa_fb.addIntermediate(running_var.shape, input_dtype)
                        tosa_fb.addOperator(
                            TosaOp.Op().ADD,
                            [running_var.name, epsilon_const.name],
                            [int2.name],
                        )
                        # Push downward the variance
                        int3 = tosa_fb.addIntermediate(running_var.shape, input_dtype)
                        tosa_fb.addOperator(TosaOp.Op().RSQRT, [int2.name], [int3.name])

                        # Reshape variable to match rank of activations
                        var_reshaped_res = promote_shape(
                            tosa_fb,
                            int3,
                            (1,)
                            + running_var.shape
                            + (
                                1,
                                1,
                            ),
                            input_dtype,
                        )

                        # Multiple shifted activations with reciprocal variance
                        # int4 = tosa_fb.addIntermediate( input_shape, input_dtype )
                        tosa_fb.addOperator(
                            TosaOp.Op().MUL,
                            [int1.name, var_reshaped_res.name],
                            [outp.name],
                            attr_torch_to_tosa(TosaOp.Op().MUL, node),
                        )
                    elif exir_ops.edge.aten.avg_pool2d.default == node.target:
                        input_tensor = inputs[0]
                        kernel_size_list = inputs[1].special
                        stride_size_list = inputs[2].special
                        try:
                            pad_size_list = inputs[3].special
                        except IndexError:
                            pad_size_list = [0, 0, 0, 0]

                        attr = ts.TosaSerializerAttribute()
                        attr.PoolAttribute(
                            kernel=kernel_size_list,
                            stride=stride_size_list,
                            pad=pad_size_list,
                            input_zp=0,
                            output_zp=0,
                            accum_dtype=8,
                        )  # FP32 accum type

                        # Torch's input is [N,C,H,W], TOSA is [N, H, W, C],
                        # Transpose to align with TOSA
                        NHWC_Order = [0, 2, 3, 1]
                        input_transposed = transpose_helper(
                            tosa_fb, input_tensor, NHWC_Order, outp.dtype
                        )

                        avg_pool2d_res_shape = [outp.shape[i] for i in NHWC_Order]
                        avg_pool2d_res = tosa_fb.addIntermediate(
                            avg_pool2d_res_shape, outp.dtype
                        )
                        tosa_fb.addOperator(
                            TosaOp.Op().AVG_POOL2D,
                            [input_transposed.name],
                            [avg_pool2d_res.name],
                            attr,
                        )

                        # TOSA is [N, H, W, C], Transpose back to Torch's [N, C, H, W]
                        NCHW_Order = [0, 3, 1, 2]
                        attr_output_transpose = ts.TosaSerializerAttribute()
                        attr_output_transpose.TransposeAttribute(NCHW_Order)
                        tosa_fb.addOperator(
                            TosaOp.Op().TRANSPOSE,
                            [avg_pool2d_res.name],
                            [outp.name],
                            attr_output_transpose,
                        )
                    elif exir_ops.edge.aten._softmax.default == node.target:
                        input_name = inputs[0].name
                        input_shape = inputs[0].shape
                        dim_value = inputs[1].number

                        ## softmax = exp(logits - max(logits)) / reduce_sum(exp(logits - max(logits)), -1)
                        # FP32
                        # reduce_max_res = reducemax(logits)
                        # sub_res = sub(inputs, reduce_max_res)
                        # exp_res = exp(sub_res)
                        # reduce_sum_res = reduce_sum(exp_res, -1)
                        # inverted_reduce_sum = reciprocal(reduce_sum_res)
                        # output = mul(exp_res, inverted_reduce_sum)

                        # Max_Reduction
                        attr_axis = ts.TosaSerializerAttribute()
                        attr_axis.AxisAttribute(axis=dim_value)
                        reduced_shape = list(input_shape)
                        reduced_shape[dim_value] = 1
                        reduce_max_res = tosa_fb.addIntermediate(
                            reduced_shape, outp.dtype
                        )
                        tosa_fb.addOperator(
                            TosaOp.Op().REDUCE_MAX,
                            [input_name],
                            [reduce_max_res.name],
                            attr_axis,
                        )

                        # Subtract max from logits
                        sub_res = tosa_fb.addIntermediate(input_shape, outp.dtype)
                        tosa_fb.addOperator(
                            TosaOp.Op().SUB,
                            [input_name, reduce_max_res.name],
                            [sub_res.name],
                        )

                        # Raise the subtraction results to exponent
                        exp_res = tosa_fb.addIntermediate(input_shape, outp.dtype)
                        tosa_fb.addOperator(
                            TosaOp.Op().EXP, [sub_res.name], [exp_res.name]
                        )

                        # Reduce_sum of the calculated exponent value
                        reduce_sum_res = tosa_fb.addIntermediate(
                            reduced_shape, outp.dtype
                        )
                        tosa_fb.addOperator(
                            TosaOp.Op().REDUCE_SUM,
                            [exp_res.name],
                            [reduce_sum_res.name],
                            attr_axis,
                        )

                        # Invert the reduce_sum
                        inverted_reduce_sum = tosa_fb.addIntermediate(
                            reduced_shape, outp.dtype
                        )
                        tosa_fb.addOperator(
                            TosaOp.Op().RECIPROCAL,
                            [reduce_sum_res.name],
                            [inverted_reduce_sum.name],
                        )

                        # Multiply two parts to get the final results
                        attr_mul = ts.TosaSerializerAttribute()
                        attr_mul.MulAttribute(0)
                        tosa_fb.addOperator(
                            TosaOp.Op().MUL,
                            [exp_res.name, inverted_reduce_sum.name],
                            [outp.name],
                            attr_mul,
                        )
                    elif operator.getitem == node.target:
                        item_name = inputs[0].name
                        ## Simply add an identityOp
                        tosa_fb.addOperator(
                            TosaOp.Op().IDENTITY, [item_name], [outp.name]
                        )
                    else:
                        raise RuntimeError(f"Unknown operator {node.target}")

                continue

            elif node.op == "placeholder":
                assert (
                    node.name == node.target
                ), "Expect placeholder name and target to match"
                assert 0 == len(node.args), "Can't handle default input values"

                # TODO: this may fail on int64 constant input
                inputs = [tosa_mapping.TosaArg(node)]
                out = node.name

                if out in edge_program.graph_signature.inputs_to_parameters:
                    parameter_name = edge_program.graph_signature.inputs_to_parameters[
                        node.name
                    ]
                    p_data = edge_program.state_dict[parameter_name]

                    assert isinstance(p_data, torch.Tensor), "Expect Attr to be tensor"
                    weight_values = p_data.detach().numpy()
                    tosa_fb.addConst(
                        inputs[0].shape, inputs[0].dtype, weight_values, name=out
                    )
                elif out in edge_program.graph_signature.inputs_to_buffers:
                    parameter_name = edge_program.graph_signature.inputs_to_buffers[
                        node.name
                    ]
                    p_data = edge_program.state_dict[parameter_name]

                    assert isinstance(p_data, torch.Tensor), "Expect Attr to be tensor"
                    weight_values = p_data.detach().numpy()
                    tosa_fb.addConst(
                        inputs[0].shape, inputs[0].dtype, weight_values, name=out
                    )
                else:
                    # Input argument
                    tensor = ts.TosaSerializerTensor(
                        inputs[0].name,
                        inputs[0].shape,
                        inputs[0].dtype,
                        data=None,
                        placeholderFilename=inputs[0].name + ".npy",
                    )
                    tosa_fb.addInputTensor(tensor)
                continue

            elif node.op == "output":
                for output in node.args[0]:
                    tosa_fb.addOutputTensor(
                        tosa_fb.currRegion.currBasicBlock.tensors[output.name]
                    )
                continue

            else:
                # This will only happen if an unpartitioned graph is passed without
                # any checking of compatibility.
                dbg_fail(node, tosa_fb, path)

        if debug_output is True:
            dbg_tosa_dump(tosa_fb, path)

        # Serialize and return the tosa flatbuffer
        # fb = bytes(tosa_fb.serialize())
        binary = vela_compile(tosa_fb)
        
        return PreprocessResult(processed_bytes=binary)
