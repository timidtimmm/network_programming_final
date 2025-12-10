# server/utils.py - 自動偵測公網 IP

import socket
import urllib.request
import json

def get_public_ip():
    """
    自動偵測伺服器的公網 IP
    方法 1: 使用外部服務查詢
    方法 2: 使用 socket 連線測試（僅獲取本地 IP）
    """
    # 方法 1: 查詢外部服務（推薦）
    try:
        with urllib.request.urlopen('https://api.ipify.org?format=json', timeout=5) as response:
            data = json.loads(response.read().decode())
            return data['ip']
    except:
        pass
    
    # 方法 2: 備用 - 使用 ifconfig.me
    try:
        with urllib.request.urlopen('https://ifconfig.me/ip', timeout=5) as response:
            return response.read().decode().strip()
    except:
        pass
    
    # 方法 3: 獲取本地 IP（僅內網）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except:
        pass
    
    return "127.0.0.1"

def get_server_host(config_host=None, fallback="127.0.0.1"):
    """
    獲取伺服器 IP
    優先順序：config 設定 > 自動偵測 > fallback
    """
    if config_host and config_host not in ("0.0.0.0", "127.0.0.1", "localhost"):
        return config_host
    
    detected = get_public_ip()
    print(f"[Utils] Auto-detected public IP: {detected}")
    
    if detected not in ("127.0.0.1", "0.0.0.0"):
        return detected
    
    return fallback

# 使用範例：
# from utils import get_server_host
# PUBLIC_HOST = get_server_host(CONF.get("public_host"))