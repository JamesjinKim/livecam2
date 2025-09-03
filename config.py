# config.py - FastAPI 단독 설정 파일 (이모지 제거 버전)
import os
from pathlib import Path

# 기본 경로
BASE_DIR = Path(__file__).parent
STREAM_DIR = Path("/tmp/stream")  # tmpfs 사용으로 I/O 최적화
LOGS_DIR = BASE_DIR / "logs"

# 디렉토리 생성
STREAM_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# FastAPI 단독 최적화 설정
FASTAPI_CONFIG = {
    "title": "Smart Toggle Camera Server",
    "version": "1.0.0",
    "description": "라즈베리파이 5 FastAPI 단독 스마트 토글 카메라",
    "docs_url": None,      # Swagger UI 비활성화 (성능 최적화)
    "redoc_url": None,     # ReDoc 비활성화
    "openapi_url": None    # OpenAPI 스키마 비활성화 (추가 최적화)
}

# 성능 설정
PERFORMANCE_CONFIG = {
    "max_connections": 12,        # 안전한 동시 접속자 수
    "connection_timeout": 60,     # 연결 타임아웃
    "keepalive_timeout": 30,      # 연결 유지 시간
    "max_request_size": 1024      # 요청 크기 제한 (KB)
}

# 카메라 설정 (안정성 우선)
CAMERA_CONFIG = {
    "default_width": 640,
    "default_height": 480,
    "default_framerate": 30,
    "default_quality": 26,        # CRF 값 (낮을수록 고품질)
    "default_preset": "ultrafast" # 인코딩 속도 우선
}

# HLS 설정 (메모리/성능 최적화)
HLS_CONFIG = {
    "segment_time": 2,            # 세그먼트 길이 (초)
    "playlist_size": 3,           # 플레이리스트 크기 (메모리 절약)
    "delete_segments": True,      # 오래된 세그먼트 자동 삭제
    "keyframe_interval": 30       # 키프레임 간격
}

# 시스템 보호 임계값
PROTECTION_THRESHOLDS = {
    "cpu_percent": 80.0,          # CPU 80% 초과 시 보호
    "cpu_temp": 70.0,             # 70도 초과 시 보호
    "memory_percent": 80.0,       # 메모리 80% 초과 시 보호
    "max_connections": 15         # 최대 연결 수
}

# 모니터링 설정
MONITORING_CONFIG = {
    "update_interval": 5,         # 상태 업데이트 간격 (초)
    "health_check_enabled": True, # 헬스 체크 활성화
    "log_level": "INFO",          # 로그 레벨
    "log_rotation_size": "10MB",  # 로그 회전 크기
    "log_retention_days": 7       # 로그 보관 일수
}

# CORS 설정
CORS_CONFIG = {
    "allow_origins": ["*"],       # 프로덕션에서는 구체적으로 설정
    "allow_credentials": True,
    "allow_methods": ["GET", "POST"],
    "allow_headers": ["*"]
}

# 네트워크 설정
NETWORK_CONFIG = {
    "host": "0.0.0.0",           # 모든 인터페이스에서 접근
    "port": 80,                   # 기본 HTTP 포트 (root 권한 필요)
    "fallback_port": 8000,        # 백업 포트
    "workers": 1,                 # 단일 워커 (리소스 절약)
    "reload": False               # 프로덕션에서는 비활성화
}

# 보안 설정
SECURITY_CONFIG = {
    "allowed_hosts": ["*"],       # 프로덕션에서는 구체적으로 설정
    "max_request_body_size": 1024 * 1024,  # 1MB
    "rate_limit_enabled": False,  # 단순화를 위해 비활성화
    "csrf_protection": False      # API 서버이므로 비활성화
}

# 카메라 별명
CAMERA_NAMES = {
    0: "카메라 1",
    1: "카메라 2"
}

# 환경 설정
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
DEBUG = os.getenv("DEBUG", "False").lower() == "true"

# 포트 자동 감지
def get_server_port():
    """실행 권한에 따라 포트 결정"""
    try:
        # root 권한 확인
        if os.geteuid() == 0:
            return NETWORK_CONFIG["port"]  # 80번 포트
        else:
            return NETWORK_CONFIG["fallback_port"]  # 8000번 포트
    except AttributeError:
        # Windows 등에서는 geteuid() 없음
        return NETWORK_CONFIG["fallback_port"]

# 런타임 설정
RUNTIME_CONFIG = {
    "server_port": get_server_port(),
    "tmpfs_enabled": STREAM_DIR.as_posix().startswith('/tmp'),
    "performance_mode": not DEBUG
}

# Docker 설정 (선택사항)
DOCKER_CONFIG = {
    "enabled": False,             # Docker 사용 여부
    "image_name": "fastapi-camera",
    "container_name": "camera-server",
    "restart_policy": "unless-stopped"
}

# 로깅 설정
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "[%(asctime)s] %(levelname)s - %(name)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S"
        },
        "detailed": {
            "format": "[%(asctime)s] %(levelname)s - %(name)s:%(lineno)d - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S"
        }
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "default"
        },
        "file": {
            "level": "INFO", 
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "detailed",
            "filename": LOGS_DIR / "camera_server.log",
            "maxBytes": 10 * 1024 * 1024,  # 10MB
            "backupCount": 5
        }
    },
    "loggers": {
        "uvicorn": {"level": "INFO"},
        "fastapi": {"level": "INFO"},
        "websockets": {"level": "WARNING"}  # WebSocket 로그 줄이기
    },
    "root": {
        "level": "INFO",
        "handlers": ["console", "file"]
    }
}

# 추가 유틸리티 설정
UTILITY_CONFIG = {
    "auto_cleanup_enabled": True,     # 자동 정리 활성화
    "cleanup_interval": 300,          # 5분마다 정리
    "max_log_files": 10,             # 최대 로그 파일 수
    "debug_mode_timeout": 3600,       # 디버그 모드 타임아웃 (1시간)
    "system_info_update_interval": 10 # 시스템 정보 업데이트 간격
}