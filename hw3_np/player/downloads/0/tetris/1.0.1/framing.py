# common/framing.py
import struct, json, asyncio

MAX_LEN = 65536

def pack_json(obj: dict) -> bytes:
    body = json.dumps(obj, separators=(',', ':')).encode('utf-8')
    n = len(body)
    if not (0 < n <= MAX_LEN):
        raise ValueError("invalid length")
    return struct.pack('!I', n) + body

async def read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = await reader.read(n - len(buf))
        if not chunk:
            # 對端關閉 → 視為正常離線，交由上層決定是否記錄
            raise ConnectionError("socket closed")
        buf.extend(chunk)
    return bytes(buf)

async def recv_json(reader: asyncio.StreamReader) -> dict:
    hdr = await read_exactly(reader, 4)
    (length,) = struct.unpack('!I', hdr)
    if not (0 < length <= MAX_LEN):
        raise ConnectionError("length out of range")
    body = await read_exactly(reader, length)
    try:
        return json.loads(body.decode('utf-8'))
    except Exception as e:
        raise ConnectionError(f"bad json: {e}")

async def send_json(writer: asyncio.StreamWriter, obj: dict):
    writer.write(pack_json(obj))
    await writer.drain()
