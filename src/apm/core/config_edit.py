"""Editing apm.yaml in place, keeping its comments and formatting.

`edit_yaml` changes a few values with ruamel.yaml's round-trip mode. Plain scalars are read as the
text they were written as, never as numbers or booleans, so whatever it doesn't change is written
back as it was: `no`, `2.00` or a handle like `12e45678` stay exactly that. It follows the file's
indentation and the spacing inside its `{ … }` mappings, and when it adds or removes an entry it
keeps the comment lines below a mapping's last entry (often the heading of the next section)
where they were.

What it can't keep: the spaces before an end-of-line comment on an edited line can shift, and the
values it writes are spelled its own way. Before handing the text back it reads both versions the
way `parse_config` does and checks that they differ by exactly the changes asked for, so a quirk
of the round trip can never quietly change another setting.
"""

import io
import itertools
import re
from collections.abc import Mapping
from typing import Any

import yaml as pyyaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.emitter import Emitter
from ruamel.yaml.error import CommentMark, YAMLError
from ruamel.yaml.events import MappingEndEvent
from ruamel.yaml.resolver import VersionedResolver
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
from ruamel.yaml.tokens import CommentToken

from apm.core.errors import ApmError

type KeyPath = tuple[str, ...]
type Scalar = bool | int | float | str
type Collection = CommentedMap | CommentedSeq

NULLS = ("", "~", "null", "Null", "NULL")
"""How YAML spells an empty value: `features:` with nothing under it."""


class ConfigEditError(ApmError, ValueError):
    """apm.yaml can't be edited as asked, e.g. a section that should be a mapping isn't one."""


def edit_yaml(text: str, changes: Mapping[KeyPath, Scalar | None]) -> str:
    """`text` with each path set to its value, or removed where the value is None. Missing
    mappings on the way are added at the end of their parent. A mapping a removal leaves empty is
    removed too, so clearing the last override leaves no empty `overrides:` behind.
    """
    yaml = _yaml(text)
    try:
        doc = yaml.load(text)
        before = pyyaml.safe_load(text)
    except (YAMLError, pyyaml.YAMLError) as err:
        raise ConfigEditError(f"apm.yaml can't be read: {err}") from None
    if not isinstance(doc, CommentedMap) or not isinstance(before, dict):
        raise ConfigEditError("apm.yaml must be a mapping")
    for path, value in changes.items():
        if value is None:
            _remove(doc, path)
            _remove_data(before, path)
        else:
            _set(doc, path, _scalar(value))
            _set_data(before, path, value)
    out = io.StringIO()
    yaml.dump(doc, out)
    if pyyaml.safe_load(out.getvalue()) != before:
        raise ConfigEditError(
            "APM couldn't make this change without touching other settings in apm.yaml: edit the"
            " file by hand"
        )
    return out.getvalue()


def _set_data(data: dict[str, Any], path: KeyPath, value: Scalar) -> None:
    """What `_set` should do, on the data `parse_config` reads."""
    for key in path[:-1]:
        if not isinstance(data.get(key), dict):
            data[key] = {}
        data = data[key]
    data[path[-1]] = value


def _remove_data(data: dict[str, Any], path: KeyPath) -> None:
    """What `_remove` should do, on the data `parse_config` reads."""
    chain = [data]
    for key in path[:-1]:
        if not isinstance(chain[-1].get(key), dict):
            return
        chain.append(chain[-1][key])
    if path[-1] not in chain[-1]:
        return
    del chain[-1][path[-1]]
    for depth in range(len(path) - 1, 0, -1):
        if chain[depth]:
            return
        del chain[depth - 1][path[depth - 1]]


# --- reading and writing the way the file is written ---


class _TextResolver(VersionedResolver):
    """Resolves no plain scalar to a type: each stays the text it was written as."""

    @property
    def versioned_resolver(self) -> dict[Any, Any]:
        return {}


class _Emitter(Emitter):
    spaced = False
    """Writes `{ a: 1 }` rather than `{a: 1}`."""

    def expect_first_flow_mapping_key(self) -> None:
        if self.spaced and not isinstance(self.event, MappingEndEvent) and self.flow_context[-1]:
            self.write_indicator(" ", False, whitespace=True)
        super().expect_first_flow_mapping_key()

    def expect_flow_mapping_key(self) -> None:
        if self.spaced and isinstance(self.event, MappingEndEvent) and self.flow_context[-1]:
            self.write_indicator(" ", False)
        super().expect_flow_mapping_key()


class _SpacedEmitter(_Emitter):
    spaced = True


_PARENT_RE = re.compile(r"( *)[^\s#-][^:]*:\s*(#.*)?")
"""A key whose value is on the lines below it."""


def _yaml(text: str) -> YAML:
    yaml = YAML()
    yaml.Resolver = _TextResolver
    yaml.Emitter = _SpacedEmitter if re.search(r"\{ \S", text) else _Emitter
    yaml.preserve_quotes = True
    yaml.width = 4096  # never fold a long line
    yaml.explicit_start = re.match(r"(\s*#.*\n|\s*\n)*---", text) is not None
    mapping, sequence, offset = _indents(text)
    yaml.indent(mapping=mapping, sequence=sequence, offset=offset)
    return yaml


def _indents(text: str) -> tuple[int, int, int]:
    """The file's indentation, as ruamel.yaml takes it: a nested mapping's step, a list item's
    content and its dash, each from the parent key. Defaults to what `apm init` writes.
    """
    mapping, sequence, offset = None, None, None
    lines = [line for line in text.splitlines() if line.strip() and line.lstrip()[0] != "#"]
    for above, below in itertools.pairwise(lines):
        if (parent := _PARENT_RE.fullmatch(above)) is None:
            continue
        step = len(below) - len(below.lstrip(" ")) - len(parent.group(1))
        item = below.lstrip(" ")
        if item.startswith("- ") and sequence is None and step >= 0:
            offset = step
            sequence = step + len(item) - len(item[1:].lstrip(" "))
        elif not item.startswith("-") and mapping is None and step > 0:
            mapping = step
    return mapping or 2, sequence or 2, offset or 0


def _scalar(value: Scalar) -> Any:
    """A value as written in the file. A string that YAML would read as something else is quoted."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.1f}" if value.is_integer() else repr(value)
    if isinstance(value, int):
        return str(value)
    try:
        plain = pyyaml.safe_load(value) == value
    except pyyaml.YAMLError:
        plain = False
    return value if plain and value.strip() == value else DoubleQuotedScalarString(value)


# --- changing the document ---


def _set(doc: CommentedMap, path: KeyPath, value: Any) -> None:
    node = doc
    for depth, key in enumerate(path[:-1], start=1):
        child = node.get(key)
        if isinstance(child, CommentedMap):
            node = child
            continue
        if child is not None and not (isinstance(child, str) and child in NULLS):
            raise ConfigEditError(f"{'.'.join(path[:depth])} in apm.yaml must be a mapping")
        # Whatever is missing goes in as one piece, so the comments below stay below all of it.
        for inner in reversed(path[depth:]):
            value = CommentedMap([(inner, value)])
        break
    else:
        key = path[-1]
    if key in node:
        node[key] = value
    else:
        _append(node, key, value)


def _remove(doc: CommentedMap, path: KeyPath) -> None:
    chain = [doc]
    for key in path[:-1]:
        child = chain[-1].get(key)
        if not isinstance(child, CommentedMap):
            return  # nothing there to remove
        chain.append(child)
    if path[-1] not in chain[-1]:
        return
    left = _delete(chain[-1], path[-1])
    # Mappings left empty go too, from the inside out, taking along the comments that followed.
    for depth in range(len(path) - 1, 0, -1):
        if len(chain[depth]):
            return
        left = _delete(chain[depth - 1], path[depth - 1], left)


def _append(node: CommentedMap, key: str, value: Any) -> None:
    """Adds an entry to a mapping: a plain value after the other plain values, so it doesn't land
    below a nested mapping (`eager:` under `overrides:`), else at the end, above the comment lines
    that followed the last entry.
    """
    if not _nested(value):
        plain = [i for i, k in enumerate(node) if not _nested(node[k])]
        if (at := plain[-1] + 1 if plain else 0) < len(node):
            node.insert(at, key, value)
            return
    tail = _detach_tail(node, list(node)[-1]) if len(node) and not _is_flow(node) else None
    node[key] = value
    if tail:
        _attach_tail(node, tail)


def _nested(value: Any) -> bool:
    """A mapping or list written over several lines."""
    return (
        isinstance(value, CommentedMap | CommentedSeq) and bool(len(value)) and not _is_flow(value)
    )


def _delete(node: CommentedMap, key: str, carried: str = "") -> str:
    """Removes an entry. The comment lines below it move to the entry above, or to the one below
    if it was the first. Returns them if the mapping is now empty, for the caller to place.
    """
    keys = list(node)
    index = keys.index(key)
    tail = (_detach_tail(node, key) or "") + carried
    del node[key]
    if not tail:
        return ""
    if index > 0:
        _attach_tail(node, tail, keys[index - 1])
    elif len(node):
        _attach_above(node, keys[1], tail)
    else:
        return tail
    return ""


# --- comments ---
#
# ruamel.yaml keeps the comment lines after an entry with the deepest last value inside it, as the
# rest of that value's end-of-line comment. Left there, they would move along with that value.


def _is_flow(node: Collection) -> bool:
    return bool(node.fa.flow_style())


def _leaf(node: Collection, key: Any) -> tuple[Collection, Any]:
    """The last value inside `node[key]`, as (its collection, its key or index)."""
    while isinstance(value := node[key], CommentedMap | CommentedSeq):
        if not len(value) or _is_flow(value):
            break
        node, key = value, (list(value)[-1] if isinstance(value, CommentedMap) else len(value) - 1)
    return node, key


def _slot(node: Collection) -> int:
    """Where a value's end-of-line comment sits in `ca.items`."""
    return 0 if isinstance(node, CommentedSeq) else 2


def _detach_tail(node: Collection, key: Any) -> str | None:
    """Takes the comment lines after `node[key]` off it (its end-of-line comment stays)."""
    leaf, leaf_key = _leaf(node, key)
    entry = leaf.ca.items.get(leaf_key)
    token = entry[_slot(leaf)] if entry else None
    if token is None:
        return None
    head, _, tail = token.value.partition("\n")
    if not tail:
        return None
    if head.strip():
        token.value = head + "\n"
    else:
        entry[_slot(leaf)] = None
    return tail


def _attach_tail(node: CommentedMap, tail: str, key: Any = None) -> None:
    """Puts comment lines after `node[key]` (default: its last entry)."""
    leaf, leaf_key = _leaf(node, list(node)[-1] if key is None else key)
    entry = leaf.ca.items.setdefault(leaf_key, [None, None, None, None])
    token = entry[_slot(leaf)]
    if token is None:
        entry[_slot(leaf)] = CommentToken("\n" + tail, CommentMark(0))
    else:
        token.value = token.value.rstrip("\n") + "\n" + tail


def _attach_above(node: CommentedMap, key: str, tail: str) -> None:
    """Puts comment lines before `node[key]`."""
    lines = [
        CommentToken(line + "\n", CommentMark(0)) for line in tail.removesuffix("\n").split("\n")
    ]
    entry = node.ca.items.setdefault(key, [None, None, None, None])
    entry[1] = [*lines, *(entry[1] or [])]
