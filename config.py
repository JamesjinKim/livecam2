# config.py - 설정 파일 (단순 버전)
import os
from pathlib import Path

# 기본 경로
BASE_DIR = Path(__file__).parent
STREAM_DIR = Path("/tmp/stream")
LOGS_DIR = BASE_DIR / "logs"

# 디렉토리 생성
STREAM_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# 카메라 기본 설정
CAMERA_DEFAULTS = {
    "width": 640,
    "height": 480,
    "framerate": 30,
    "quality": 26,
    "preset": "ultrafast"
}

# 성능 설정
PERFORMANCE = {
    "max_connections": 12,
    "hls_segment_time": 2,
    "hls_playlist_size": 3,
    "update_interval": 5
}

# 보호 임계값
PROTECTION = {
    "cpu_percent": 80.0,
    "cpu_temp": 70.0,
    "memory_percent": 80.0
}

# 네트워크 설정
def get_port():
    """실행 권한에 따라 포트 자동 결정"""
    try:
        return 80 if os.geteuid() == 0 else 8000
    except:
        return 8000

NETWORK = {
    "host": "0.0.0.0",
    "port": get_port()
}

# 카메라 이름
CAMERA_NAMES = {
    0: "카메라 0",
    1: "카메라 1"
}