import builtins
import dataclasses
import itertools
import math
import operator

import torch
from torch.fx import Transformer
from torch.fx.operator_schemas import get_signature_for_torch_op

from torchdynamo.utils import counters
from torchdynamo.allowed_functions import _allowed_function_ids

VIEW_OPS = {
    # list taken from https://pytorch.org/docs/stable/tensor_view.html
    "getitem",
    "as_strided",
    "detach",
    "diagonal",
    "expand",
    "expand_as",
    "movedim",
    "narrow",
    "permute",
    "select",
    "squeeze",
    "transpose",
    "t",
    "T",
    "real",
    "imag",
    "view_as_real",
    "view_as_imag",
    "unflatten",
    "unfold",
    "unsqueeze",
    "view",
    "view_as",
    "unbind",
    "split",
    "split_with_sizes",
    "swapaxes",
    "swapdims",
    "chunk",
    "indices",
    "values",
}
MAYBE_VIEW_OPS = {"contiguous", "reshape"}

# convert x.foo(...) to torch.foo(x, ...)
NORMALIZE_METHODS = {
    # 'size'
    # 'permute'
    # 'reshape'
    "add_": operator.iadd,
    "all": torch.all,
    "chunk": torch.chunk,
    "clamp": torch.clamp,
    "clone": torch.clone,
    "exp": torch.exp,
    "flatten": torch.flatten,
    "flip": torch.flip,
    "log_softmax": torch.nn.functional.log_softmax,
    "max": torch.max,
    "mean": torch.mean,
    "mul_": operator.imul,
    "narrow": torch.narrow,
    "nonzero": torch.nonzero,
    "numel": torch.numel,
    "pow": torch.pow,
    "rsqrt": torch.rsqrt,
    "sigmoid": torch.sigmoid,
    "softmax": torch.nn.functional.softmax,
    "sort": torch.sort,
    "squeeze": torch.squeeze,
    "std": torch.std,
    "sum": torch.sum,
    "transpose": torch.transpose,
    "tril": torch.tril,
    "unsqueeze": torch.unsqueeze,
}
DONT_EXPAND_MODULES = {
    # These have internal control flow
    "ConvTranspose2d",
    "EmbeddingBag",
    "LSTM",
}
FUNCTION_REPLACEMENTS = {
    torch.nn.functional.sigmoid: torch.sigmoid,
    torch.nn.functional.tanh: torch.tanh,
}

F = torch.nn.functional
INPLACE_OPS = {
    F.mish,
    F.silu,
    F.hardsigmoid,
    F.rrelu,
    F.leaky_relu,
    F.celu,
    F.selu,
    F.elu,
    F.relu6,
    F.hardswish,
    F.hardtanh,
    F.relu,
    F.threshold,
}

SKIP_INPLACE = {
    v
    for v in itertools.chain(
        math.__dict__.values(), builtins.__dict__.values(), operator.__dict__.values()
    )
    if callable(v)
}


def always_true(*args, **kwargs):
    return True


class InliningTracer(torch.fx.Tracer):
    def is_leaf_module(self, m: torch.nn.Module, module_qualified_name: str) -> bool:
        return False


def expand_module_call(prefix, graph: torch.fx.Graph, module, args, kwargs):
    # this patch is needed to make BatchNorm2D FX trace
    module.__dict__["_check_input_dim"] = always_true
    try:
        assert not kwargs
        arg_index = itertools.count()
        vars = dict()
        for node in InliningTracer().trace(module).nodes:
            if node.op == "placeholder":
                vars[node] = args[next(arg_index)]
            elif node.op == "output":
                assert len(node.args) == 1
                return vars[node.args[0]]
            elif node.op == "get_attr":
                vars[node] = graph.get_attr(f"{prefix}{node.target}")
            else:
                vars[node] = graph.node_copy(node, vars.__getitem__)
        assert False
    except Exception:
        print(f"Error while expanding {module.__class__.__name__}")
        raise
    finally:
        del module.__dict__["_check_input_dim"]


@dataclasses.dataclass
class NodeCounts:
    usages: int = 0


def short_name(gm, node: torch.fx.Node):
    if node.op == "call_function":
        return node.target.__name__
    elif node.op == "call_method":
        return node.target
    elif node.op == "call_module":
        return gm.get_submodule(node.target).__class__.__name__
    elif node.op == "get_attr":
        return node.target
    elif node.op == "output":
        return "output"
    assert False, node.op


def long_name(gm, node: torch.fx.Node):
    name = short_name(gm, node)
    target = node.target
    if node.op == "call_function":
        try:
            return _allowed_function_ids()[id(node.target)]
        except KeyError:
            return f"{getattr(target, '__module__', '')}.{name}"
    elif node.op == "call_method":
        return name
    elif node.op == "call_module":
        target = gm.get_submodule(target).__class__
        return f"{getattr(target, '__module__', '')}.{getattr(target, '__name__', '')}"
    elif node.op == "get_attr":
        return name
    elif node.op == "output":
        return "output"
    assert False


class Inplacifier:
    def __init__(self, gm: torch.fx.GraphModule):
        self.gm = gm

    def can_be_view(self, node):
        name = short_name(self.gm, node)
        return name in VIEW_OPS or name in MAYBE_VIEW_OPS

    def inplacify(self):
        counts = dict()

        def record_usage(node):
            counts[node].usages += 1
            return node

        for node in self.gm.graph.nodes:
            if node.op in ("call_function", "call_method", "call_module"):
                if self.can_be_view(node):
                    # Aliasing
                    counts[node] = counts[node.args[0]]
                elif "out" in node.kwargs:
                    counts[node] = counts[node.kwargs["out"]]
                else:
                    counts[node] = NodeCounts(0)
            else:
                counts[node] = NodeCounts(float("inf"))

        for node in reversed(list(self.gm.graph.nodes)):
            kwargs = dict(node.kwargs)
            if "inplace" in kwargs:
                kwargs.pop("inplace")
            if node.op == "call_function" and len(node.args) + len(kwargs) == 1:
                arg = node.args[0] if node.args else next(kwargs.values())
                if isinstance(arg, torch.fx.Node) and counts[arg].usages == 0:
                    if node.target in SKIP_INPLACE:
                        continue
                    elif node.target in INPLACE_OPS:
                        kwargs["inplace"] = True
                        counters["optimizations"]["inplace"] += 1
                    elif " out: torch.Tensor" in repr(
                        get_signature_for_torch_op(node.target)
                    ):
                        kwargs["out"] = arg
                        counters["optimizations"]["out"] += 1
                    else:
                        continue
                    with self.gm.graph.inserting_before(node):
                        node.replace_all_uses_with(
                            self.gm.graph.call_function(node.target, node.args, kwargs)
                        )
                    self.gm.graph.erase_node(node)

            torch.fx.map_arg((node.args, node.kwargs), record_usage)


class Functionalization(Transformer):
    """
    Remove most cases of mutation from a given fx Graph.
    """

    def __init__(self, *args, **kwargs):
        super(Functionalization, self).__init__(*args, **kwargs)
        self.tracer.tensor_attrs = dict()  # TODO(jansel): upstream this fix

    def run_node(self, n: torch.fx.Node):
        patches = []
        target = n.target
        args, kwargs = self.fetch_args_kwargs_from_env(n)
        kwargs = dict(kwargs)
        module_name = getattr(n.target, "__module__", None)
        if not n.meta["is_input_mutation"] and issubclass(n.meta["type"], torch.Tensor):
            if "inplace" in n.kwargs:
                if kwargs["inplace"]:
                    patches.append(n.args[0])
                kwargs.pop("inplace")
            elif "out" in n.kwargs:
                kwargs.pop("out")
                patches.append(n.kwargs["out"])
            elif n.target is torch.relu_:
                target = torch.relu
                patches.append(n.args[0])
            elif module_name == "_operator" and n.target.__name__.startswith("i"):
                target = getattr(torch, n.target.__name__[1:])  # iadd, imul, etc
                patches.append(n.args[0])
            elif module_name == "_operator" and n.target not in (
                operator.getitem,
                operator.setitem,
                # TODO(jansel): debug issue with truediv on maskrcnn
                operator.truediv,
            ):
                name = n.target.__name__
                # if name == "truediv":
                #     if isinstance(args[0], (float, int)):
                #         args = (torch.Tensor([args[0]], device=n.meta["device"])[0], args[1])
                #     # target = torch.ops.aten.true_divide
                #     target = torch.div
                # else:
                name = {
                    "truediv": "div",
                    "and_": "bitwise_and",
                    "or_": "bitwise_or",
                }.get(name, name)
                target = getattr(torch, name)
            elif n.meta["is_mutation"]:
                counters["mutation"][long_name(self.module, n)] += 1

        if target is builtins.getattr:
            if args[1] == "dtype":
                return n.args[0].meta["dtype"]
            elif args[1] == "device":
                return n.args[0].meta["device"]
            else:
                counters["getattr"][args[1]] += 1
        elif not issubclass(n.meta["type"], torch.Tensor):
            counters["nontensor"][long_name(self.module, n)] += 1

        result = getattr(self, n.op)(target, args, kwargs)

        for patch in patches:
            assert isinstance(patch, torch.fx.Node)
            if patch in self.env:
                self.env[patch] = result

        return result


def swap_node(graph, old_node, new_node):
    old_node.replace_all_uses_with(new_node)
    graph.erase_node(old_node)


def normalize(gm: torch.fx.GraphModule):
    # gm.graph.print_tabular()
    graph: torch.fx.Graph = gm.graph

    for node in list(graph.nodes):
        with graph.inserting_before(node):
            if node.op == "call_method" and node.target in NORMALIZE_METHODS:
                swap_node(
                    graph,
                    node,
                    graph.call_function(
                        NORMALIZE_METHODS[node.target], node.args, node.kwargs
                    ),
                )
            elif node.op == "call_module":
                submod = gm.get_submodule(node.target)
                if submod.__class__.__name__ not in DONT_EXPAND_MODULES:
                    swap_node(
                        graph,
                        node,
                        expand_module_call(
                            f"{node.target}.", graph, submod, node.args, node.kwargs
                        ),
                    )

    # gm.graph.print_tabular()
