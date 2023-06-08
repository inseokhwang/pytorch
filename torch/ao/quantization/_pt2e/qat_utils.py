import copy
import itertools
import operator
from typing import Any, Callable, List, Tuple

import torch
from torch.fx import Graph, GraphModule, Node
from torch.fx.subgraph_rewriter import replace_pattern_with_filters
import torch.nn.functional as F
from torch.ao.quantization.fx._decomposed import quantized_decomposed_lib  # noqa: F401
from .utils import _fold_bn_weights_into_conv_node

# Example inputs for `_conv2d_bn_pattern`, `_qat_conv2d_bn_pattern`, and `_qat_conv2d_bn_pattern_no_bias`
_conv2d_bn_pattern_example_inputs = (
    torch.randn(1, 1, 3, 3),  # x
    torch.randn(1, 1, 1, 1),  # conv_weight
    torch.randn(1),           # conv_bias
    torch.randn(1),           # bn_weight
    torch.randn(1),           # bn_bias
    torch.randn(1),           # bn_running_mean
    torch.randn(1),           # bn_running_var
)

# Example inputs for both `_quantized_qat_conv2d_bn_pattern` and `_folded_quantized_qat_conv2d_bn_pattern`
_quantized_conv2d_bn_pattern_example_inputs = (
    torch.randn(1, 1, 3, 3).to(torch.int8),  # x
    torch.randn(1, 1, 1, 1),  # conv_weight
    torch.randn(1),           # conv_bias
    torch.randn(1),           # bn_weight
    torch.randn(1),           # bn_bias
    torch.randn(1),           # bn_running_mean
    torch.randn(1),           # bn_running_var
    torch.tensor([1], dtype=torch.float),  # input_scale
    torch.tensor([0], dtype=torch.int),    # input_zero_point
    torch.tensor([1], dtype=torch.float),  # weight_scale
    torch.tensor([0], dtype=torch.int),    # weight_zero_point
    torch.tensor([1], dtype=torch.float),  # output_scale
    torch.tensor([0], dtype=torch.int),    # output_zero_point
)

def _conv2d_bn_pattern(
    x: torch.Tensor,
    conv_weight: torch.Tensor,
    conv_bias: torch.Tensor,
    bn_weight: torch.Tensor,
    bn_bias: torch.Tensor,
    bn_running_mean: torch.Tensor,
    bn_running_var: torch.Tensor,
) -> torch.Tensor:
    x = F.conv2d(x, conv_weight, conv_bias)
    x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True)
    return x

# TODO: merge this with the `no_conv_bias` case
def _qat_conv2d_bn_pattern(
    x: torch.Tensor,
    conv_weight: torch.Tensor,
    conv_bias: torch.Tensor,
    bn_weight: torch.Tensor,
    bn_bias: torch.Tensor,
    bn_running_mean: torch.Tensor,
    bn_running_var: torch.Tensor,
) -> torch.Tensor:
    """
    Approximated method to fuse conv and bn. It requires only one forward pass.
    conv_orig = conv / scale_factor where scale_factor = bn.weight / running_std.
    This is based on `nniqat.ConvBn2d._forward_approximate`.
    """
    # TODO: allow setting eps
    bn_eps = 1e-5
    running_std = torch.sqrt(bn_running_var + bn_eps)
    scale_factor = bn_weight / running_std
    weight_shape = [1] * len(conv_weight.shape)
    weight_shape[0] = -1
    bias_shape = [1] * len(conv_weight.shape)
    bias_shape[1] = -1
    scaled_weight = conv_weight * scale_factor.reshape(weight_shape)
    zero_bias = torch.zeros_like(conv_bias, dtype=x.dtype)
    x = F.conv2d(x, scaled_weight, zero_bias)
    x = x / scale_factor.reshape(bias_shape)
    x = x + conv_bias.reshape(bias_shape)
    x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True, eps=bn_eps)
    return x

def _qat_conv2d_bn_pattern_no_conv_bias(
    x: torch.Tensor,
    conv_weight: torch.Tensor,
    # Not used, only for matching convenience
    conv_bias: torch.Tensor,
    bn_weight: torch.Tensor,
    bn_bias: torch.Tensor,
    bn_running_mean: torch.Tensor,
    bn_running_var: torch.Tensor,
) -> torch.Tensor:
    """
    Same as `_qat_conv2d_bn_pattern`, but handles the case with no conv bias.
    """
    # TODO: allow setting eps
    bn_eps = 1e-5
    running_std = torch.sqrt(bn_running_var + bn_eps)
    scale_factor = bn_weight / running_std
    weight_shape = [1] * len(conv_weight.shape)
    weight_shape[0] = -1
    bias_shape = [1] * len(conv_weight.shape)
    bias_shape[1] = -1
    scaled_weight = conv_weight * scale_factor.reshape(weight_shape)
    x = F.conv2d(x, scaled_weight, None)
    x = x / scale_factor.reshape(bias_shape)
    x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True, eps=bn_eps)
    return x

def _get_quantized_qat_conv2d_bn_pattern(
    is_per_channel: bool,
    has_relu: bool,
    has_bias: bool,
    relu_is_inplace: bool,
) -> Callable:
    """
    Return the quantized version of QAT conv + BN pattern.
    This is based on `nniqat.ConvBn2d._forward_approximate`,
    used in QAT convert. We first match this pattern and replace
    it with the normal [conv - bn] pattern, then fold the BN
    weights into conv.
    """
    # TODO: allow setting eps
    bn_eps = 1e-5
    weight_quant_min = -127
    weight_quant_max = 127
    input_quant_min = -128
    input_quant_max = 127
    output_quant_min = -128
    output_quant_max = 127
    per_channel_axis = 0

    def _quantized_qat_conv2d_bn_pattern(
        x: torch.Tensor,
        conv_weight: torch.Tensor,
        conv_bias: torch.Tensor,
        bn_weight: torch.Tensor,
        bn_bias: torch.Tensor,
        bn_running_mean: torch.Tensor,
        bn_running_var: torch.Tensor,
        input_scale: torch.Tensor,
        input_zero_point: torch.Tensor,
        weight_scale: torch.Tensor,
        weight_zero_point: torch.Tensor,
        output_scale: torch.Tensor,
        output_zero_point: torch.Tensor,
    ) -> torch.Tensor:
        running_std = torch.sqrt(bn_running_var + bn_eps)
        scale_factor = bn_weight / running_std
        weight_shape = [1] * len(conv_weight.shape)
        weight_shape[0] = -1
        bias_shape = [1] * len(conv_weight.shape)
        bias_shape[1] = -1
        scaled_weight = conv_weight * scale_factor.reshape(weight_shape)
        x = torch.ops.quantized_decomposed.dequantize_per_tensor(
            x, input_scale, input_zero_point, input_quant_min, input_quant_max, torch.int8)
        if is_per_channel:
            scaled_weight = torch.ops.quantized_decomposed.quantize_per_channel(
                scaled_weight, weight_scale, weight_zero_point, per_channel_axis,
                weight_quant_min, weight_quant_max, torch.int8,
            )
            scaled_weight = torch.ops.quantized_decomposed.dequantize_per_channel(
                scaled_weight, weight_scale, weight_zero_point, per_channel_axis,
                weight_quant_min, weight_quant_max, torch.int8,
            )
        else:
            scaled_weight = torch.ops.quantized_decomposed.quantize_per_tensor(
                scaled_weight, weight_scale, weight_zero_point, weight_quant_min, weight_quant_max, torch.int8,
            )
            scaled_weight = torch.ops.quantized_decomposed.dequantize_per_tensor(
                scaled_weight, weight_scale, weight_zero_point, weight_quant_min, weight_quant_max, torch.int8,
            )
        if has_bias:
            zero_bias = torch.zeros_like(conv_bias, dtype=x.dtype)
            x = F.conv2d(x, scaled_weight, zero_bias)
        else:
            x = F.conv2d(x, scaled_weight, None)
        x = x / scale_factor.reshape(bias_shape)
        if has_bias:
            x = x + conv_bias.reshape(bias_shape)
        x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True, eps=bn_eps)
        if has_relu:
            if relu_is_inplace:
                x = F.relu_(x)
            else:
                x = F.relu(x)
        x = torch.ops.quantized_decomposed.quantize_per_tensor(
            x, output_scale, output_zero_point, output_quant_min, output_quant_max, torch.int8)
        return x
    return _quantized_qat_conv2d_bn_pattern

def _get_folded_quantized_qat_conv2d_bn_pattern(
    is_per_channel: bool,
    has_relu: bool,
    has_bias: bool,
    relu_is_inplace: bool,
) -> Callable:
    """
    Quantized QAT conv - bn pattern with bn weights being folded into conv.
    """
    # TODO: allow setting eps
    bn_eps = 1e-5
    weight_quant_min = -127
    weight_quant_max = 127
    input_quant_min = -128
    input_quant_max = 127
    output_quant_min = -128
    output_quant_max = 127
    per_channel_axis = 0

    def _folded_quantized_qat_conv2d_bn_pattern(
        x: torch.Tensor,
        conv_weight: torch.Tensor,
        conv_bias: torch.Tensor,
        bn_weight: torch.Tensor,
        bn_bias: torch.Tensor,
        bn_running_mean: torch.Tensor,
        bn_running_var: torch.Tensor,
        input_scale: torch.Tensor,
        input_zero_point: torch.Tensor,
        weight_scale: torch.Tensor,
        weight_zero_point: torch.Tensor,
        output_scale: torch.Tensor,
        output_zero_point: torch.Tensor,
    ) -> torch.Tensor:
        x = torch.ops.quantized_decomposed.dequantize_per_tensor(
            x, input_scale, input_zero_point, input_quant_min, input_quant_max, torch.int8)
        if is_per_channel:
            conv_weight = torch.ops.quantized_decomposed.quantize_per_channel(
                conv_weight, weight_scale, weight_zero_point, per_channel_axis,
                weight_quant_min, weight_quant_max, torch.int8,
            )
            conv_weight = torch.ops.quantized_decomposed.dequantize_per_channel(
                conv_weight, weight_scale, weight_zero_point, per_channel_axis,
                weight_quant_min, weight_quant_max, torch.int8,
            )
        else:
            conv_weight = torch.ops.quantized_decomposed.quantize_per_tensor(
                conv_weight, weight_scale, weight_zero_point, weight_quant_min, weight_quant_max, torch.int8,
            )
            conv_weight = torch.ops.quantized_decomposed.dequantize_per_tensor(
                conv_weight, weight_scale, weight_zero_point, weight_quant_min, weight_quant_max, torch.int8,
            )
        if has_bias:
            x = F.conv2d(x, conv_weight, conv_bias)
        else:
            x = F.conv2d(x, conv_weight, None)
        x = F.batch_norm(x, bn_running_mean, bn_running_var, bn_weight, bn_bias, training=True, eps=bn_eps)
        if has_relu:
            if relu_is_inplace:
                x = F.relu_(x)
            else:
                x = F.relu(x)
        x = torch.ops.quantized_decomposed.quantize_per_tensor(
            x, output_scale, output_zero_point, output_quant_min, output_quant_max, torch.int8)
        return x
    return _folded_quantized_qat_conv2d_bn_pattern

def _get_aten_graph_module(
    pattern: Callable,
    example_inputs: Tuple[Any, ...],
) -> GraphModule:
    """
    Convert the pattern to an FX graph with decomposed aten ops.
    """
    # Avoid circular imports
    import torch._dynamo
    aten_pattern, _ = torch._dynamo.export(
        pattern,
        *copy.deepcopy(example_inputs),
        aten_graph=True,
        tracing_mode="real",
    )
    aten_pattern.graph.eliminate_dead_code()
    aten_pattern.recompile()
    return aten_pattern

def _has_conv_bias_filter(
    match: "InternalMatch",  # type: ignore[name-defined]
    original_graph: Graph,
    pattern_graph: Graph,
) -> bool:
    """
    Match filter for the subgraph rewriter that returns True if the conv node in
    the original graph has bias.
    """
    for _, n in match.nodes_map.items():
        if n.target == torch.ops.aten.convolution.default:
            return n.args[2] is not None
    raise ValueError("Could not find conv node in matched conv + bn pattern")

def _no_conv_bias_filter(
    match: "InternalMatch",  # type: ignore[name-defined]
    original_graph: Graph,
    pattern_graph: Graph,
) -> bool:
    """
    Match filter for the subgraph rewriter that returns True if the conv node in
    the original graph does NOT have bias.
    """
    return not _has_conv_bias_filter(match, original_graph, pattern_graph)

def _get_conv_bn_getitem_nodes(nodes: List[Node]) -> Tuple[Node, Node, Node]:
    """
    Helper function to extract the conv, bn, and getitem nodes from the list.
    This asserts that the list contains exactly one of each of the above nodes.

    Return a 3-tuple of (conv node, bn node, getitem node).
    """
    conv_node, bn_node, getitem_node = None, None, None
    for n in nodes:
        if n.op != "call_function":
            continue
        if n.target == torch.ops.aten.convolution.default:
            assert conv_node is None
            conv_node = n
        elif n.target == torch.ops.aten._native_batch_norm_legit.default:
            assert bn_node is None
            bn_node = n
        elif n.target == operator.getitem:
            assert getitem_node is None
            getitem_node = n
    assert conv_node is not None
    assert bn_node is not None
    assert getitem_node is not None
    return (conv_node, bn_node, getitem_node)

def _node_is_input_to_matched_pattern(original_node: Node) -> bool:
    """
    Return whether the node is an input to the matched pattern in the original graph.

    We expect the subgraph rewriter to erase the non-literal args of the matched nodes.
    However, this is not done for nodes that are inputs to the matched pattern, since
    these nodes do not need to be replaced. We wish to filter out these input nodes
    since they do not need metadata copying. For example, in the following pattern:

      getitem_input -> conv -> bn -> getitem

    The args of the above nodes should look like:

      getitem_input(some_node, 0)
      conv(None, None, None, ...)         # args: input, weight, bias
      bn(None, None, None, ...)           # args: input, weight, bias
      getitem(None, 0)

    In this example, this function will return True only for `getitem_input`.
    """
    return any(isinstance(a, Node) for a in original_node.args)

def _copy_over_literal_conv_args(original_node: Node, new_node: Node):
    """
    Copy over literal args in conv, such as stride and padding, from the matched node
    in the original graph to its replacement in the new graph.

    This is needed due to the following limitation in the subgraph rewriter when used
    with dynamo export: literal (non-tensor) args are not supported in the match and
    replacement patterns. This is because dynamo export automatically inlines these
    literal args, making them dead placeholder nodes. In the future, we should check
    if dynamo export can optionally disable this inlining, or if subgraph rewriter
    can do the copying for us. See https://github.com/pytorch/pytorch/issues/100419.

    Note: Unlike other tensor args like conv weights and biases, literal args are
    preserved in the original nodes after replacement, so we can access them here.
    """
    assert original_node.target == torch.ops.aten.convolution.default
    assert new_node.target == torch.ops.aten.convolution.default
    # x, weight, bias, [stride, padding, dilation, transposed, output_padding, groups]
    new_node.args = new_node.args[:3] + original_node.args[3:]

def _fuse_conv_bn_qat(m: GraphModule) -> GraphModule:
    """
    Given a graph of decomposed aten ops, replace the (conv + bn) pattern with
    the fused QAT subgraph equivalent. The input graph should already be annotated.
    The annotations in the original nodes will be preserved in the corresponding
    nodes in the new subgraph.

    Note: This also handles the (conv + bn + relu) pattern.
    """
    m.graph.eliminate_dead_code()
    m.recompile()
    example_inputs = _conv2d_bn_pattern_example_inputs
    match_pattern = _get_aten_graph_module(_conv2d_bn_pattern, example_inputs)

    # Step (1): Replace patterns with conv bias
    #
    # Here we do replacement separately for cases with and without conv bias, since
    # the replacement patterns for these two cases are substantially different.
    # TODO: use the public replace_pattern API once it also returns replacement nodes

    replacement_pattern_with_conv_bias = _get_aten_graph_module(
        _qat_conv2d_bn_pattern,
        example_inputs,
    )
    replacements_with_conv_bias = replace_pattern_with_filters(
        m,
        match_pattern,
        replacement_pattern_with_conv_bias,
        match_filters=[_has_conv_bias_filter],
        ignore_literals=True,
    )
    m.recompile()

    # Step (2): Replace patterns without conv bias

    replacement_pattern_no_conv_bias = _get_aten_graph_module(
        _qat_conv2d_bn_pattern_no_conv_bias,
        example_inputs,
    )
    replacements_no_conv_bias = replace_pattern_with_filters(
        m,
        match_pattern,
        replacement_pattern_no_conv_bias,
        match_filters=[_no_conv_bias_filter],
        ignore_literals=True,
    )
    m.recompile()

    # Step (3): Post processing
    #
    # Due to limited functionality in the subgraph rewriter, here we manually
    # update the replacement graph as follows:
    #
    #   (1) Copy over metadata from original subgraph. This ensures the stack traces
    #       and annotations are preserved in the new subgraph
    #
    #   (2) Copy over literal args for conv from the original subgraph
    #       TODO: do this for literal args for batchnorm as well
    #
    # In the future, we should try to push as much of this functionality into the
    # subgraph rewriter as possible, so we don't have to manually copy anything over.
    # For more detail, see https://github.com/pytorch/pytorch/issues/100419.

    for r in replacements_with_conv_bias + replacements_no_conv_bias:
        (replacement_conv_node, replacement_bn_node, replacement_getitem_node) =\
            _get_conv_bn_getitem_nodes(r.replacements)

        # Copy over metadata for all three nodes in [conv - bn - getitem]
        # Also copy over literal args for conv
        for match_pattern_node, original_node in r.nodes_map.items():
            # bias can be None
            if original_node is None or _node_is_input_to_matched_pattern(original_node):
                continue
            if original_node.target == torch.ops.aten.convolution.default:
                _copy_over_literal_conv_args(original_node, replacement_conv_node)
                replacement_conv_node.meta = original_node.meta
                # original annotation is referring to the node object in the graph
                # after rewrite we'll need to update this mapping (input_qspec_map)
                # update quantization_annotation
                original_input_qspec_map = original_node.meta["quantization_annotation"].input_qspec_map
                if "quantization_annotation" not in original_node.meta:
                    continue
                input_qspec_map = {}
                # get the list of configs, it should be ordered as input, weight, bias
                # note: this is really hacky, we need a better solution, hopefully
                # in subgraph_rewriter, issue tracking the problem: https://github.com/pytorch/pytorch/issues/101820
                all_configs = list(original_input_qspec_map.items())
                # input activation
                input_qspec_map[replacement_conv_node.args[0]] = all_configs[0][1]
                # weight
                input_qspec_map[replacement_conv_node.args[1]] = all_configs[1][1]
                # bias
                if len(replacement_conv_node.args) > 2 and len(all_configs) > 2:
                    input_qspec_map[replacement_conv_node.args[2]] = all_configs[2][1]
                replacement_conv_node.meta["quantization_annotation"].input_qspec_map = input_qspec_map
            if original_node.target == torch.ops.aten._native_batch_norm_legit.default:
                replacement_bn_node.meta = original_node.meta
            if original_node.target == operator.getitem:
                replacement_getitem_node.meta = original_node.meta
    return m

def _remove_placeholder_node(m: GraphModule, placeholder_index: int):
    """
    Helper function to erase the N'th placeholder node from the graph.
    This assumes the specified placeholder node has no users.
    """
    num_placeholders_seen = 0
    for n in m.graph.nodes:
        if n.op != "placeholder":
            continue
        if num_placeholders_seen == placeholder_index:
            m.graph.erase_node(n)
            m.recompile()
            return
        num_placeholders_seen += 1
    assert placeholder_index >= num_placeholders_seen
    raise ValueError("Placeholder index %s out of bounds" % placeholder_index)

def _duplicate_dequantize_node(m: GraphModule):
    """
    Helper function to duplicate all dequantize nodes in the graph if the
    node has more than one user. For example:

    Before:
      quantize -> dequantize -> a
                           \--> b
                           \--> c

    After:
      quantize -> dequantize_1 -> a
             \--> dequantize_2 -> b
             \--> dequantize_3 -> c

    This is useful for subgraph rewriting. E.g. if we wish to match the
    pattern [dequantize - a] above, subgraph matching would fail because
    the dequantize node has users outside the matched portion of the graph.
    Instead, we match [dequantize_1 - a], which is safe.
    """
    dequantize_op = torch.ops.quantized_decomposed.dequantize_per_tensor
    for n in m.graph.nodes:
        if n.op != "call_function" or n.target != dequantize_op or len(n.users) == 1:
             continue
        for user in list(n.users):
            with m.graph.inserting_before(n):
                new_node = m.graph.create_node("call_function", dequantize_op, n.args, n.kwargs)
            user.replace_input_with(n, new_node)
        m.graph.erase_node(n)
    m.recompile()

def _fold_conv_bn_qat(m: GraphModule) -> GraphModule:
    """
    Replace the quantized (conv + bn) pattern with conv with bn weights folded into the weights of conv.
    """
    m.graph.eliminate_dead_code()
    m.recompile()
    _duplicate_dequantize_node(m)

    # Step (1): Replace QAT pattern with simple [conv - bn] pattern
    replacements = []
    replacement_options = itertools.product(
        [True, False],  # is_per_channel
        [True, False],  # has_relu
        [True, False],  # has_bias
        [True, False],  # relu_is_inplace
    )
    for is_per_channel, has_relu, has_bias, relu_is_inplace in replacement_options:
        # For the cases without relu, `relu_is_inplace` is irrelevant, so here we arbitrarily
        # filter out one of the values for this flag to avoid having duplicate patterns
        if not has_relu and relu_is_inplace:
            continue
        example_inputs = _quantized_conv2d_bn_pattern_example_inputs
        match_pattern = _get_quantized_qat_conv2d_bn_pattern(
            is_per_channel, has_relu, has_bias, relu_is_inplace,
        )
        match_pattern = _get_aten_graph_module(match_pattern, example_inputs)
        replacement_pattern = _get_folded_quantized_qat_conv2d_bn_pattern(
            is_per_channel, has_relu, has_bias, relu_is_inplace,
        )
        replacement_pattern = _get_aten_graph_module(replacement_pattern, example_inputs)

        # In the no conv bias case, remove the `conv_bias` placeholder nodes from the graph so
        # subgraph rewriter doesn't complain
        if not has_bias:
            _remove_placeholder_node(match_pattern, 2)
            _remove_placeholder_node(replacement_pattern, 2)

        # Workaround: current convert does not produce q/dq ops with a specific overload
        # we'll remove the overload from the pattern here as a workaround since we do not want to break BC
        for n in match_pattern.graph.nodes:
            if n.op != "call_function":
                continue
            if n.target == torch.ops.quantized_decomposed.quantize_per_tensor.tensor:
                n.target = torch.ops.quantized_decomposed.quantize_per_tensor
            if n.target == torch.ops.quantized_decomposed.dequantize_per_tensor.tensor:
                n.target = torch.ops.quantized_decomposed.dequantize_per_tensor
            if n.target == torch.ops.quantized_decomposed.quantize_per_channel.default:
                n.target = torch.ops.quantized_decomposed.quantize_per_channel
            if n.target == torch.ops.quantized_decomposed.dequantize_per_channel.default:
                n.target = torch.ops.quantized_decomposed.dequantize_per_channel
        replacements.extend(replace_pattern_with_filters(
            m, match_pattern, replacement_pattern, match_filters=[], ignore_literals=True,
        ))
    m.recompile()

    # Step (2): Fold BN weights into conv
    for r in replacements:
        (conv_node, bn_node, _) = _get_conv_bn_getitem_nodes(r.replacements)

        # get conv weight and bias
        conv_weight_dq = conv_node.args[1]
        assert isinstance(conv_weight_dq, Node)
        assert conv_weight_dq.target in (
            torch.ops.quantized_decomposed.dequantize_per_tensor.tensor,
            torch.ops.quantized_decomposed.dequantize_per_channel.default,
        )
        conv_weight_q = conv_weight_dq.args[0]
        assert isinstance(conv_weight_q, Node)
        assert conv_weight_q.target in (
            torch.ops.quantized_decomposed.quantize_per_tensor.tensor,
            torch.ops.quantized_decomposed.quantize_per_channel.default,
        )
        conv_weight = conv_weight_q.args[0]
        assert isinstance(conv_weight, Node)
        assert conv_weight.op == "get_attr"
        conv_bias = conv_node.args[2]
        assert conv_bias is None or isinstance(conv_bias, Node)

        # fold bn weights into conv
        _fold_bn_weights_into_conv_node(conv_node, conv_weight, conv_bias, bn_node, m)

        # Copy over literal args for conv
        for _, original_node in r.nodes_map.items():
            # bias can be None
            if original_node is None or _node_is_input_to_matched_pattern(original_node):
                continue
            if original_node.target == torch.ops.aten.convolution.default:
                _copy_over_literal_conv_args(original_node, conv_node)

    m.graph.eliminate_dead_code()
    m.recompile()
    return m
