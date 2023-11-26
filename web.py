import asyncio
import base64
from collections import deque
import datetime
from enum import Enum
import hashlib
import inspect
import io
import json
from mimetypes import guess_type
import os
from pathlib import Path
import re
import stat
import struct
import time
import traceback
from typing import Any, Callable, Coroutine, Optional, Union, get_args
import typing

import aiofiles

from utils import Client, fixedValue, parse_obj_as_type, error, info
from urllib import parse as urlparse

class Route:
    def __init__(self, path: str, method: str, handler: Callable[..., Coroutine], ratelimit_seconds: float = 0, ratelimit_count: int = -1, ratelimit_func = None) -> None:
        if not path.startswith("/"):
            path = f'/{path}'
        path = path.replace("//", "/")
        self.raw_path = path
        self.path = [path.rstrip("/"), path.rstrip("/") + "/"]
        self.handler = handler
        self.method = method or "GET"
        self.ratelimit = ratelimit_seconds != 0 and ratelimit_count >= 1
        self.params = self.is_params(path)
        if self.params:
            self.param = re.findall(r'{(\w+)}', path)
            self.regexp: list[str] = [rf"^{p.replace('{', '(?P<').replace('}', r'>[^/]*)')}$" for p in self.path]
        if self.ratelimit:
            self.ratelimit_configs = {
                "seconds": ratelimit_seconds,
                "count": ratelimit_count,
                "func": ratelimit_func,
            }
            self.ratelimit_cache = {}

    def is_params(self, path: str):
        return path.count("{") == path.count("}") != 0
    def __str__(self) -> str:
        return self.raw_path[0]
    def get_limit_func(self):
        return self.ratelimit_configs['func']
    def is_limit(self, request: 'Request'):
        if not self.ratelimit:
            return False
        if request.address not in self.ratelimit_cache:
            self.ratelimit_cache[request.address] = {
                'count': 0,
                'seconds': 0
            }
        return self.ratelimit_cache[request.address]['count'] >= self.ratelimit_configs['count'] and self.ratelimit_cache[request.address]['seconds'] >= time.time()

    def add_limit(self, request: 'Request'):
        if not self.ratelimit:
            return
        if request.address not in self.ratelimit_cache:
            self.ratelimit_cache[request.address] = {
                'count': 0,
                'seconds': 0
            }
        if self.ratelimit_cache[request.address]['seconds'] < time.time():
            self.ratelimit_cache[request.address]['seconds'] = time.time() + self.ratelimit_configs['seconds']
        self.ratelimit_cache[request.address]['count'] += 1
    def get_limit(self, request: 'Request'):
        if request.address not in self.ratelimit_cache:
            self.ratelimit_cache[request.address] = {
                'count': 0,
                'seconds': 0
            }
        return self.ratelimit_cache[request.address]

class _StopIteration(Exception):
    ...

class Application:
    def __init__(self) -> None:
        global application
        application = self
        self.routes: list[Route] = []
        self.param_routes: list[Route] = []
        self.resources: dict[str, str] = {}
        self.startups = []
        self.shutdowns = []
        self.started = False
    async def start(self):
        if self.started:
            return
        self.started = True
        [asyncio.create_task(task() if inspect.iscoroutinefunction(task) else task) for task in self.startups]
    async def stop(self):
        if not self.started:
            return
        self.started = False
        [asyncio.create_task(task() if inspect.iscoroutinefunction(task) else task) for task in self.shutdowns]
    def startup(self):
        def decorator(f):
            self.startups.append(f)
            return f
        return decorator   
    def shutdown(self):
        def decorator(f):
            self.shutdowns.append(f)
            return f
        return decorator
    def _add_route(self, path, method, f, ratelimit_seconds: float = 0, ratelimit_count: int = -1, ratelimit_func = None):
        route = Route(path, method, f, ratelimit_seconds=ratelimit_seconds, ratelimit_count=ratelimit_count, ratelimit_func=ratelimit_func)
        if route.params:
            self.param_routes.append(route)
            self.param_routes.sort(key=lambda route: route.raw_path.index("{"), reverse=True)
        else:
            self.routes.append(route)
    def route(self, path, method, ratelimit_seconds: float = 0, ratelimit_count: int = -1, ratelimit_func = None):
        def decorator(f):
            self._add_route(path, method, f, ratelimit_seconds=ratelimit_seconds, ratelimit_count=ratelimit_count, ratelimit_func=ratelimit_func)
            return f
        return decorator
    async def handle(self, request: 'Request', client: Client):
        routes = filter(lambda route: route.method == request.method, [*self.routes, *self.param_routes])
        for route in routes:
            matched: tuple[bool, dict[str, Any]] = self.match_url(route, request.path)
            if matched[0]:
                params: dict[str, Any] = matched[1]
                url_params: dict[str, Any] = request.params
                has_set = []
                handler = route.handler if not route.is_limit(request=request) else (default_limit if route.get_limit_func() is None else route.get_limit_func())
                annotations = inspect.get_annotations(handler)
                default_params: dict[str, Any] = {name.lower(): value.default for name, value in inspect.signature(handler).parameters.items() if (not isinstance(value, inspect._empty)) and (value.default != inspect._empty)}
                request_params_lower = {k.lower(): v for k, v in request.params.items()}
                request_params_lower.update({k.lower(): v for k, v in url_params.items()})
                if not route.is_limit(request=request):
                    route.add_limit(request)
                for include_name, include_type in annotations.items():
                    if include_type == Request:
                        params[include_name] = request
                        has_set.append(include_name.lower())
                    elif include_type == WebSocket and hasattr(request, "websocket"):
                        params[include_name] = request.websocket
                        has_set.append(include_name.lower())
                    elif include_type == RouteLimit:
                        params[include_name] = RouteLimit(request, route)
                        has_set.append(include_name.lower())
                    elif include_type == Form and hasattr(request, "form"):
                        params[include_name] = request.form
                        has_set.append(include_name.lower())
                    else:
                        param_lower = include_name.lower()
                        if hasattr(request, 'json') and param_lower in request.json:
                            params[include_name] = parse_obj_as_type(request.json[param_lower], include_type)
                        elif param_lower in request_params_lower:
                            params[include_name] = request_params_lower[param_lower]
                        elif param_lower in default_params:
                            params[include_name] = default_params[param_lower]
                        elif hasattr(include_type, '__origin__') and include_type.__origin__ is Union and type(None) in get_args(include_type):
                            params[include_name] = None
                        try:
                            params[include_name] = parse_obj_as_type(params[include_name], include_type)
                            has_set.append(param_lower)
                        except:
                            return ErrorResponse.internal_error(request.path)
                if any(value.kind == value.VAR_KEYWORD for value in inspect.signature(handler).parameters.values()):
                    params.update(fixedValue({key: value for key, value in request_params_lower.items() if key not in has_set}))
                websocket = hasattr(request, "websocket")
                result: Any = None
                if websocket:
                    await request.websocket()
                    if inspect.iscoroutinefunction(handler):
                        await handler(**params)
                    else:
                        handler(**params)
                else:
                    try:
                        result = await timings.add_request(request.path, handler, params)
                    except:
                        result = ErrorResponse.internal_error(request.path)
                if not isinstance(result, Response):
                    result = Response(content=result)   
                return await result(request, client)
        if self.resources:
            for key, value in self.resources.items():
                if request.path.startswith(key):
                    return await Response(Path(f".{value}/{request.path.lstrip(key)}"), mount=key)(request, client)
        await Response(b'')(request, client)
    
    def mount(self, path: str, source: str):
        self.resources[fixed_path(path).lower()] = fixed_path(source)
    @staticmethod
    def match_url(target: Route, source: str) -> tuple[bool, dict[str, Any]]:
        params = {}
        if target.params:
            for rp in target.regexp:
                r = re.match(rp, source.lower())
                if r:
                    params = {name: r.group(name) for name in target.param}
        return (source.lower() in target.path or bool(params), params)
    def get(self, path, ratelimit_seconds: float = 0, ratelimit_count: int = -1, ratelimit_func = None):
        return self.route(path, 'GET', ratelimit_seconds=ratelimit_seconds, ratelimit_count=ratelimit_count, ratelimit_func=ratelimit_func)
    def post(self, path, ratelimit_seconds: float = 0, ratelimit_count: int = -1, ratelimit_func = None):
        return self.route(path, 'POST', ratelimit_seconds=ratelimit_seconds, ratelimit_count=ratelimit_count, ratelimit_func=ratelimit_func)
    def delete(self, path, ratelimit_seconds: float = 0, ratelimit_count: int = -1, ratelimit_func = None):
        return self.route(path, 'DELETE', ratelimit_seconds=ratelimit_seconds, ratelimit_count=ratelimit_count, ratelimit_func=ratelimit_func)
    def websocket(self, path, ratelimit_seconds: float = 0, ratelimit_count: int = -1, ratelimit_func = None):
        return self.route(path, 'WebSocket', ratelimit_seconds=ratelimit_seconds, ratelimit_count=ratelimit_count, ratelimit_func=ratelimit_func)

class Cookie:
    def __init__(self, key: str, value: str,
            expiry: Optional[float] = None,
            path: str = "/",
            maxAge: Optional[int] = None) -> None:
        self.key = key
        self.value = value
        self.expiry = expiry + time.time() if expiry else None
        self.path = (path if path else "")
        self.path = self.path if self.path.startswith("/") else "/" + self.path
        self.max_age = maxAge

    def __str__(self) -> str:
        return self.key + '=' + self.value + "; Path=" + self.path + ('; Expires=' + datetime.datetime.utcfromtimestamp(self.expiry).strftime('%a, %d %b %Y %H:%M:%S GMT') if self.expiry else '') + ("; Max-Age" if self.max_age else '')

class RouteLimit:
    def __init__(self, request: 'Request', route: 'Route') -> None:
        self.value = route.get_limit(request)
        self.config = route.ratelimit_configs
    def __str__(self) -> str:
        return str(self.value)

class Request:
    async def __call__(self, data: bytes, client: Client) -> 'Request':
        if data.count(b"\r\n\r\n") == 0:
            data = await client.readuntil(b'\r\n\r\n')
        (a := (b := data.split(b"\r\n\r\n", 1))[0].decode("utf-8").split("\r\n"))[1:]
        self.raw_header = {key: value for key, value in (b.split(": ") for b in a[1:])}
        self.method, self.raw_path = a[0].split(" ")[0:2]
        self.headers = {key.lower(): value for key, value in self.raw_header.items()}
        self.path = urlparse.unquote((url := urlparse.urlparse(self.raw_path)).path).replace("//", "/")
        self.params = {k: v[-1] for k, v in urlparse.parse_qs(url.query).items()}
        self.length = int(self.headers.get("content-length", 0))
        self.content = io.BytesIO()
        self.content.write(b[1])
        self.content.write(await client.read(self.length - self.content.tell()))
        self.address = client.get_ip()
        content_type = self.headers.get("content-type", None)
        self.user_agent = self.headers.get("user-agent", "")
        if 'connection' in self.headers and self.headers['connection'].lower() == "upgrade" and 'upgrade' in self.headers and self.headers['upgrade'].lower() == "websocket" and 'sec-websocket-version' in self.headers and self.headers['sec-websocket-version'] == '13':
            self.websocket = WebSocket(client=client, request=self)
            self.method = "WebSocket"
        if not content_type:
            return self
        if content_type.startswith("application/json"):
            self.json = json.loads(self.content.getvalue())
        elif content_type.startswith("multipart/form-data"):
            self.boundary = content_type.split("; boundary=")[1]
            self.form = Form(self.boundary, self.content)
        return self
    def __str__(self) -> str:
        return str({
            "length": self.length,
            "address": self.address,
            "method": self.method,
            "path": self.raw_path,
            "content-type": self.headers.get("content-type", None)
        })
    def __repr__(self) -> str:
        return self.__str__()

class OPCODE(Enum):
    CONTINUATION = 0x0
    TEXT = 0x1
    BINARY = 0x2
    CLOSE = 0x8
    PING = 0x9
    PONG = 0xA

class WebSocket:
    def __init__(self, client: Client, request: Request) -> None:
        self.client = client
        self.request = request
        self.__close = False
        self.stats = {
            "send": {
                "count": 0,
                "length": 0
            },
            "recv": {
                "count": 0,
                "length": 0
            }
        }
    async def __call__(self) -> Any:
        await Response(headers={
            'Connection': 'Upgrade',
            'Upgrade': 'WebSocket',
            'Sec-WebSocket-Accept': base64.b64encode(hashlib.sha1(self.request.headers['sec-websocket-key'].encode('utf-8') + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest()).decode('utf-8')
        }, status_code=101)(self.request, self.client)
        self.keepalive = asyncio.create_task(self._ping())
        self.last_ping = False
    def is_closed(self, ) -> bool:
        return self.__close
    async def _ping(self, ):
        try:
            while not self.__close and self.last_ping:
                self.last_ping = True
                await self.send(b"", OPCODE.PING)
                await asyncio.sleep(5)
        except:
            await error()
        self.__close = True
        await self.close()
    async def read_frame(self):
        try:
            data = await self.client.readexactly(2)
            head1, head2 = struct.unpack("!BB", data)
            fin  = bool(head1 & 0b10000000)
            mask = (head1 & 0x80) >> 7
            opcode = head1 & 0b00001111
            length = head2 & 0b01111111
            mask_bits = b''
            if length == 126:
                data = await self.client.readexactly(2)
                (length,) = struct.unpack("!H", data)
            elif length == 127:
                data = await self.client.readexactly(8)
                (length,) = struct.unpack("!Q", data)
            if mask:
                mask_bits = await self.client.readexactly(4)
            data = await self.client.readexactly(length)
            if (mask and mask_bits is None) or (mask and mask_bits and len(mask_bits) != 4):
                raise ValueError("mask must contain 4 bytes")
            if mask:
                data = bytes([data[i] ^ mask_bits[i % 4] for i in range(len(data))])
            if opcode == 0x8:  # Close opcode
                self.__close = True
                data = data[2:]
                if length > 1 and data:
                    await error(f"Close reason: {data.decode('utf-8')}")
                return opcode, data
            if not fin:
                c, d = await self.read_frame()
                if c != opcode:
                    raise ValueError("opcode doesn't match {} {}".format(opcode, c))
                data += d
            return opcode, data
        except:
            self.__close = True
            return -1, b''

    async def read(self,) -> bytes:
        if self.__close:
            return b''
        try:
            code, payload = await self.read_frame()
            if code == OPCODE.PONG.value:
                return await self.read()
            if code == OPCODE.PING.value:
                await self.send(payload, OPCODE.PONG)
                return await self.read()
            if not (3 >= code >= 0):
                self.__close = True
                return b''
            self.stats["recv"]["count"] += 1
            self.stats["recv"]["length"] += len(payload)
            return payload
        except:
            self.__close = True
            return b''

    async def send(self, payload: bytes | list | dict | tuple, opcode: OPCODE = OPCODE.TEXT) -> int:
        if self.__close:
            return -1
        if isinstance(payload, (list, dict, tuple)):
            payload = json.dumps(payload).encode("utf-8")
        elif not isinstance(payload, bytes):
            payload = str(payload).encode("utf-8")
        output = io.BytesIO()

        head1 = 0b10000000 | opcode.value
        head2 = 0
        length = len(payload)
        if length < 126:
            output.write(struct.pack("!BB", head1, head2 | length))
        elif length < 65536:
            output.write(struct.pack("!BBH", head1, head2 | 126, length))
        else:
            output.write(struct.pack("!BBQ", head1, head2 | 127, length))
        self.stats["send"]["count"] += 1
        self.stats["send"]["length"] += length
        output.write(payload)
        try:
            self.client.write(output.getvalue())
            if opcode == OPCODE.CLOSE:
                self.__close = True
            return length
        except:
            self.__close = True
            return -1

    async def close(self, payload: Any | tuple = b'') -> int:
        if self.__close:
            return -1
        if isinstance(payload, tuple) and len(payload) == 2:  # If there is a close code and reason
            close_code, close_reason = payload
            payload = struct.pack('!H', close_code) + close_reason.encode('utf-8')  # Pack the close code and reason
        else:
            if isinstance(payload, (list, dict, tuple)):
                payload = json.dumps(payload).encode("utf-8")
            elif not isinstance(payload, bytes):
                payload = str(payload).encode("utf-8")
            payload = str(payload).encode("utf-8")  # Encode the close reason
        return await self.send(payload, OPCODE.CLOSE)
    async def keep(self):
        async for data in self.keepRead():
            if not data:
                break
    async def keepRead(self):
        while data := await self.read():
            if not data:
                break
            yield data
    async def __aiter__(self):
        while data := await self.read():
            if not data:
                break
            yield data

class Response:
    def __init__(self, content: bytes | memoryview | str | None | Path | bool | typing.AsyncIterable | typing.Iterable | io.BytesIO | Any = b'', headers: dict[str, Any] = {}, status_code: int = 200, content_type: str = 'text/plain', **kwargs) -> None:
        self.headers: dict[str, Any] = default_headers.copy()
        self.headers["Date"] = datetime.datetime.fromtimestamp(time.time()).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.set_header(headers)
        self.set_header({
            'Content-Type': content_type
        })
        self.set_content(content)
        self.status_code = status_code
        self.cookies = []
        self.kwargs = kwargs
    def set_header(self, headers: dict[str, Any] = {}):
        tmp_headers_index: dict[str, str] = {key.lower(): key for key in self.headers.keys()}
        for key, value in headers.items():
            if key.lower() not in ("server", "date", "content-length"):
                self.headers[tmp_headers_index.get(key.lower(), key)] = value
        return self
    def set_content(self, content: bytes | memoryview | str | None | Path | bool | typing.AsyncIterator | typing.Iterator | io.BytesIO |Any = b''):
        if isinstance(content, Path):
            content = Path(f"./{str(Path(str(content)))}")
            if not content.exists():
                self.status_code = 404
        elif isinstance(content, typing.Iterator):
            content = iterate_in_threadpool(content)
        self.content = content
    def set_cookie(self, *cookie):
        self.cookies = list(cookie)

    async def __call__(self, request: Request, client: Client) -> Any:
        if client.is_closed():
            return
        async def iter():
            if isinstance(self.content, typing.AsyncIterator):
                async for data in self.content:
                    yield data
            elif isinstance(self.content, Path):
                def format_size(bytes):
                    """Take a size in bytes, and return a human-readable string."""
                    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB', 'YB']
                    index = 0
                    while bytes >= 900 and index < len(units) - 1:
                        bytes /= 1024
                        index += 1
                    return f"{bytes:.2f} {units[index]}"
                def get_directory_size(path):
                    total: int = 0
                    try:
                        with os.scandir(path) as it:
                            for entry in it:
                                if entry.is_file():
                                    total += entry.stat().st_size
                                elif entry.is_dir():
                                    total += get_directory_size(entry.path)
                    except:
                        total = 0
                    return total
                if self.content.exists():
                    if self.content.is_dir():
                        content_type = content_type = list(set([key for key in self.headers.keys() if key.lower() == "content-type"] or ["Content-Type"]))[0]
                        self.headers[content_type] = 'text/html; charset=utf-8'
                        yield '<!DOCTYPE html>\n'
                        yield '<html>'
                        yield '\t<head>'
                        yield '\t\t<meta charset="utf-8"/>'
                        yield '\t<head/>'
                        yield '\t<body>'
                        for path in self.content.iterdir():
                            yield f'\t\t<a href="{self.kwargs.get("mount", "") + str(Path(str(path).replace(path.absolute().drive, "")))}" title="{format_size(path.stat().st_size if path.is_file() else await asyncio.get_event_loop().run_in_executor(None, get_directory_size, path))}">{path.name}</a><br/>'.encode('utf-8')
                        yield '\t<body/>'
                        yield '<html/>'
            elif isinstance(self.content, (list, dict, tuple)) or None:
                try:
                    yield json.dumps(self.content).encode('utf-8')
                except:
                    ...
            elif isinstance(self.content, (str, bool, int, float)):
                yield str(self.content).encode("utf-8")
            yield b''
        content = io.BytesIO()
        length: int = 0
        keepalive: bool = False
        if isinstance(self.content, Path) and self.content.exists() and self.content.is_file():
            length = self.content.stat().st_size
            keepalive = True
        elif isinstance(self.content, io.BytesIO):
            length = self.content.tell()
            keepalive = True
        else:
            try:
                async for data in iter():
                    content.write(data if isinstance(data, bytes) else data.encode("utf-8"))
                length = content.tell()
            except: 
                content = io.BytesIO()
        self.headers["Content-Length"] = length
        tmp_headers = {key.lower(): key for key in self.headers.keys()}
        self.headers[tmp_headers.get("connection", "Connection")] = self.headers.get(tmp_headers.get("connection", "connection"), "Closed")
        start_bytes, end_bytes = 0, 0
        if keepalive:
            content_type = list(set([key for key in self.headers.keys() if key.lower() == "content-type"] or ["Content-Type"]))[0]
            self.headers[content_type] = guess_type(self.content)[0] if isinstance(self.content, Path) and self.content.is_file() else ''
            range_str = request.headers.get('range', '')
            range_match = re.search(r'bytes=(\d+)-(\d+)', range_str, re.S) or re.search(r'bytes=(\d+)-', range_str, re.S)
            end_bytes = length - 1
            length = length - start_bytes if isinstance(self.content, Path) and self.content.is_file() and stat.S_ISREG(self.content.stat().st_mode) else length
            if range_match:
                start_bytes = int(range_match.group(1)) if range_match else 0
                if range_match.lastindex == 2:
                    end_bytes = int(range_match.group(2))
            self.set_header({
                'accept-ranges': 'bytes',
                'connection': 'keep-alive',
                'content-range': f'bytes {start_bytes}-{end_bytes}/{length}'
            })
            self.status_code = 206 if start_bytes > 0 else 200
        set_cookie = '\r\n'.join(("Set-Cookie: " + str(cookie) for cookie in self.cookies))
        if set_cookie:
            set_cookie += '\r\n'
        tmp_header: str = "\r\n".join(f"{k}: {v}" for k, v in self.headers.items())
        await info(self.status_code, request.address, request.raw_path, request.user_agent)
        client.write(f'HTTP/1.1 {self.status_code} {status_codes.get(self.status_code, status_codes.get(self.status_code // 100 * 100))}\r\n{tmp_header}\r\n{set_cookie}\r\n'.encode("utf-8"))
        if isinstance(self.content, Path) and self.content.exists() and self.content.is_file():
            async with aiofiles.open(self.content, "rb") as r:
                await r.seek(start_bytes, os.SEEK_SET)
                while data := await r.read(1024 * 1024 * 8):
                    if not data:
                        break
                    client.write(data)
        else:
            content = self.content if isinstance(self.content, io.BytesIO) else content
            content.seek(start_bytes, os.SEEK_SET)
            client.write(content.getbuffer())
        if keepalive:
            client.set_keepalive_connection(True)

class RequestHandler:
    def __init__(self, seconds=(1, 5, 15, 20), timing = 180):
        self.seconds = seconds
        self.paths = {}
        self.timing = timing

    async def add_request(self, path, handler, params):
        start_time = time.time()
        if inspect.iscoroutinefunction(handler):
            result = await handler(**params)
        else:
            result = handler(**params)
        end_time = time.time()
        if path not in self.paths:
            self.paths[path] = {'timings': {}, 'request_times_second': {second: deque() for second in self.seconds}}
        elapsed_time = end_time - start_time
        self.paths[path]['timings'][end_time] = elapsed_time
        for second in self.paths[path]['request_times_second']:
            self.paths[path]['request_times_second'][second].append(end_time)
        return result

    def get_requests_per_second(self, path):
        self.remove(path)
        return {second: len(times) for second, times in self.paths[path]['request_times_second'].items()}

    def getvalues(self, path):
        self.remove(path)
        return list(self.paths[path]['timings'].values())

    def average(self, path):
        values = self.getvalues(path)
        return sum(values) / len(values) if values else 0

    def max(self, path):
        values = self.getvalues(path)
        return max(values) if values else 0

    def min(self, path):
        values = self.getvalues(path)
        return min(values) if values else 0

    def remove(self, path):
        self.paths[path]['timings'] = {time: elapsed for time, elapsed in self.paths[path]['timings'].items() if time + self.timing >= time.time()}
        for second in self.paths[path]['request_times_second']:
            while self.paths[path]['request_times_second'][second] and time.time() - self.paths[path]['request_times_second'][second][0] > second:
                self.paths[path]['request_times_second'][second].popleft()

    def getAll(self):
        return {path: {
            "per_second": self.get_requests_per_second(path),
            "average": self.average(path),
            "max": self.max(path),
            "min": self.min(path)
        } for path in list(self.paths.keys())}

class ErrorResponse:
    @staticmethod
    def generate_error(path: str, description: str, **kwargs) -> dict:
        error = {
            'path': path,
            'description': description,
        }
        error.update(**kwargs)
        return error

    @staticmethod
    def missing_parameter(path: str, param: str, **kwargs) -> dict:
        return ErrorResponse.generate_error(path, f'missing.parameter.{param}', kwargs=kwargs)

    @staticmethod
    def invalid_parameter(path: str, param: str, **kwargs) -> dict:
        return ErrorResponse.generate_error(path, f'invalid.parameter.{param}', kwargs=kwargs)

    @staticmethod
    def wrong_type_parameter(path: str, param: str, expected_type: str, **kwargs) -> dict:
        return ErrorResponse.generate_error(path, f'wrong.type.parameter.{param}.expected.{"integer" if expected_type == "int" else ("string" if expected_type == "str" else expected_type)}', kwargs=kwargs)

    @staticmethod
    def not_found(path: str, **kwargs) -> dict:
        return ErrorResponse.generate_error(path, "not.found", kwargs=kwargs)

    @staticmethod
    def internal_error(path: str, **kwargs) -> dict:
        format_exc = traceback.format_exc()
        asyncio.get_event_loop().run_until_complete(error(format_exc))
        return ErrorResponse.generate_error(path, "internal.error", details=format_exc, kwargs=kwargs)

class Form:
    def __init__(self, boundary: str, payload: io.BytesIO):
        self.boundary = boundary
        self.payload = payload
        self.fields = {}
        self.files = {}
        self.parse_multipart_form_data()

    def parse_multipart_form_data(self):
        parts = self.payload.getvalue().split(b'--' + self.boundary.encode("utf-8"))
        for part in parts[1:-1]:
            headers, body = part.split(b"\r\n\r\n", 1)
            headers, body = {key: value for key, value in ((a.groupdict()['key'], a.groupdict()['value']) for a in re.finditer(r'(?P<key>\w+)="(?P<value>[^"\\]*(\\.[^"\\]*)*)"', headers.decode("utf-8")))}, body[:-2]
            if 'filename' in headers:
                if headers['filename'] not in self.files:
                    self.files[headers['filename']] = []
                self.files[headers['filename']].append(io.BytesIO(body))
            else:
                if headers['name'] not in self.fields:
                    self.fields[headers['name']] = []
                self.fields[headers['name']].append(io.BytesIO(body))
default_headers: dict[str, Any] = {
    'Server': 'TTBServer'
}

status_codes: dict[int, str] = {
    100: "Continue",
    101: "Switching Protocols",
    200: "OK",
    201: "Created",
    202: "Accepted",
    203: "Non-Authoritative Information",
    204: "No Content",
    205: "Reset Content",
    206: "Partial Content",
    300: "Multiple Choices",
    301: "Moved Pemanently",
    302: "Found",
    303: "See Other",
    304: "Not Modified",
    305: "Use Proxy",
    306: "Unused",
    307: "Temporary Redirect",
    400: "Bad Request",
    401: "Unauthorized",
    402: "Payment Required",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    407: "Proxy Authentication Required",
    408: "Request Time-out",
    409: "Conflict",
    410: "Gone",
    411: "Length Required",
    412: "Precondition Failed",
    413: "Request Entity Too Large",
    414: "Request-URI Too Large",
    415: "Unsupported Media Type",
    416: "Requested range not satisfiable",
    417: "Expectation Failed",
    418: "I'm a teapot",
    421: "Misdirected Request",
    422: "Unprocessable Entity",
    423: "Locked",
    424: "Failed Dependency",
    425: "Too Early",
    426: "Upgrade Required",
    428: "Precondition Required",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    451: "Unavailable For Legal Reasons",
    500: "Internal Server Eror",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Time-out",
    505: "HTTP Version not supported",
}
T = typing.TypeVar("T")
application: Optional['Application'] = None
timings = RequestHandler()
def default_limit(request: 'Request', route: 'RouteLimit'):
    return {
        "path": request.raw_path,
        "description": "ratelimit",
        "pastInSeconds": route.config['seconds'],
        "pastInRequests": route.config['count'],
    }
def _next(iterator: typing.Iterator):
    # We can't raise `StopIteration` from within the threadpool iterator
    # and catch it outside that context, so we coerce them into a different
    # exception type.
    try:
        return next(iterator)
    except StopIteration:
        raise _StopIteration
async def iterate_in_threadpool(iterator: typing.Iterator) -> typing.AsyncIterator:
    while True:
        try:
            yield await anyio.to_thread.run_sync(_next, iterator) # type: ignore
        except _StopIteration:
            break
async def handle(data: bytes, client: Client):
    request: Request = await Request()(data, client)
    if not application:
        await Response('The web server is not initization')(request, client)
    else:
        await application.handle(request, client)
def fixed_path(url: str):
    url = url.replace("//", "/").rstrip("/")
    if not url.startswith("/"):
        return f'/{url}'
    return url
