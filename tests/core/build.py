"""Small hand-built graphs for tests."""

from collections.abc import Iterable

from apm.core.model import Delivery, Node, NodeId, Status


def make_id(n: int) -> NodeId:
    """A valid id whose handle is `n` in hex: ids sort by `n`."""
    return f"01930000-0000-7000-8000-{n:012x}"


# Ids in creation order: A < B < C < D < E < F < G < H.
A, B, C, D, E, F, G, H = (make_id(n) for n in range(0xA, 0x12))


def node(
    node_id: NodeId,
    parent: NodeId | None = None,
    deps: Iterable[NodeId] = (),
    status: Status = Status.TODO,
    delivery: Delivery | None = None,
) -> Node:
    return Node(
        id=node_id,
        parent=parent,
        title=f"node {node_id[-2:]}",
        status=status,
        depends_on=tuple(sorted(deps)),
        delivery=delivery,
    )
