import asyncio
from dataclasses import dataclass
from enum import Enum
import inspect
from pathlib import Path
import socket
import time
from typing import Any, Iterable, Type, Union, get_args
import traceback as traceback_
import aiofiles
from rich.console import Console
from rich.text import Text

@dataclass
class Client:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    keepalive_connection: bool = False
    async def readline(self):
        return await self.reader.readline()

    async def readuntil(self, separator: bytes | bytearray | memoryview = b"\n"):
        return await self.reader.readuntil(separator=separator)

    async def read(self, n: int = -1):
        return await self.reader.read(n)

    async def readexactly(self, n: int):
        return await self.reader.readexactly(n)

    def __aiter__(self):
        return self.reader.__aiter__()

    async def __anext__(self):
        return await self.reader.__anext__()
    
    def get_address(self):
        return self.writer.get_extra_info("peername")[:2]
    
    def get_ip(self):
        return self.get_address()[0]
    def get_port(self):
        return self.get_address()[1]
    
    def write(self, data: bytes | bytearray | memoryview):
        if self.is_closed():
            return None
        try:
            return self.writer.write(data)
        except:
            self.close()
        return None
    
    def writelines(self, data: Iterable[bytes | bytearray | memoryview]):
        return self.writer.writelines(data)
    
    def set_keepalive_connection(self, value: bool):
        self.keepalive_connection = value

    def close(self):
        return self.writer.close()
    def is_closed(self):
        return self.writer.is_closing()


def parse_obj_as_type(obj: Any, type_: Type[Any]) -> Any:
    if obj is None:
        return obj
    origin = getattr(type_, '__origin__', None)
    args = get_args(type_)
    if origin == Union:
        for arg in args:
            try:
                return parse_obj_as_type(obj, getattr(arg, '__origin__', arg))
            except:
                ...
        return None
    elif origin == dict:
        for arg in args:
            try:
                return parse_obj_as_type(obj, getattr(arg, '__origin__', arg))
            except:
                ...
        return load_params(obj, origin)
    elif origin == inspect._empty:
        return None
    elif origin == list:
        for arg in args:
            try:
                return [parse_obj_as_type(o, getattr(arg, '__origin__', arg)) for o in obj]
            except:
                ...
        return []
    elif origin is not None:
        return origin(obj)
    else:
        for arg in args:
            try:
                return parse_obj_as_type(obj, getattr(arg, '__origin__', arg))
            except:
                return arg(**load_params(obj, type_))
    try:
        return type_(**load_params(obj, type_))
    except:
        try:
            return load_params(obj, type_)
        except:
            return type_(obj)

def load_params(data: Any, type_: Any):
    value = {name: (value.default if value.default is not inspect._empty else None) for name, value in inspect.signature(type_).parameters.items() if not isinstance(value, inspect._empty)}
    if isinstance(data, dict):
        for k, v in data.items():
            value[k] = v
        return value
    else:
        return data

def fixedValue(data: dict[str, Any]):
    for key, value in data.items():
        if value.lower() == 'true':
            data[key] = True
        elif value.lower() == 'false':
            data[key] = False
        elif value.isdigit():
            data[key] = int(value)
        else:
            try:
                data[key] = float(value)
            except ValueError:
                pass
    return data

class TTLCache:
    def __init__(self, maxsize=1024, ttl=600):
        self.maxsize = maxsize
        self.ttl = ttl
        self.cache = {}
        self.timings = {}
    def __delitem__(self, __key: Any) -> None:
        del self.cache[__key]
        del self.timings[__key]
    def check(self, key: Any):
        if key in self.timings and time.time() - self.timings[key] > self.ttl:
            del self.cache[key]
            del self.timings[key]
    def __contains__(self, __key: object) -> bool:
        self.check(__key)
        return __key in self.cache
    def __getitem__(self, key) -> Any:
        self.check(key)
        return self.cache.get(key)

    def __setitem__(self, key, value) -> Any:
        if len(self.cache) >= self.maxsize:
            oldest_key = min(self.timings.keys(), key=lambda k:self.timings[k])
            del self.cache[oldest_key]
            del self.timings[oldest_key]
        self.cache[key] = value
        self.timings[key] = time.time()
        return value

cache_ip: TTLCache = TTLCache(maxsize=1024, ttl=300)
def get_domain_to_ipv4(addr: str) -> str:
    if addr in cache_ip:
        return cache_ip[addr]
    address = socket.gethostbyname(addr)
    if not address:
        return '127.0.0.1'
    cache_ip[addr] = address
    return cache_ip[addr]

mime_types = {
    "aac": "audio/aac",
    "abw": "application/x-abiword",
    "arc": "application/x-freearc",
    "avi": "video/x-msvideo",
    "bin": "application/octet-stream",
    "bmp": "image/bmp",
    "bz2": "application/x-bzip2",
    "css": "text/css",
    "csv": "text/csv",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "gif": "image/gif",
    "html": "text/html",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "json": "application/json",
    "mp3": "audio/mp3",
    "mp4": "video/mp4",
    "png": "image/png",
    "pdf": "application/pdf",
    "js": "javascript",
    "htm": "text/html",
    "svg": "image/svg+xml"
}
def getSuffix(text: str):
    return text.split(".")[-1] if '.' in (text := Path(text).name) else text
def get_media(filename):
    return mime_types.get(getSuffix(filename), "application/octet-stream")

class ChatColor(Enum):
    BLACK = {
        "code": "0",
        "name": "black",
        "color": 0
    }
    DARK_BLUE = {
        "code": "1",
        "name": "dark_blue",
        "color": 170
    }
    DARK_GREEN = {
        "code": "2",
        "name": "dark_green",
        "color": 43520
    }
    DARK_AQUA = {
        "code": "3",
        "name": "dark_aqua",
        "color": 43690
    }
    DARK_RED = {
        "code": "4",
        "name": "dark_red",
        "color": 11141120
    }
    DARK_PURPLE = {
        "code": "5",
        "name": "dark_purple",
        "color": 11141290
    }
    GOLD = {
        "code": "6",
        "name": "gold",
        "color": 16755200
    }
    GRAY = {
        "code": "7",
        "name": "gray",
        "color": 11184810
    }
    DARK_GRAY = {
        "code": "8",
        "name": "dark_gray",
        "color": 5592405
    }
    BLUE = {
        "code": "9",
        "name": "blue",
        "color": 5592575
    }
    GREEN = {
        "code": "a",
        "name": "green",
        "color": 5635925
    }
    AQUA = {
        "code": "b",
        "name": "aqua",
        "color": 5636095
    }
    RED = {
        "code": "c",
        "name": "red",
        "color": 16733525
    }
    LIGHT_PURPLE = {
        "code": "d",
        "name": "light_purple",
        "color": 16733695
    }
    YELLOW = {
        "code": "e",
        "name": "gray",
        "color": 16777045
    }
    WHITE = {
        "code": "f",
        "name": "white",
        "color": 16777215
    }
    @staticmethod
    def getAllowcateCodes():
        code: str = ""
        for value in list(ChatColor):
            code += value.value["code"]
        return code
    @staticmethod
    def getByChatToHex(code: str):
        if len(code) != 1: return "000000"
        for value in list(ChatColor):
            if code == value.value["code"]:
                return f"{value.value['color']:06X}"
        return "000000"
    @staticmethod
    def getByChatToName(code: str):
        if len(code) != 1: return "black"
        for value in list(ChatColor):
            if code == value.value["code"]:
                return value.value["name"]
        return "black"


messages: list[Text] = []
__all__ = [
    'logger',
    'warn',
    'info',
    'debug',
    'error',
    'traceback',
    'get_media',
    'getSuffix',
    'ChatColor',
    'get_domain_to_ipv4',
    'parse_obj_as_type',
    'load_params',
    'fixedValue',
    'Client'
]

console = Console(color_system='auto')
Force = True

async def logger(*message, level: int = 0, force = False):
    global Force
    if Force or force:
        datetime: time.struct_time = time.localtime()
        msg: Text = await formatColor(f"§{(await getLevelColor(level)).value['code']}[{datetime.tm_year:04d}-{datetime.tm_mon:02d}-{datetime.tm_mday:02d} {datetime.tm_hour:02d}:{datetime.tm_min:02d}:{datetime.tm_sec:02d}] [{await getLevel(level)}] " + ' '.join([str(msg) for msg in message]))
        console.print(msg)
        async with aiofiles.open(Path("/logs.log"), "a") as w:
            await w.write(str(msg) + "\n")

async def warn(*message):
    return await logger(*message, level = 1)

async def info(*message):
    return await logger(*message, level = 0)

async def error(*message):
    return await logger(*message, level = 2, force=True)

async def traceback():
    return await error(traceback_.format_exc())

async def debug(*message):
    return await logger(*message, level = 3)

async def formatColor(message: str) -> Text:
    text: Text = Text("")
    temp: str
    start: int = 0
    while (start := message.find("§", start)) != -1:
        if (start + 1) > len(message): break
        if message.find("§", start + 1) != -1:
            temp = message[start + 2 : message.index("§", start + 1)]
        else:
            temp = message[start + 2:]
        text.append(temp, style=f"{ChatColor.getByChatToName(message[start + 1 : start + 2])}")
        start += 1
    return text

async def getLevel(level: int = 0):
    match (level):
        case 0:
            return "INFO"
        case 1:
            return "WARN"
        case 2:
            return "ERROR"
        case 3:
            return "DEBUG"
        case _:
            return "LOGGER"

async def getLevelColor(level: int = 0):
    match (level):
        case 0:
            return ChatColor.GREEN
        case 1:
            return ChatColor.YELLOW
        case 2:
            return ChatColor.RED
        case _:
            return ChatColor.WHITE