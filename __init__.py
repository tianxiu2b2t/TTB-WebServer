import asyncio
from dataclasses import dataclass
from enum import Enum
import time
import traceback
from typing import Any, Coroutine, Optional

from utils import Client
import web

class ProtocolHeader(Enum):
    HTTP = 'HTTP'
    NONE = 'Unknown'
    @staticmethod
    def from_data(data: bytes):
        if b'HTTP/1.1\r\n' in data or b'HTTP/1.0\r\n' in data:
            return ProtocolHeader.HTTP
        return ProtocolHeader.NONE
    def __str__(self) -> str:
        return self.value
    def __repr__(self) -> str:
        return self.value
port_: int = 34901
protocol_handler: dict[ProtocolHeader, Any] = {}
protocol_startup: dict[ProtocolHeader, Any] = {}
protocol_shutdown: dict[ProtocolHeader, Any] = {}
connections: int = 0
async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    global protocol_handler, connections
    connections += 1
    client = Client(reader=reader, writer=writer)
    protocol: Optional[ProtocolHeader] = None
    try:
        while data := await client.read(8192):
            if not data:
                break
            if not protocol:
                protocol = ProtocolHeader.from_data(data)
            if protocol_handler.get(protocol) != None:
                await protocol_handler[protocol](data, client)
            if not client.keepalive_connection:
                break
    except:
        ...
    client.close()
    connections -= 1

async def start_():
    global protocol_shutdown, protocol_startup
    server = await asyncio.start_server(handle, port=port_)
    [await startup() for startup in protocol_startup.values() if startup]
    await server.serve_forever()
    [await shutdown() for shutdown in protocol_shutdown.values() if shutdown]

def start():
    asyncio.run(start_())

def set_port(port: int):
    global port_
    port_ = port
    
def set_protocol_handler(protocol: ProtocolHeader, handler: Any):
    global protocol_handler
    protocol_handler[protocol] = handler

def set_protocol_startup(protocol: ProtocolHeader, startup: Any):
    global protocol_startup
    protocol_startup[protocol] = startup

def set_protocol_shutdown(protocol: ProtocolHeader, shutdown: Any):
    global protocol_shutdown
    protocol_shutdown[protocol] = shutdown

if __name__ == "__main__":
    set_protocol_handler(ProtocolHeader.HTTP, web.handle)
    if web.application:
        set_protocol_startup(ProtocolHeader.HTTP, web.application.start)
    start()