"""A whole schema snapshot, and its JSON form.

Making an inventory round-trip through JSON is the highest-leverage structural choice in the
tool:

* production and QA can be captured separately and compared later, with no moment at which
  one process holds credentials for both;
* ``diff-inventories`` and ``render`` work entirely offline;
* integration tests capture real catalogs once and unit tests replay them forever, so
  normalisation can be tested against real server output without a database.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, is_dataclass, replace
from dataclasses import fields as dataclass_fields
from datetime import datetime
from functools import cache
from typing import Any, get_args, get_origin, get_type_hints

from .changelog import ChangelogState
from .keys import ObjectKey
from .kinds import KIND_ORDER, ObjectKind

SCHEMA_VERSION = 1

#: Object types keyed by kind, for deserialisation.
_OBJECT_TYPES: dict[ObjectKind, type] = {}


def _register() -> None:
    from . import objects

    _OBJECT_TYPES[ObjectKind.TABLE] = objects.Table
    _OBJECT_TYPES[ObjectKind.COLUMN] = objects.Column
    _OBJECT_TYPES[ObjectKind.CONSTRAINT] = objects.Constraint
    _OBJECT_TYPES[ObjectKind.INDEX] = objects.Index
    _OBJECT_TYPES[ObjectKind.VIEW] = objects.View
    _OBJECT_TYPES[ObjectKind.MATVIEW] = objects.View
    _OBJECT_TYPES[ObjectKind.SEQUENCE] = objects.Sequence
    _OBJECT_TYPES[ObjectKind.ROUTINE] = objects.Routine
    _OBJECT_TYPES[ObjectKind.TRIGGER] = objects.Trigger
    _OBJECT_TYPES[ObjectKind.ENUM_TYPE] = objects.UserType
    _OBJECT_TYPES[ObjectKind.DOMAIN_TYPE] = objects.UserType
    _OBJECT_TYPES[ObjectKind.COMPOSITE_TYPE] = objects.UserType
    _OBJECT_TYPES[ObjectKind.RANGE_TYPE] = objects.UserType
    _OBJECT_TYPES[ObjectKind.EXTENSION] = objects.Extension


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """What was inspected, and by which server.

    Never carries a credential: only the parts of a connection that are safe to publish in a
    CI artifact.
    """

    label: str
    database: str
    user: str
    server_version_num: int
    server_version: str
    encoding: str
    datcollate: str
    datctype: str
    captured_at: datetime
    host: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def major_version(self) -> int:
        """PostgreSQL major release, e.g. ``15``."""
        return self.server_version_num // 10000

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "host": self.host,
            "database": self.database,
            "user": self.user,
            "server_version_num": self.server_version_num,
            "server_version": self.server_version,
            "encoding": self.encoding,
            "datcollate": self.datcollate,
            "datctype": self.datctype,
            "captured_at": self.captured_at.isoformat(),
            "notes": list(self.notes),
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> SourceInfo:
        return cls(
            label=data["label"],
            host=data.get("host"),
            database=data["database"],
            user=data["user"],
            server_version_num=int(data["server_version_num"]),
            server_version=data["server_version"],
            encoding=data["encoding"],
            datcollate=data["datcollate"],
            datctype=data["datctype"],
            captured_at=datetime.fromisoformat(data["captured_at"]),
            notes=tuple(data.get("notes", ())),
        )


@dataclass(frozen=True)
class Inventory:
    """Every comparable object in one database, already canonicalised."""

    source: SourceInfo
    schemas: tuple[str, ...]
    objects: Mapping[ObjectKey, Any] = field(default_factory=dict)
    changelog: ChangelogState | None = None

    def __len__(self) -> int:
        return len(self.objects)

    @property
    def is_empty(self) -> bool:
        """Whether nothing a deployment would have created was found.

        A brand-new target should produce one "target is empty" finding rather than one per object
        the master has.

        Extensions are ignored when deciding this. ``plpgsql`` is installed in every PostgreSQL
        database by default, so counting it would make a freshly created database look non-empty and
        the short circuit would never fire — which is exactly when it is needed.
        """
        return not any(key.kind is not ObjectKind.EXTENSION for key in self.objects)

    def by_kind(self, kind: ObjectKind) -> dict[ObjectKey, Any]:
        """Every object of one kind, keyed as in :attr:`objects`."""
        return {key: value for key, value in self.objects.items() if key.kind is kind}

    def kinds(self) -> tuple[ObjectKind, ...]:
        """Kinds actually present, in report order."""
        present = {key.kind for key in self.objects}
        return tuple(kind for kind in KIND_ORDER if kind in present)

    def sorted_keys(self) -> list[ObjectKey]:
        """Every key in deterministic report order."""
        return sorted(self.objects, key=lambda key: key.sort_key)

    def counts(self) -> dict[ObjectKind, int]:
        """How many objects of each kind, in report order."""
        counts: dict[ObjectKind, int] = {}
        for key in self.objects:
            counts[key.kind] = counts.get(key.kind, 0) + 1
        return {kind: counts[kind] for kind in KIND_ORDER if kind in counts}

    def remap_schemas(self, mapping: Mapping[str, str]) -> Inventory:
        """Rename schemas so this inventory can be compared in another one's namespace.

        Needed because some services deploy with a per-environment ``defaultSchemaName``, so
        the same table lives under a different schema name in each environment.
        """
        if not mapping:
            return self
        remapped: dict[ObjectKey, Any] = {}
        for key, value in self.objects.items():
            new_schema = mapping.get(key.schema, key.schema)
            if new_schema == key.schema:
                remapped[key] = value
                continue
            new_key = key.with_schema(new_schema)
            remapped[new_key] = replace(value, key=new_key)
        return Inventory(
            source=self.source,
            schemas=tuple(mapping.get(s, s) for s in self.schemas),
            objects=remapped,
            changelog=self.changelog,
        )

    def fingerprint(self) -> str:
        """Stable digest of the whole inventory.

        Equal fingerprints mean the two databases are structurally identical, which makes a
        cheap "nothing changed since the last run" check possible.
        """
        digest = hashlib.sha256()
        for key in self.sorted_keys():
            digest.update(key.path.encode("utf-8"))
            digest.update(repr(self.objects[key]).encode("utf-8"))
        return digest.hexdigest()

    def to_json_dict(self) -> dict[str, Any]:
        """Lossless JSON form. See :func:`Inventory.from_json_dict` for the inverse."""
        return {
            "schema_version": SCHEMA_VERSION,
            "source": self.source.to_json_dict(),
            "schemas": list(self.schemas),
            "objects": [_object_to_json(key, self.objects[key]) for key in self.sorted_keys()],
            "changelog": self.changelog.to_json_dict() if self.changelog else None,
        }

    def to_json(self) -> str:
        """Pretty, stably ordered JSON text."""
        return json.dumps(self.to_json_dict(), indent=2, ensure_ascii=False, sort_keys=False)

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> Inventory:
        """Rebuild an inventory written by :meth:`to_json_dict`."""
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported inventory schema_version {version!r}; "
                f"this build writes {SCHEMA_VERSION}"
            )
        if not _OBJECT_TYPES:
            _register()
        objects: dict[ObjectKey, Any] = {}
        for entry in data["objects"]:
            key, value = _object_from_json(entry)
            objects[key] = value
        changelog = data.get("changelog")
        return cls(
            source=SourceInfo.from_json_dict(data["source"]),
            schemas=tuple(data["schemas"]),
            objects=objects,
            changelog=ChangelogState.from_json_dict(changelog) if changelog else None,
        )

    @classmethod
    def from_json(cls, text: str) -> Inventory:
        """Rebuild an inventory from JSON text."""
        return cls.from_json_dict(json.loads(text))


def _encode_nested(item: Any) -> dict[str, Any]:
    """One nested dataclass as a plain dict."""
    return {f.name: getattr(item, f.name) for f in dataclass_fields(item)}


def _object_to_json(key: ObjectKey, value: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": key.kind.value,
        "schema": key.schema,
        "name": key.name,
    }
    if key.subname is not None:
        payload["subname"] = key.subname
    attributes: dict[str, Any] = {}
    raw: dict[str, str] = {}
    value_type: type = type(value)
    nested = _nested_element_types(value_type)
    for f in dataclass_fields(value):
        if f.name == "key":
            continue
        current = getattr(value, f.name)
        if f.name == "raw":
            raw = dict(current.values)
            continue
        if f.name in nested:
            attributes[f.name] = [_encode_nested(item) for item in current]
        elif isinstance(current, tuple):
            attributes[f.name] = list(current)
        else:
            attributes[f.name] = current
    payload["attributes"] = attributes
    if raw:
        payload["raw"] = raw
    return payload


@cache
def _nested_element_types(cls: type) -> dict[str, type]:
    """Tuple fields whose elements are themselves dataclasses, and that element type.

    An index's keys are a tuple of :class:`~cumo_schema_comparer.model.objects.IndexKey`, so the
    JSON form has to nest and the round trip has to rebuild them. Resolved from the annotations
    rather than by inspecting values, so an empty tuple round-trips correctly too.
    """
    hints = get_type_hints(cls)
    nested: dict[str, type] = {}
    for f in dataclass_fields(cls):
        hint = hints.get(f.name)
        if get_origin(hint) is not tuple:
            continue
        args = [a for a in get_args(hint) if a is not Ellipsis]
        if len(args) == 1 and is_dataclass(args[0]) and isinstance(args[0], type):
            nested[f.name] = args[0]
    return nested


@cache
def _sequence_fields(cls: type) -> frozenset[str]:
    """Names of fields declared as tuples, so JSON lists are restored as tuples.

    Resolved from the real annotations rather than by inspecting their text: these classes
    use ``from __future__ import annotations``, so ``field.type`` is an unresolved string and
    substring-matching it would silently misclassify a field on the next type change.
    """
    hints = get_type_hints(cls)
    names = set()
    for f in dataclass_fields(cls):
        hint = hints.get(f.name)
        for candidate in (hint, *get_args(hint)):
            if get_origin(candidate) is tuple:
                names.add(f.name)
                break
    return frozenset(names)


def _object_from_json(entry: Mapping[str, Any]) -> tuple[ObjectKey, Any]:
    from .objects import RawValues

    kind = ObjectKind(entry["kind"])
    key = ObjectKey(kind, entry["schema"], entry["name"], entry.get("subname"))
    cls = _OBJECT_TYPES.get(kind)
    if cls is None:
        raise ValueError(f"inventory contains an object kind this build cannot read: {kind.value}")
    attributes = dict(entry.get("attributes", {}))
    unknown = set(attributes) - {f.name for f in dataclass_fields(cls)}
    if unknown:
        # A newer build wrote attributes this one does not know. Failing loudly beats
        # silently comparing a subset of the data and reporting "in sync".
        raise ValueError(
            f"inventory {kind.value} {key.path!r} has attributes this build cannot read: "
            + ", ".join(sorted(unknown))
        )
    nested = _nested_element_types(cls)
    for name, element_type in nested.items():
        if attributes.get(name) is not None:
            attributes[name] = tuple(element_type(**item) for item in attributes[name])
    for name in _sequence_fields(cls) - set(nested):
        if attributes.get(name) is not None:
            attributes[name] = tuple(attributes[name])
    kwargs: dict[str, Any] = {"key": key, **attributes}
    if "raw" in entry:
        kwargs["raw"] = RawValues(dict(entry["raw"]))
    return key, cls(**kwargs)


def iter_objects(inventory: Inventory, kind: ObjectKind) -> Iterator[Any]:
    """Objects of one kind, in report order."""
    for key in inventory.sorted_keys():
        if key.kind is kind:
            yield inventory.objects[key]
