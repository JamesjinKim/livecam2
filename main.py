# main.py - FastAPI 단독 스마트 토글 카메라 스트리밍 서버 (이모지 제거 버전)
import asyncio
import json
import subprocess
import psutil
import logging
import signal
import mimetypes
import os
from typing import Dict, Optional
from datetime import datetime
from pathlib import Path
from enum import Enum

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('streaming.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 상태 enum
class CameraState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    SWITCHING = "switching"
    ERROR = "error"

class SystemState(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"

# 데이터 모델
class CameraConfig(BaseModel):
    camera_id: int
    width: int = 640
    height: int = 480
    framerate: int = 30
    quality: int = 26
    preset: str = "ultrafast"

class SystemStatus(BaseModel):
    cpu_percent: float
    cpu_temp: float
    memory_percent: float
    disk_percent: float
    uptime: str
    state: SystemState
    timestamp: datetime

class SmartToggleStatus(BaseModel):
    active_camera: Optional[int]
    camera_state: CameraState
    switch_progress: int
    switch_message: str
    last_switch_time: Optional[datetime]
    system_protected: bool

# 전역 설정
STREAM_BASE_PATH = Path("/tmp/stream")  # tmpfs 사용으로 I/O 성능 향상
STREAM_BASE_PATH.mkdir(parents=True, exist_ok=True)

# 성능 설정
MAX_CONNECTIONS = 12  # 안전한 동시 접속자 수
HLS_SEGMENT_TIME = 2  # 세그먼트 길이
HLS_PLAYLIST_SIZE = 3  # 플레이리스트 크기 (메모리 절약)

# 스마트 토글 매니저
class SmartToggleManager:
    def __init__(self):
        self.active_camera: Optional[int] = None
        self.camera_state = CameraState.STOPPED
        self.processes: Dict[str, subprocess.Popen] = {}
        self.switch_lock = asyncio.Lock()
        self.switch_progress = 0
        self.switch_message = "대기 중"
        self.last_switch_time: Optional[datetime] = None
        self.system_protected = False
        self.active_connections = 0
        
        # 보호 임계값
        self.protection_thresholds = {
            'cpu': 80.0,      # CPU 80% 초과 시 보호
            'temp': 70.0,     # 70도 초과 시 보호
            'memory': 80.0,   # 메모리 80% 초과 시 보호
            'connections': MAX_CONNECTIONS
        }

    async def smart_switch_camera(self, target_camera: int, config: CameraConfig) -> bool:
        """스마트 카메라 전환"""
        async with self.switch_lock:
            try:
                logger.info(f"Starting camera switch to {target_camera}")
                
                self.camera_state = CameraState.SWITCHING
                self.switch_progress = 0
                self.switch_message = f"카메라 {target_camera}로 전환 중..."
                
                # 1단계: 기존 카메라 안전 정지
                if self.active_camera is not None:
                    self.switch_message = "기존 카메라 정리 중..."
                    success = await self._safe_stop_current_camera()
                    if not success:
                        self.camera_state = CameraState.ERROR
                        self.switch_message = "기존 카메라 정지 실패"
                        return False
                    
                    self.switch_progress = 40
                    await asyncio.sleep(1)  # 리소스 정리 대기
                
                # 2단계: 새 카메라 시작
                self.switch_message = f"카메라 {target_camera} 시작 중..."
                self.switch_progress = 50
                
                success = await self._safe_start_camera(target_camera, config)
                
                if success:
                    self.active_camera = target_camera
                    self.camera_state = CameraState.RUNNING
                    self.switch_progress = 100
                    self.switch_message = f"카메라 {target_camera} 활성화 완료"
                    self.last_switch_time = datetime.now()
                    
                    logger.info(f"Successfully switched to camera {target_camera}")
                    return True
                else:
                    self.camera_state = CameraState.ERROR
                    self.switch_message = f"카메라 {target_camera} 시작 실패"
                    return False
                    
            except Exception as e:
                logger.error(f"Camera switch failed: {str(e)}")
                self.camera_state = CameraState.ERROR
                self.switch_message = f"전환 중 오류: {str(e)}"
                return False

    async def _safe_stop_current_camera(self) -> bool:
        """현재 카메라 안전 정지"""
        if not self.processes:
            return True
            
        try:
            self.camera_state = CameraState.STOPPING
            
            # FFmpeg 프로세스 우선 종료
            if 'ffmpeg' in self.processes:
                proc = self.processes['ffmpeg']
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    proc.kill()
            
            # rpicam 프로세스 종료
            if 'rpicam' in self.processes:
                proc = self.processes['rpicam']
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
            
            # 프로세스 정리
            self.processes.clear()
            
            # 스트림 파일 정리
            if self.active_camera is not None:
                stream_dir = STREAM_BASE_PATH / f"cam{self.active_camera}"
                if stream_dir.exists():
                    for file in stream_dir.glob("*"):
                        try:
                            file.unlink()
                        except:
                            pass
            
            logger.info(f"Camera {self.active_camera} stopped safely")
            return True
            
        except Exception as e:
            logger.error(f"Failed to stop camera safely: {str(e)}")
            return False

    async def _safe_start_camera(self, camera_id: int, config: CameraConfig) -> bool:
        """새 카메라 안전 시작"""
        try:
            self.camera_state = CameraState.STARTING
            
            # 스트림 디렉토리 생성
            stream_dir = STREAM_BASE_PATH / f"cam{camera_id}"
            stream_dir.mkdir(exist_ok=True)
            
            # 기존 파일 정리
            for file in stream_dir.glob("*"):
                try:
                    file.unlink()
                except:
                    pass
            
            self.switch_progress = 60
            
            # rpicam-vid 명령어
            rpicam_cmd = [
                "rpicam-vid",
                "--camera", str(camera_id),
                "--width", str(config.width),
                "--height", str(config.height),
                "--framerate", str(config.framerate),
                "-t", "0",
                "-o", "-",
                "--codec", "mjpeg",
                "--flush"
            ]
            
            # FFmpeg HLS 명령어 (최적화)
            ffmpeg_cmd = [
                "ffmpeg",
                "-f", "mjpeg",
                "-i", "-",
                "-c:v", "libx264",
                "-preset", config.preset,
                "-crf", str(config.quality),
                "-tune", "zerolatency",
                "-g", str(config.framerate),
                "-keyint_min", str(config.framerate),
                "-sc_threshold", "0",  # 키프레임 강제
                "-f", "hls",
                "-hls_time", str(HLS_SEGMENT_TIME),
                "-hls_list_size", str(HLS_PLAYLIST_SIZE),
                "-hls_flags", "delete_segments+independent_segments",
                "-hls_segment_filename", f"{stream_dir}/seg_%03d.ts",
                f"{stream_dir}/index.m3u8"
            ]
            
            self.switch_progress = 70
            
            # 프로세스 시작
            logger.info(f"Starting rpicam: {' '.join(rpicam_cmd[:6])}...")
            rpicam_process = subprocess.Popen(
                rpicam_cmd, 
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            self.switch_progress = 80
            
            logger.info(f"Starting FFmpeg with HLS output...")
            ffmpeg_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=rpicam_process.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            rpicam_process.stdout.close()
            
            # 프로세스 저장
            self.processes = {
                'rpicam': rpicam_process,
                'ffmpeg': ffmpeg_process
            }
            
            self.switch_progress = 90
            
            # 프로세스 상태 확인
            await asyncio.sleep(2)
            
            if rpicam_process.poll() is not None:
                raise Exception(f"rpicam-vid died: {rpicam_process.returncode}")
                
            if ffmpeg_process.poll() is not None:
                raise Exception(f"FFmpeg died: {ffmpeg_process.returncode}")
            
            # HLS 파일 생성 확인
            m3u8_file = stream_dir / "index.m3u8"
            for i in range(8):  # 8초 대기
                if m3u8_file.exists():
                    break
                await asyncio.sleep(1)
            else:
                raise Exception("HLS playlist not created")
            
            logger.info(f"Camera {camera_id} started successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start camera {camera_id}: {str(e)}")
            await self._cleanup_failed_start()
            return False

    async def _cleanup_failed_start(self):
        """실패한 시작 시도 정리"""
        for name, proc in self.processes.items():
            try:
                if proc.poll() is None:
                    proc.terminate()
                    await asyncio.sleep(1)
                    if proc.poll() is None:
                        proc.kill()
            except Exception as e:
                logger.error(f"Failed to cleanup process {name}: {str(e)}")
        
        self.processes.clear()

    async def stop_all_cameras(self) -> bool:
        """모든 카메라 정지"""
        async with self.switch_lock:
            success = await self._safe_stop_current_camera()
            self.active_camera = None
            self.camera_state = CameraState.STOPPED
            self.switch_progress = 0
            self.switch_message = "모든 카메라 정지됨"
            return success

    def get_status(self) -> SmartToggleStatus:
        """현재 상태 반환"""
        return SmartToggleStatus(
            active_camera=self.active_camera,
            camera_state=self.camera_state,
            switch_progress=self.switch_progress,
            switch_message=self.switch_message,
            last_switch_time=self.last_switch_time,
            system_protected=self.system_protected
        )

    def get_stream_url(self, camera_id: int) -> Optional[str]:
        """스트림 URL 반환"""
        if (self.active_camera == camera_id and 
            self.camera_state == CameraState.RUNNING):
            return f"/stream/cam{camera_id}/index.m3u8"
        return None

    async def check_system_protection(self, system_stats) -> bool:
        """시스템 보호 확인"""
        should_protect = (
            system_stats.cpu_percent > self.protection_thresholds['cpu'] or
            system_stats.cpu_temp > self.protection_thresholds['temp'] or
            system_stats.memory_percent > self.protection_thresholds['memory'] or
            self.active_connections > self.protection_thresholds['connections']
        )
        
        if should_protect and not self.system_protected:
            logger.warning("System protection activated - stopping cameras")
            await self.stop_all_cameras()
            self.system_protected = True
            self.switch_message = "시스템 보호를 위해 스트림 중지"
            
        elif not should_protect and self.system_protected:
            logger.info("System protection deactivated")
            self.system_protected = False
            
        return self.system_protected

# 시스템 모니터
class SystemMonitor:
    @staticmethod
    async def get_system_status() -> SystemStatus:
        try:
            # CPU 온도
            try:
                temp_result = subprocess.check_output([
                    "vcgencmd", "measure_temp"
                ], text=True)
                cpu_temp = float(temp_result.replace("temp=", "").replace("'C\n", ""))
            except:
                cpu_temp = 0.0
            
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory_percent = psutil.virtual_memory().percent
            disk_percent = psutil.disk_usage('/').percent
            
            # 업타임
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
                uptime_str = f"{int(uptime_seconds // 3600)}h {int((uptime_seconds % 3600) // 60)}m"
            
            # 시스템 상태
            if cpu_percent > 75 or cpu_temp > 70 or memory_percent > 80:
                state = SystemState.CRITICAL
            elif cpu_percent > 60 or cpu_temp > 60 or memory_percent > 70:
                state = SystemState.WARNING
            else:
                state = SystemState.NORMAL
            
            return SystemStatus(
                cpu_percent=cpu_percent,
                cpu_temp=cpu_temp,
                memory_percent=memory_percent,
                disk_percent=disk_percent,
                uptime=uptime_str,
                state=state,
                timestamp=datetime.now()
            )
            
        except Exception as e:
            logger.error(f"Failed to get system status: {str(e)}")
            return SystemStatus(
                cpu_percent=0, cpu_temp=0, memory_percent=0, disk_percent=0,
                uptime="unknown", state=SystemState.CRITICAL, timestamp=datetime.now()
            )

# FastAPI 애플리케이션
app = FastAPI(
    title="Smart Toggle Camera Server",
    version="1.0.0",
    description="라즈베리파이 5 FastAPI 단독 스마트 토글 카메라 스트리밍 시스템",
    docs_url=None,  # Swagger UI 비활성화 (성능 최적화)
    redoc_url=None  # ReDoc 비활성화
)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 전역 매니저
toggle_manager = SmartToggleManager()
system_monitor = SystemMonitor()

# WebSocket 연결 관리
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        if len(self.active_connections) >= MAX_CONNECTIONS:
            await websocket.close(code=1008, reason="Maximum connections exceeded")
            return False
            
        await websocket.accept()
        self.active_connections.append(websocket)
        toggle_manager.active_connections = len(self.active_connections)
        logger.info(f"WebSocket connected. Total: {len(self.active_connections)}")
        return True

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            toggle_manager.active_connections = len(self.active_connections)
            logger.info(f"WebSocket disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        if not self.active_connections:
            return
            
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message, default=str))
            except:
                disconnected.append(connection)
        
        for conn in disconnected:
            self.disconnect(conn)

connection_manager = ConnectionManager()

# HLS 파일 서빙 (핵심 기능)
@app.get("/stream/{camera_id}/{filename}")
async def serve_hls_file(camera_id: int, filename: str):
    """HLS 파일 직접 서빙 (NGINX 대체)"""
    if camera_id not in [0, 1]:
        raise HTTPException(status_code=404, detail="Invalid camera")
    
    file_path = STREAM_BASE_PATH / f"cam{camera_id}" / filename
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    # MIME 타입 설정
    content_type = "application/vnd.apple.mpegurl" if filename.endswith('.m3u8') else "video/mp2t"
    
    # 캐시 헤더 설정
    if filename.endswith('.m3u8'):
        # 플레이리스트: 캐싱 방지
        headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*"
        }
    else:
        # 세그먼트: 짧은 캐싱
        headers = {
            "Cache-Control": "max-age=10",
            "Access-Control-Allow-Origin": "*"
        }
    
    return FileResponse(
        file_path,
        media_type=content_type,
        headers=headers
    )

# API 엔드포인트
@app.post("/api/camera/{camera_id}/switch")
async def switch_camera(camera_id: int, config: CameraConfig):
    """스마트 카메라 전환"""
    if camera_id not in [0, 1]:
        raise HTTPException(status_code=400, detail="Invalid camera ID")
    
    if toggle_manager.system_protected:
        raise HTTPException(status_code=503, detail="System protected due to high load")
    
    config.camera_id = camera_id
    success = await toggle_manager.smart_switch_camera(camera_id, config)
    
    if success:
        await connection_manager.broadcast({
            "type": "camera_switched",
            "camera_id": camera_id,
            "status": toggle_manager.get_status().dict()
        })
        return {"success": True, "message": f"Switched to camera {camera_id}"}
    else:
        status = toggle_manager.get_status()
        raise HTTPException(status_code=500, detail=status.switch_message)

@app.post("/api/camera/stop")
async def stop_all_cameras():
    """모든 카메라 정지"""
    success = await toggle_manager.stop_all_cameras()
    
    if success:
        await connection_manager.broadcast({
            "type": "all_cameras_stopped",
            "status": toggle_manager.get_status().dict()
        })
        return {"success": True, "message": "All cameras stopped"}
    else:
        raise HTTPException(status_code=500, detail="Failed to stop cameras")

@app.get("/api/status")
async def get_full_status():
    """전체 상태 조회"""
    return {
        "toggle": toggle_manager.get_status().dict(),
        "system": (await system_monitor.get_system_status()).dict(),
        "connections": len(connection_manager.active_connections)
    }

@app.get("/api/stream/{camera_id}/url")
async def get_stream_url(camera_id: int):
    """스트림 URL 조회"""
    stream_url = toggle_manager.get_stream_url(camera_id)
    return {
        "url": stream_url,
        "active": stream_url is not None
    }

# WebSocket 엔드포인트
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    connected = await connection_manager.connect(websocket)
    if not connected:
        return
        
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message["type"] == "get_status":
                toggle_status = toggle_manager.get_status()
                system_status = await system_monitor.get_system_status()
                
                await websocket.send_text(json.dumps({
                    "type": "status_update",
                    "toggle": toggle_status.dict(),
                    "system": system_status.dict(),
                    "connections": len(connection_manager.active_connections)
                }, default=str))
                
    except WebSocketDisconnect:
        connection_manager.disconnect(websocket)

# 메인 페이지 (임베디드 HTML - 이모지 제거 버전)
@app.get("/")
async def get_main_page():
    """메인 웹 페이지"""
    html_content = """
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>FastAPI 단독 스마트 토글 카메라</title>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.4.12/hls.min.js"></script>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                color: #333;
            }
            .container { max-width: 1000px; margin: 0 auto; padding: 20px; }
            .header { text-align: center; color: white; margin-bottom: 30px; }
            .header h1 { font-size: 2.5em; margin-bottom: 10px; text-shadow: 2px 2px 4px rgba(0,0,0,0.3); }
            .subtitle { font-size: 1.1em; opacity: 0.9; }
            
            .status-bar {
                background: rgba(255, 255, 255, 0.95);
                border-radius: 15px;
                padding: 20px;
                margin-bottom: 20px;
                box-shadow: 0 8px 25px rgba(0, 0, 0, 0.1);
            }
            .system-stats {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 15px;
            }
            .stat-item {
                background: #f8f9fa;
                padding: 15px;
                border-radius: 10px;
                text-align: center;
                border-left: 4px solid #667eea;
            }
            .stat-item.warning { border-left-color: #f39c12; background: #fef9e7; }
            .stat-item.critical { border-left-color: #e74c3c; background: #fdedec; }
            .stat-label { font-size: 0.9em; color: #666; margin-bottom: 8px; }
            .stat-value { font-size: 1.4em; font-weight: bold; }
            
            .main-video-section {
                background: rgba(255, 255, 255, 0.95);
                border-radius: 20px;
                padding: 30px;
                margin-bottom: 20px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.15);
            }
            .camera-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 25px;
                flex-wrap: wrap;
                gap: 15px;
            }
            .camera-title { font-size: 1.6em; font-weight: bold; }
            .camera-status {
                padding: 8px 16px;
                border-radius: 25px;
                font-weight: bold;
            }
            .status-stopped { background: #f8d7da; color: #721c24; }
            .status-running { background: #d4edda; color: #155724; }
            .status-switching { background: #cce5ff; color: #004085; }
            .status-error { background: #f5c6cb; color: #721c24; }
            
            .main-video-container {
                position: relative;
                width: 100%;
                height: 400px;
                background: linear-gradient(45deg, #2c3e50, #3498db);
                border-radius: 15px;
                overflow: hidden;
                margin-bottom: 25px;
                box-shadow: 0 5px 15px rgba(0, 0, 0, 0.2);
            }
            .video-player { width: 100%; height: 100%; object-fit: cover; }
            .video-overlay {
                position: absolute;
                top: 0; left: 0; width: 100%; height: 100%;
                background: rgba(0, 0, 0, 0.7);
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                color: white;
                font-size: 1.2em;
                text-align: center;
                padding: 20px;
            }
            .video-overlay .icon { 
                font-size: 3em; 
                margin-bottom: 15px; 
                opacity: 0.8;
                font-weight: bold;
            }
            
            .progress-container {
                position: absolute;
                bottom: 15px; left: 15px; right: 15px;
                background: rgba(255, 255, 255, 0.2);
                border-radius: 10px;
                padding: 10px;
                backdrop-filter: blur(10px);
            }
            .progress-bar {
                width: 100%; height: 6px;
                background: rgba(255, 255, 255, 0.3);
                border-radius: 3px;
                overflow: hidden;
            }
            .progress-fill {
                height: 100%;
                background: linear-gradient(90deg, #4CAF50, #45a049);
                transition: width 0.3s ease;
                width: 0%;
            }
            .progress-text {
                color: white;
                font-size: 0.85em;
                margin-top: 5px;
                text-align: center;
            }
            
            .camera-controls {
                display: flex;
                justify-content: center;
                align-items: center;
                gap: 20px;
                flex-wrap: wrap;
            }
            .camera-toggle-group {
                display: flex;
                background: #f1f3f4;
                border-radius: 50px;
                padding: 5px;
                box-shadow: inset 0 2px 4px rgba(0,0,0,0.1);
            }
            .camera-btn {
                padding: 12px 20px;
                border: none;
                border-radius: 45px;
                cursor: pointer;
                font-weight: bold;
                transition: all 0.3s ease;
                background: transparent;
                color: #666;
                min-width: 110px;
            }
            .camera-btn.active {
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
                box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
            }
            .camera-btn:hover:not(.active) {
                background: rgba(102, 126, 234, 0.1);
                color: #667eea;
            }
            .camera-btn:disabled { opacity: 0.5; cursor: not-allowed; }
            
            .stop-btn {
                padding: 12px 25px;
                border: none;
                border-radius: 25px;
                background: linear-gradient(135deg, #e74c3c, #c0392b);
                color: white;
                font-weight: bold;
                cursor: pointer;
                transition: all 0.3s ease;
                box-shadow: 0 4px 15px rgba(231, 76, 60, 0.3);
            }
            .stop-btn:hover { transform: translateY(-1px); }
            .stop-btn:disabled { opacity: 0.5; cursor: not-allowed; }
            
            .settings-panel {
                background: #f8f9fa;
                border-radius: 10px;
                padding: 15px;
                margin-top: 15px;
                display: flex;
                gap: 15px;
                flex-wrap: wrap;
                justify-content: center;
            }
            .setting-group {
                display: flex;
                align-items: center;
                gap: 5px;
            }
            .setting-group label {
                font-size: 0.9em;
                color: #555;
                font-weight: 500;
            }
            .setting-group select {
                padding: 5px 8px;
                border: 2px solid #ddd;
                border-radius: 5px;
                background: white;
            }
            
            .connection-status {
                position: fixed;
                top: 15px; right: 15px;
                padding: 8px 12px;
                border-radius: 20px;
                font-weight: bold;
                font-size: 0.85em;
                z-index: 1000;
            }
            .connected { background: #d4edda; color: #155724; }
            .disconnected { background: #f8d7da; color: #721c24; }
            
            .info-panel {
                background: rgba(255, 255, 255, 0.95);
                border-radius: 15px;
                padding: 20px;
                margin-top: 20px;
                text-align: center;
            }
            
            @media (max-width: 768px) {
                .container { padding: 15px; }
                .system-stats { grid-template-columns: repeat(2, 1fr); }
                .main-video-container { height: 250px; }
                .camera-header { flex-direction: column; text-align: center; }
                .camera-toggle-group { width: 100%; }
                .camera-btn { flex: 1; }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>FastAPI 단독 스마트 토글 카메라</h1>
                <p class="subtitle">NGINX 없이 FastAPI만으로 구현한 초간단 스트리밍 시스템</p>
            </div>

            <div class="connection-status" id="connectionStatus">연결 중...</div>

            <div class="status-bar">
                <div class="system-stats">
                    <div class="stat-item" id="cpuStat">
                        <div class="stat-label">CPU 사용률</div>
                        <div class="stat-value" id="cpuUsage">--%</div>
                    </div>
                    <div class="stat-item" id="tempStat">
                        <div class="stat-label">CPU 온도</div>
                        <div class="stat-value" id="cpuTemp">--°C</div>
                    </div>
                    <div class="stat-item" id="memoryStat">
                        <div class="stat-label">메모리</div>
                        <div class="stat-value" id="memoryUsage">--%</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">접속자</div>
                        <div class="stat-value" id="connections">--</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">업타임</div>
                        <div class="stat-value" id="uptime">--</div>
                    </div>
                </div>
            </div>

            <div class="main-video-section">
                <div class="camera-header">
                    <div class="camera-title" id="cameraTitle">[CAMERA] 스트리밍</div>
                    <div class="camera-status status-stopped" id="cameraStatus">대기 중</div>
                </div>
                
                <div class="main-video-container">
                    <video class="video-player" id="mainVideo" controls muted></video>
                    <div class="video-overlay" id="videoOverlay">
                        <div class="icon">[CAM]</div>
                        <div id="overlayMessage">카메라를 선택해주세요</div>
                        <div style="margin-top: 10px; font-size: 0.9em; opacity: 0.8;" id="overlaySubmessage">
                            아래 버튼으로 카메라를 선택할 수 있습니다
                        </div>
                    </div>
                    <div class="progress-container" id="progressContainer" style="display: none;">
                        <div class="progress-bar">
                            <div class="progress-fill" id="progressFill"></div>
                        </div>
                        <div class="progress-text" id="progressText">전환 중...</div>
                    </div>
                </div>

                <div class="camera-controls">
                    <div class="camera-toggle-group">
                        <button class="camera-btn" id="camera0Btn" onclick="switchToCamera(0)">
                            [CAM1] 카메라 1
                        </button>
                        <button class="camera-btn" id="camera1Btn" onclick="switchToCamera(1)">
                            [CAM2] 카메라 2
                        </button>
                    </div>
                    <button class="stop-btn" id="stopBtn" onclick="stopAllCameras()">
                        [STOP] 정지
                    </button>
                </div>

                <div class="settings-panel">
                    <div class="setting-group">
                        <label>화질:</label>
                        <select id="qualitySelect">
                            <option value="23">고품질</option>
                            <option value="26" selected>보통</option>
                            <option value="28">저품질</option>
                        </select>
                    </div>
                    <div class="setting-group">
                        <label>프레임률:</label>
                        <select id="framerateSelect">
                            <option value="15">15 fps</option>
                            <option value="24">24 fps</option>
                            <option value="30" selected>30 fps</option>
                        </select>
                    </div>
                    <div class="setting-group">
                        <label>프리셋:</label>
                        <select id="presetSelect">
                            <option value="ultrafast" selected>Ultra Fast</option>
                            <option value="superfast">Super Fast</option>
                        </select>
                    </div>
                </div>
            </div>

            <div class="info-panel">
                <h3>FastAPI 단독 구현 특징</h3>
                <p>• NGINX 없이 FastAPI만으로 HLS 스트리밍 구현<br>
                • 설정 초간단, 메모리 절약, 안전한 8-12명 동시 접속<br>
                • tmpfs 사용으로 I/O 성능 최적화</p>
            </div>
        </div>

        <script>
            class SmartToggleController {
                constructor() {
                    this.websocket = null;
                    this.hlsPlayer = null;
                    this.currentCamera = null;
                    this.isConnected = false;
                    this.initWebSocket();
                }

                initWebSocket() {
                    const wsUrl = `ws://${window.location.host}/ws`;
                    this.websocket = new WebSocket(wsUrl);
                    
                    this.websocket.onopen = () => {
                        this.isConnected = true;
                        this.updateConnectionStatus();
                        this.requestStatus();
                    };
                    
                    this.websocket.onmessage = (event) => {
                        const data = JSON.parse(event.data);
                        this.handleMessage(data);
                    };
                    
                    this.websocket.onclose = () => {
                        this.isConnected = false;
                        this.updateConnectionStatus();
                        setTimeout(() => this.initWebSocket(), 5000);
                    };
                }

                handleMessage(data) {
                    if (data.type === 'status_update' || data.type === 'periodic_update') {
                        this.updateSystemStats(data.system);
                        this.updateToggleStatus(data.toggle);
                        document.getElementById('connections').textContent = data.connections || 0;
                    }
                }

                updateSystemStats(stats) {
                    document.getElementById('cpuUsage').textContent = `${stats.cpu_percent.toFixed(1)}%`;
                    document.getElementById('cpuTemp').textContent = `${stats.cpu_temp.toFixed(1)}°C`;
                    document.getElementById('memoryUsage').textContent = `${stats.memory_percent.toFixed(1)}%`;
                    document.getElementById('uptime').textContent = stats.uptime;
                    
                    this.updateStatClass('cpuStat', stats.cpu_percent, 60, 75);
                    this.updateStatClass('tempStat', stats.cpu_temp, 60, 70);
                    this.updateStatClass('memoryStat', stats.memory_percent, 70, 80);
                }

                updateStatClass(id, value, warn, crit) {
                    const el = document.getElementById(id);
                    el.classList.remove('warning', 'critical');
                    if (value >= crit) el.classList.add('critical');
                    else if (value >= warn) el.classList.add('warning');
                }

                updateToggleStatus(toggle) {
                    const statusEl = document.getElementById('cameraStatus');
                    const titleEl = document.getElementById('cameraTitle');
                    const overlayEl = document.getElementById('videoOverlay');
                    const progressEl = document.getElementById('progressContainer');

                    statusEl.className = `camera-status status-${toggle.camera_state}`;
                    statusEl.textContent = this.getStatusText(toggle.camera_state);

                    if (toggle.active_camera !== null) {
                        titleEl.textContent = `[CAMERA] 카메라 ${toggle.active_camera + 1}`;
                    } else {
                        titleEl.textContent = '[CAMERA] 스트리밍';
                    }

                    this.updateButtons(toggle);

                    if (toggle.camera_state === 'switching') {
                        progressEl.style.display = 'block';
                        overlayEl.style.display = 'flex';
                        document.getElementById('progressFill').style.width = `${toggle.switch_progress}%`;
                        document.getElementById('progressText').textContent = toggle.switch_message;
                        document.getElementById('overlayMessage').textContent = '전환 중...';
                    } else {
                        progressEl.style.display = 'none';
                        if (toggle.camera_state === 'running' && toggle.active_camera !== null) {
                            overlayEl.style.display = 'none';
                            this.initHLS(toggle.active_camera);
                        } else {
                            overlayEl.style.display = 'flex';
                            this.destroyHLS();
                            if (toggle.camera_state === 'error') {
                                document.getElementById('overlayMessage').textContent = '오류 발생';
                                document.getElementById('overlaySubmessage').textContent = toggle.switch_message;
                            } else {
                                document.getElementById('overlayMessage').textContent = '카메라를 선택해주세요';
                            }
                        }
                    }
                }

                getStatusText(state) {
                    const texts = {
                        stopped: '정지됨', starting: '시작 중', running: '실행 중',
                        stopping: '정지 중', switching: '전환 중', error: '오류'
                    };
                    return texts[state] || '알 수 없음';
                }

                updateButtons(toggle) {
                    document.getElementById('camera0Btn').classList.toggle('active', toggle.active_camera === 0);
                    document.getElementById('camera1Btn').classList.toggle('active', toggle.active_camera === 1);
                    
                    const disabled = toggle.camera_state === 'switching';
                    document.getElementById('camera0Btn').disabled = disabled;
                    document.getElementById('camera1Btn').disabled = disabled;
                    document.getElementById('stopBtn').disabled = disabled;
                }

                initHLS(cameraId) {
                    const video = document.getElementById('mainVideo');
                    const streamUrl = `/stream/cam${cameraId}/index.m3u8`;
                    
                    if (Hls.isSupported()) {
                        if (this.hlsPlayer) this.hlsPlayer.destroy();
                        
                        this.hlsPlayer = new Hls({
                            enableWorker: false,
                            lowLatencyMode: true,
                            maxBufferLength: 30
                        });
                        
                        this.hlsPlayer.loadSource(streamUrl);
                        this.hlsPlayer.attachMedia(video);
                        this.hlsPlayer.on(Hls.Events.MANIFEST_PARSED, () => video.play());
                    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                        video.src = streamUrl;
                        video.play();
                    }
                }

                destroyHLS() {
                    if (this.hlsPlayer) {
                        this.hlsPlayer.destroy();
                        this.hlsPlayer = null;
                    }
                    document.getElementById('mainVideo').src = '';
                }

                updateConnectionStatus() {
                    const el = document.getElementById('connectionStatus');
                    el.textContent = this.isConnected ? '[ONLINE] 연결됨' : '[OFFLINE] 끊어짐';
                    el.className = `connection-status ${this.isConnected ? 'connected' : 'disconnected'}`;
                }

                requestStatus() {
                    if (this.websocket?.readyState === WebSocket.OPEN) {
                        this.websocket.send(JSON.stringify({type: 'get_status'}));
                    }
                }
            }

            let controller;
            window.onload = () => {
                controller = new SmartToggleController();
                setInterval(() => controller.requestStatus(), 5000);
            };

            async function switchToCamera(cameraId) {
                const quality = document.getElementById('qualitySelect').value;
                const framerate = document.getElementById('framerateSelect').value;
                const preset = document.getElementById('presetSelect').value;
                
                try {
                    const response = await fetch(`/api/camera/${cameraId}/switch`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            camera_id: cameraId,
                            width: 640, height: 480,
                            framerate: parseInt(framerate),
                            quality: parseInt(quality),
                            preset: preset
                        })
                    });
                    
                    if (!response.ok) {
                        const error = await response.json();
                        console.error('Switch failed:', error.detail);
                    }
                } catch (error) {
                    console.error('Switch error:', error);
                }
            }

            async function stopAllCameras() {
                try {
                    const response = await fetch('/api/camera/stop', {method: 'POST'});
                    if (!response.ok) {
                        const error = await response.json();
                        console.error('Stop failed:', error.detail);
                    }
                } catch (error) {
                    console.error('Stop error:', error);
                }
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# 백그라운드 모니터링
async def background_monitor():
    """백그라운드 시스템 모니터링"""
    while True:
        try:
            system_status = await system_monitor.get_system_status()
            toggle_status = toggle_manager.get_status()
            
            # 시스템 보호 확인
            await toggle_manager.check_system_protection(system_status)
            
            # 주기적 상태 브로드캐스트
            await connection_manager.broadcast({
                "type": "periodic_update",
                "toggle": toggle_status.dict(),
                "system": system_status.dict(),
                "connections": len(connection_manager.active_connections)
            })
            
        except Exception as e:
            logger.error(f"Background monitor error: {str(e)}")
        
        await asyncio.sleep(5)

# 시작/종료 이벤트
@app.on_event("startup")
async def startup_event():
    logger.info("FastAPI Smart Toggle Camera Server starting...")
    logger.info(f"Max connections: {MAX_CONNECTIONS}")
    logger.info(f"Stream directory: {STREAM_BASE_PATH}")
    
    # tmpfs 마운트 확인
    if STREAM_BASE_PATH.as_posix().startswith('/tmp'):
        logger.info("Using tmpfs for optimal I/O performance")
    
    # 백그라운드 모니터링 시작
    asyncio.create_task(background_monitor())

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down server...")
    await toggle_manager.stop_all_cameras()
    logger.info("All cameras stopped, server shut down complete")

# 메인 실행부
if __name__ == "__main__":
    # 포트 설정: 80번 포트 사용 (sudo 필요) 또는 8000번 포트
    port = 80 if os.geteuid() == 0 else 8000
    
    logger.info(f"Starting server on port {port}")
    logger.info("Access web interface at: http://<your-pi-ip>")
    
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=port,
        reload=False,  # 프로덕션에서는 비활성화
        workers=1,     # 단일 워커로 리소스 절약
        log_level="info"
    )