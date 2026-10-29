"""Small tool registry; drawing remains intentionally disconnected in v1."""

import asyncio
import json
import re
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["pending", "succeeded", "failed"]
    message: str = Field(default="", max_length=6000)
    operation_id: str = Field(default="", max_length=500)
    images: list[str] = Field(default_factory=list, max_length=10)
    evidence_images: list[str] = Field(default_factory=list, max_length=10)
    next_arguments: dict[str, JsonValue] | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_result(self):
        if self.state == "pending" and not self.operation_id:
            raise ValueError("pending tool requires an operation_id")
        if self.next_arguments is not None and (self.state != 'succeeded'
                or {'request_id','operation_id'} & self.next_arguments.keys()
                or len(json.dumps(self.next_arguments,ensure_ascii=False,allow_nan=False).encode()) > 16384):
            raise ValueError('invalid tool continuation')
        if any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
               for value in self.images + self.evidence_images):
            raise ValueError("tool images must reference registered asset hashes")
        if self.state != "succeeded" and (self.images or self.evidence_images):
            raise ValueError("unfinished tools cannot supply result images")
        if len(json.dumps(self.data, ensure_ascii=False, allow_nan=False).encode()) > 65536:
            raise ValueError("structured tool data exceeds 64 KiB")
        return self


class ToolRegistry:
    def __init__(self):
        self.handlers = {}
        self._registered = {}

    def register(self, name: str, handler, *, description: str, input_schema: dict, continuation=False):
        if (not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name) or
                name in {'image_generate', 'image_edit'} or name in self._registered or name in self.handlers):
            raise ValueError('invalid, reserved or duplicate tool name')
        if (not callable(handler) or not isinstance(description, str) or not description.strip()
                or len(description) > 1000 or not isinstance(input_schema, dict)
                or input_schema.get('type') != 'object' or type(continuation) is not bool):
            raise ValueError('tool requires a handler, description and object input schema')
        schema = json.loads(json.dumps(input_schema, allow_nan=False))
        self._registered[name] = ({'name':name, 'description':description, 'input':schema,
                                  'continuation':continuation}, handler)
        self.enable(name)

    def disable(self, name: str):
        if name not in self._registered:
            raise KeyError(name)
        self.handlers.pop(name, None)

    def enable(self, name: str):
        self.handlers[name] = self._registered[name][1]

    def unregister(self, name: str):
        self.disable(name)
        del self._registered[name]

    def definitions(self) -> list[dict]:
        builtins = [
            {"name": name, "description": description, "enabled": name in self.handlers,
             "input": {"prompt": "string", "reference_images": "optional array of asset hashes"}}
            for name, description in [("image_generate", "根据文字生成图片"),
                                       ("image_edit", "按文字要求编辑参考图片")]
        ]
        return builtins + [deepcopy(spec) | {'enabled':spec['name'] in self.handlers}
                           for spec, _ in tuple(self._registered.values())]

    def continues(self, name):
        return bool(name in self._registered and self._registered[name][0]['continuation'])

    def validate_call(self, name, arguments):
        """Check the JSON envelope and declared scalar fields; providers validate semantics/nesting."""
        if not isinstance(name, str) or name not in self.handlers:
            raise ValueError('tool is not enabled')
        if (not isinstance(arguments, dict) or {'request_id','operation_id'} & arguments.keys()
                or len(json.dumps(arguments,ensure_ascii=False,allow_nan=False).encode()) > 16384):
            raise ValueError('invalid tool arguments')
        schema = self._registered[name][0]['input'] if name in self._registered else {}
        properties = schema.get('properties', {})
        if (set(schema.get('required', [])) - arguments.keys()
                or schema.get('additionalProperties') is False and arguments.keys() - properties.keys()):
            raise ValueError('tool arguments do not match declared fields')
        types = {'string':(str,), 'integer':(int,), 'number':(int,float),
                 'boolean':(bool,), 'object':(dict,), 'array':(list,), 'null':(type(None),)}
        for key, value in arguments.items():
            rule = properties.get(key, {})
            kind = rule.get('type')
            if kind in types and type(value) not in types[kind]:
                raise ValueError('tool argument type mismatch')
            if 'enum' in rule and value not in rule['enum']:
                raise ValueError('tool argument outside declared choices')
            if isinstance(value, str) and not rule.get('minLength',0) <= len(value) <= rule.get('maxLength',16384):
                raise ValueError('tool argument length outside declared bounds')
            if type(value) in (int,float) and not rule.get('minimum',float('-inf')) <= value <= rule.get('maximum',float('inf')):
                raise ValueError('tool argument outside declared bounds')
        return json.loads(json.dumps(arguments,allow_nan=False))

    async def invoke(self, name: str, payload: dict, *, request_id: str = "",
                     operation_id: str = "") -> dict:
        handler = self.handlers.get(name)
        if handler is None:
            return {"state": "unavailable", "message": "暂时还不会画图" if name in
                    {'image_generate', 'image_edit'} else "该技能尚未启用或已移除"}
        async with asyncio.timeout(30):
            result = await handler(payload | {"request_id":request_id, "operation_id":operation_id})
        result = ToolResult.model_validate(result)
        if result.next_arguments is not None and not self.continues(name):
            raise ValueError('tool has no continuation permission')
        return result.model_dump()
