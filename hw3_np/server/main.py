# server/main.py - 修正 IP 偵測與連線問題
import json, asyncio, threading, urllib.request, socket, re
from pathlib import Path
from dev_server import serve as serve_dev_sync
from lobby_server import serve as serve_lobby_sync

ROOT = Path(__file__).resolve().parents[1]
CONF = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
RUNTIME_FILE = ROOT / "server" / "runtime_ports.json"

def is_valid_ipv4(ip):
    """驗證是否為有效的 IPv4 地址"""
    pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
    if not re.match(pattern, ip):
        return False
    parts = ip.split('.')
    return all(0 <= int(part) <= 255 for part in parts)
def is_local_ip(ip: str) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((ip, 0))
        s.close()
        return True
    except OSError:
        return False

def pick_public_ip_from_list(conf):
    manual = (conf.get("public_host") or "").strip()
    if manual and is_valid_ipv4(manual):
        return manual

    for ip in (conf.get("public_hosts") or []):
        if is_valid_ipv4(ip) and is_local_ip(ip):
            return ip

    return None

def get_public_ip():
    """自動偵測公網 IP，並驗證結果"""
    
    # 方法 1: 使用 ipify.org
    try:
        with urllib.request.urlopen('https://api.ipify.org?format=text', timeout=5) as response:
            ip = response.read().decode().strip()
            if is_valid_ipv4(ip):
                print(f"[Main] ✅ 偵測到公網 IP: {ip}")
                return ip
    except Exception as e:
        print(f"[Main] ⚠️  ipify 查詢失敗: {e}")
    
    # 方法 2: 使用 ifconfig.me
    try:
        with urllib.request.urlopen('https://ifconfig.me/ip', timeout=5) as response:
            ip = response.read().decode().strip()
            if is_valid_ipv4(ip):
                print(f"[Main] ✅ 偵測到公網 IP: {ip}")
                return ip
    except Exception as e:
        print(f"[Main] ⚠️  ifconfig.me 查詢失敗: {e}")
    
    # 方法 3: 獲取本地 IP（內網）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if is_valid_ipv4(ip):
            print(f"[Main] ✅ 偵測到本地 IP: {ip}")
            return ip
    except Exception as e:
        print(f"[Main] ⚠️  本地 IP 偵測失敗: {e}")
    
    print("[Main] ⚠️  IP 偵測失敗，使用 127.0.0.1")
    return "127.0.0.1"

def _pick_free_port(min_port=10000, max_port=65535):
    """在指定範圍內尋找可用 port"""
    import random
    
    for _ in range(100):
        port = random.randint(min_port, max_port)
        try:
            s = socket.socket()
            s.bind(("0.0.0.0", port))
            actual_port = s.getsockname()[1]
            s.close()
            return actual_port
        except OSError:
            continue
    
    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    
    if port < min_port:
        return _pick_free_port(min_port, max_port)
    
    return port

async def run_dev_server(host, port, stop_event):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, serve_dev_sync, host, port, stop_event)

async def run_lobby_server(host, port, stop_event):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, serve_lobby_sync, host, port, stop_event)

async def main():
    runtime_data = {}
    if RUNTIME_FILE.exists():
        try:
            runtime_data = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
        except:
            runtime_data = {}
    
    # 動態分配 port
    dev_port = runtime_data.get("developer_port")
    lobby_port = runtime_data.get("lobby_port")
    
    if not dev_port:
        dev_port = _pick_free_port(10000, 65535)
    if not lobby_port:
        lobby_port = _pick_free_port(10000, 65535)
    
    if dev_port == lobby_port:
        lobby_port = _pick_free_port(10000, 65535)

    picked = pick_public_ip_from_list(CONF)
    if picked:
        public_ip = picked
        print(f"[Main] ✅ public_hosts 命中本機 IP: {public_ip}")
    else:
        # 真的找不到才用你原本的外網/route 偵測
        config_public = CONF.get("public_host") or CONF.get("lobby_endpoint", {}).get("public_host")
        if config_public and config_public not in ("0.0.0.0", "127.0.0.1") and is_valid_ipv4(config_public):
            public_ip = config_public
            print(f"[Main] 使用 config.json 設定的公網 IP: {public_ip}")
        else:
            public_ip = get_public_ip()


    # ✅ 如果是在本機測試，使用 127.0.0.1
    if public_ip.startswith("192.168.") or public_ip.startswith("10.") or public_ip.startswith("172."):
        print(f"[Main] ⚠️  偵測到內網 IP: {public_ip}")
        print(f"[Main] 如果 Client 在同一台機器，將使用 127.0.0.1")
        public_ip = "127.0.0.1"

    # Server 綁定的 IP（接受所有來源）
    host_dev = CONF.get("developer_endpoint", {}).get("host", "0.0.0.0")
    host_lobby = CONF.get("lobby_endpoint", {}).get("host", "0.0.0.0")

    # 保存 runtime ports
    runtime_data = {
        "developer_port": dev_port, 
        "lobby_port": lobby_port,
        "dev_host": public_ip,
        "lobby_host": public_ip
    }
    RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_FILE.write_text(json.dumps(runtime_data, indent=2), encoding="utf-8")

    print("\n" + "="*60)
    print("[Main] DeveloperServer")
    print(f"       綁定地址: {host_dev}:{dev_port}")
    print(f"       Client 連線: {public_ip}:{dev_port}")
    print("[Main] LobbyServer")
    print(f"       綁定地址: {host_lobby}:{lobby_port}")
    print(f"       Client 連線: {public_ip}:{lobby_port}")
    print(f"[Main] 配置已儲存: {RUNTIME_FILE}")
    print("="*60)
    
    if public_ip == "127.0.0.1":
        print("\n✅ 本機測試模式")
        print("   Server 和 Client 必須在同一台機器上執行")
    else:
        print(f"\n⚠️  遠端連線模式 (IP: {public_ip})")
        print(f"   請確保防火牆已開放 port {dev_port}, {lobby_port}, 10000-65535")
    
    print("\n按 Ctrl+C 停止伺服器\n")

    stop_event = threading.Event()

    try:
        await asyncio.gather(
            run_dev_server(host_dev, dev_port, stop_event),
            run_lobby_server(host_lobby, lobby_port, stop_event),
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\n[Main] Ctrl+C detected, stopping servers...")
        stop_event.set()
        await asyncio.sleep(1.0)
    finally:
        print("[Main] Bye.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Main] KeyboardInterrupt at top level. Exit.")
