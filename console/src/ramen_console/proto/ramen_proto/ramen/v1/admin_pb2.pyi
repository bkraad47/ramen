from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class ReloadRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class LoadResult(_message.Message):
    __slots__ = ("json",)
    JSON_FIELD_NUMBER: _ClassVar[int]
    json: bytes
    def __init__(self, json: _Optional[bytes] = ...) -> None: ...

class MetricsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class MetricsReply(_message.Message):
    __slots__ = ("json",)
    JSON_FIELD_NUMBER: _ClassVar[int]
    json: bytes
    def __init__(self, json: _Optional[bytes] = ...) -> None: ...
