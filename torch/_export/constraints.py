from typing import Optional, Callable, Union

import torch
from torch.types import Number, _complex
from torch import SymInt, SymFloat
from torch._dynamo import allow_in_graph
from torch.fx.experimental.symbolic_shapes import constrain_range_non_symint
from torch.utils._sympy.value_ranges import ValueRangeError

# `Scalar` type used in native_functions.ymal will be translated to `Union[Number, _complex]`
# could cause type error during since `SymInt` or `SymFloat` will be used.
# Here manually specify the type explicitly.
sym_constrain_range: Callable[
    [Union[Number, _complex, SymInt, SymFloat], Optional[int], Optional[int]],
    None,
] = torch.sym_constrain_range  # type: ignore[assignment]


def _constrain_range(symbol, min: Optional[int] = None, max: Optional[int] = None):
    if not isinstance(symbol, SymInt):
        # This is needed to check if an input value (as a real numeric value, or
        # an value derived from numeric input) for exporting is within range or
        # not.
        constrain_range_non_symint(symbol, min=min, max=max)
        return

    sym_constrain_range(symbol, min, max)


# TODO: we want to hide this min/max stuff under some abstraction similar to
# DynamicDim
@allow_in_graph
def constrain_as_value(symbol, min: Optional[int] = None, max: Optional[int] = None):
    """
    Add min/max constraint on the intermediate symbol at tracing time
    """

    _constrain_range(symbol, min=min, max=max)
    return symbol


# TODO: we want to hide this min/max stuff under some abstraction similar to
# DynamicDim
@allow_in_graph
def constrain_as_size(symbol, min: int = 2, max: Optional[int] = None):
    """
    Add min/max constraint on the intermediate symbol which will be used as a size
    """

    # TODO: we should investigate turning off 0/1 specialization for unbacked
    # SymInts
    if min < 2:
        raise ValueRangeError(
            "Unable to set min size to be <= 2 because we specialize on 0/1 sizes."
        )
    return constrain_as_value(symbol, min, max)
