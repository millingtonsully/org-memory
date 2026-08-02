"""Shared request-field types for temporal tool surfaces."""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator

from org_memory.services.temporality.grain import parse_host_as_of_grain
from org_memory.services.temporality.types import TimeGrain

HostAsOfGrain = Annotated[
    TimeGrain | None,
    BeforeValidator(parse_host_as_of_grain),
]
