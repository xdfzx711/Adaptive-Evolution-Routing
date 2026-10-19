"""Serializable domain values, independent of Agent and model SDKs."""

from collections.abc import Mapping
from datetime import datetime, timezone
from math import isfinite
from types import MappingProxyType
from typing import Annotated, TypeVar

from pydantic import (
    AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field,
    JsonValue, PlainSerializer,
)


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must contain non-whitespace characters")
    return value


def _tuple(value):
    return tuple(value) if isinstance(value, list) else value


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("JSON numbers must be finite")
    return value


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(timezone.utc)
    return value


T = TypeVar("T")
Items = Annotated[tuple[T, ...], BeforeValidator(_tuple)]
Identifier = Annotated[str, AfterValidator(_non_blank)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
NonNegativeFloat = Annotated[FiniteFloat, Field(ge=0)]
PositiveFloat = Annotated[FiniteFloat, Field(gt=0)]
Score = Annotated[FiniteFloat, Field(ge=0, le=1)]
Reward = Annotated[FiniteFloat, Field(ge=-1, le=1)]
UtcTimestamp = Annotated[datetime, BeforeValidator(_utc)]
JsonObject = Annotated[
    Mapping[Identifier, JsonValue],
    AfterValidator(_freeze),
    PlainSerializer(_plain, return_type=dict[str, JsonValue]),
]
ReadOnlyMap = Annotated[
    Mapping[Identifier, T],
    AfterValidator(lambda value: MappingProxyType(dict(value))),
    PlainSerializer(dict, return_type=dict[str, T]),
]


class DomainModel(BaseModel):
    """Strict, frozen fields with immutable collection types in domain schemas.

    Use model_validate/model_validate_json for untrusted data and replace for
    updates. Pydantic's model_construct/model_copy(update=...) bypass validation.
    """

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False,
        validate_default=True,
    )

    def replace(self, **updates):
        data = self.model_dump(mode="python")
        data.update(updates)
        return type(self).model_validate(data)
