from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Target(StrictModel):
    kind: Literal["label", "role", "text"]
    name: str = Field(min_length=1, max_length=100)
    role: Literal["button", "link", "heading", "status", "textbox"] | None = None
    frame: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def role_target(self):
        if (self.kind == "role") != (self.role is not None):
            raise ValueError("Only role targets must declare a role")
        return self


class InputSpec(StrictModel):
    type: Literal["string"] = "string"
    description: str = Field(max_length=200)
    sensitive: bool = True
    max_length: int = Field(default=24, ge=1, le=100)


class OutputSpec(StrictModel):
    type: Literal["decimal", "string"]
    target: Target
    description: str = Field(max_length=200)


class Step(StrictModel):
    action: Literal["fill", "click"]
    target: Target
    input_ref: str | None = None

    @model_validator(mode="after")
    def value_source(self):
        if (self.action == "fill") != (self.input_ref is not None):
            raise ValueError("Only fill actions must have an input_ref")
        return self


class Capability(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,49}$")
    revision: int = Field(default=1, ge=1)
    product: Literal["demo-corebank"] = "demo-corebank"
    product_version: Literal["1"] = "1"
    inputs: dict[str, InputSpec] = Field(min_length=1, max_length=10)
    outputs: dict[str, OutputSpec] = Field(min_length=1, max_length=10)
    steps: list[Step] = Field(min_length=1, max_length=40)
    checkpoint: Target

    @model_validator(mode="after")
    def references(self):
        for name in [*self.inputs, *self.outputs]:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
                raise ValueError("Invalid contract field name")
        if set(self.inputs) & set(self.outputs):
            raise ValueError("Input/output field names must be distinct")
        for step in self.steps:
            if step.input_ref is not None and step.input_ref not in self.inputs:
                raise ValueError("Undeclared input reference")
        return self


class Control(StrictModel):
    id: str
    target: Target
    actions: list[Literal["fill", "click"]]


class Observation(StrictModel):
    route: str
    headings: list[str]
    controls: list[Control]
    condition: Literal["ready", "not_found", "validation", "permission_denied", "session_expired", "transient", "app_error", "unexpected_dialog", "policy_blocked"] = "ready"


class Decision(StrictModel):
    action: Literal["fill", "click", "done", "escalate"]
    control: str | None
    input_ref: str | None
    reason: Literal["locate_record", "navigate", "read_result", "complete", "blocked"]


def validate_inputs(specs: dict[str, InputSpec], values: dict) -> None:
    if set(specs) != set(values):
        raise ValueError("Missing or unexpected input fields")
    for name, spec in specs.items():
        value = values[name]
        if type(value) is not str or not 0 < len(value) <= spec.max_length:
            raise ValueError("Invalid input type or length")
        if any(ord(char) < 32 for char in value):
            raise ValueError("Control characters are not valid input")
