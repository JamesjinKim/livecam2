# setup.sh - FastAPI 단독 설정 스크립트 (이모지 제거 버전)
#!/bin/bash
set -e

echo "FastAPI 단독 스마트 토글 카메라 설정"
echo "=================================="

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }

# 시스템 패키지 업데이트
log_info "시스템 업데이트 중..."
sudo apt update && sudo apt upgrade -y

# 필수 패키지 설치
log_info "필수 패키지 설치 중..."
sudo apt install -y \
    python3-pip \
    python3-venv \
    ffmpeg \
    git \
    htop \
    bc

# 프로젝트 디렉토리 생성
log_info "프로젝트 디렉토리 설정 중..."
mkdir -p /home/pi/fastapi-camera
cd /home/pi/fastapi-camera

# tmpfs 설정 (I/O 성능 향상)
log_info "tmpfs 설정 중 (I/O 성능 최적화)..."
if ! grep -q "/tmp/stream" /etc/fstab; then
    echo "tmpfs /tmp/stream tmpfs defaults,size=100M 0 0" | sudo tee -a /etc/fstab
    sudo mkdir -p /tmp/stream
    sudo mount /tmp/stream
    log_success "tmpfs 설정 완료 - 메모리 기반 I/O로 성능 향상!"
fi

# Python 가상환경 설정
log_info "Python 가상환경 생성 중..."
python3 -m venv venv
source venv/bin/activate

# Python 패키지 설치
log_info "Python 패키지 설치 중..."
pip install --upgrade pip
pip install -r requirements.txt

# systemd 서비스 생성
log_info "systemd 서비스 설정 중..."
sudo tee /etc/systemd/system/fastapi-camera.service > /dev/null << EOF
[Unit]
Description=FastAPI Smart Toggle Camera Server
After=network.target

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/home/pi/fastapi-camera
Environment=PATH=/home/pi/fastapi-camera/venv/bin
ExecStart=/home/pi/fastapi-camera/venv/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# 80번 포트 사용을 위한 권한 (선택사항)
# ExecStart=/home/pi/fastapi-camera/venv/bin/sudo /home/pi/fastapi-camera/venv/bin/python main.py

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable fastapi-camera

# 라즈베리파이 최적화
log_info "라즈베리파이 성능 최적화 중..."

# GPU 메모리 최소화 (H.264 하드웨어 인코더 없으므로)
if ! grep -q "gpu_mem=64" /boot/config.txt; then
    echo "gpu_mem=64" | sudo tee -a /boot/config.txt
fi

# CPU 성능 최적화
if ! grep -q "arm_freq=2400" /boot/config.txt; then
    echo "arm_freq=2400" | sudo tee -a /boot/config.txt
fi

# 안정성을 위한 오버볼팅
if ! grep -q "over_voltage=2" /boot/config.txt; then
    echo "over_voltage=2" | sudo tee -a /boot/config.txt
fi

# 온도 제한
if ! grep -q "temp_limit=70" /boot/config.txt; then
    echo "temp_limit=70" | sudo tee -a /boot/config.txt
fi

# 스와프 설정 최적화
log_info "스와프 설정 중..."
sudo dphys-swapfile swapoff 2>/dev/null || true
echo 'CONF_SWAPSIZE=512' | sudo tee /etc/dphys-swapfile > /dev/null
sudo dphys-swapfile setup
sudo dphys-swapfile swapon

# 제어 스크립트 생성
log_info "제어 스크립트 생성 중..."

# 시작 스크립트
cat > start.sh << 'EOF'
#!/bin/bash
echo "FastAPI 카메라 서버 시작 중..."

# tmpfs 마운트 확인
sudo mount | grep -q "/tmp/stream" || sudo mount /tmp/stream

# 서비스 시작
sudo systemctl start fastapi-camera

# 상태 확인
sleep 3
if systemctl is-active --quiet fastapi-camera; then
    echo "[SUCCESS] 서버 시작 성공!"
    echo "[WEB] 웹 인터페이스: http://$(hostname -I | awk '{print $1}')"
    echo "[INFO] 상태 확인: ./status.sh"
else
    echo "[ERROR] 서버 시작 실패!"
    echo "[DEBUG] 로그 확인: sudo journalctl -u fastapi-camera -f"
fi
EOF

# 중지 스크립트
cat > stop.sh << 'EOF'
#!/bin/bash
echo "FastAPI 카메라 서버 중지 중..."

sudo systemctl stop fastapi-camera

# 스트림 파일 정리
sudo rm -rf /tmp/stream/*

echo "[SUCCESS] 서버 중지 완료!"
EOF

# 상태 확인 스크립트
cat > status.sh << 'EOF'
#!/bin/bash

echo "FastAPI 카메라 서버 상태"
echo "======================="

# 서비스 상태
if systemctl is-active --quiet fastapi-camera; then
    echo -e "[SERVICE] \033[32m실행 중\033[0m"
else
    echo -e "[SERVICE] \033[31m중지됨\033[0m"
fi

# 포트 확인
if netstat -tlnp 2>/dev/null | grep -q ":80 "; then
    echo -e "[PORT] 80: \033[32m열림\033[0m"
elif netstat -tlnp 2>/dev/null | grep -q ":8000 "; then
    echo -e "[PORT] 8000: \033[32m열림\033[0m"
else
    echo -e "[PORT] \033[31m닫힘\033[0m"
fi

# 시스템 리소스
CPU_TEMP=$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2 | cut -d\' -f1 || echo "N/A")
CPU_USAGE=$(top -bn1 | grep "Cpu(s)" | awk '{print $2}' | cut -d'%' -f1 2>/dev/null || echo "N/A")
MEMORY_USAGE=$(free | grep Mem | awk '{printf "%.1f", $3/$2 * 100.0}' 2>/dev/null || echo "N/A")

echo "[TEMP] CPU 온도: ${CPU_TEMP}°C"
echo "[CPU] CPU 사용률: ${CPU_USAGE}%"
echo "[MEM] 메모리 사용률: ${MEMORY_USAGE}%"

# tmpfs 상태
if mount | grep -q "/tmp/stream"; then
    echo -e "[TMPFS] \033[32m활성화\033[0m (I/O 최적화)"
    echo "[FILES] 스트림 파일: $(ls /tmp/stream 2>/dev/null | wc -l)개"
else
    echo -e "[TMPFS] \033[31m비활성화\033[0m"
fi

# 카메라 감지
echo ""
echo "[CAMERA] 카메라 상태:"
for i in {0..1}; do
    if [ -c "/dev/video$i" ]; then
        echo -e "         카메라 $i: \033[32m감지됨\033[0m"
    else
        echo -e "         카메라 $i: \033[31m없음\033[0m"
    fi
done

# 접속 정보
echo ""
echo "[ACCESS] 접속 정보:"
echo "         로컬: http://localhost"
echo "         네트워크: http://$(hostname -I | awk '{print $1}')"

echo ""
echo "[COMMANDS] 유용한 명령어:"
echo "           실시간 로그: sudo journalctl -u fastapi-camera -f"
echo "           서비스 재시작: sudo systemctl restart fastapi-camera"
echo "           성능 모니터: htop"
EOF

# 로그 확인 스크립트
cat > logs.sh << 'EOF'
#!/bin/bash
echo "FastAPI 카메라 서버 로그"
echo "======================"
echo "실시간 로그 (Ctrl+C로 종료):"
echo ""
sudo journalctl -u fastapi-camera -f --no-pager
EOF

# 벤치마크 스크립트
cat > benchmark.sh << 'EOF'
#!/bin/bash
echo "시스템 성능 벤치마크"
echo "=================="

echo "[CPU] CPU 정보:"
cat /proc/cpuinfo | grep "model name" | head -1 | cut -d: -f2

echo ""
echo "[TEMP] 현재 온도:"
vcgencmd measure_temp

echo ""
echo "[CLOCK] CPU 클럭:"
vcgencmd measure_clock arm

echo ""
echo "[MEMORY] 메모리 정보:"
free -h

echo ""
echo "[DISK] 디스크 사용량:"
df -h / | tail -1

echo ""
echo "[NETWORK] 네트워크 테스트 (5초):"
ping -c 5 8.8.8.8 | tail -1

echo ""
echo "[tmpfs] tmpfs 상태:"
df -h /tmp/stream 2>/dev/null || echo "tmpfs가 마운트되지 않음"

echo ""
echo "[CAMERA] 카메라 장치:"
ls -la /dev/video* 2>/dev/null || echo "카메라 장치 없음"

echo ""
echo "[PERFORMANCE] 시스템 부하 (10초 평균):"
uptime
EOF

# 스크립트 실행 권한 부여
chmod +x *.sh

# 방화벽 설정
log_info "방화벽 설정 중..."
sudo ufw allow 22/tcp   # SSH
sudo ufw allow 80/tcp   # HTTP
sudo ufw allow 8000/tcp # FastAPI 백업
sudo ufw --force enable

# 완료 메시지
log_success "FastAPI 단독 카메라 시스템 설치 완료!"
echo ""
echo "[NEXT] 다음 단계:"
echo "1. main.py 파일을 이 디렉토리에 복사"
echo "2. 시스템 재부팅: sudo reboot"
echo "3. 서버 시작: ./start.sh"
echo "4. 상태 확인: ./status.sh"
echo "5. 웹 접속: http://$(hostname -I | awk '{print $1}')"
echo ""
log_warning "재부팅 후 자동으로 서비스가 시작됩니다"
log_info "tmpfs 사용으로 I/O 성능이 대폭 향상됩니다!"

# requirements-dev.txt - 개발용 추가 패키지 (선택사항)
fastapi==0.104.1
uvicorn[standard]==0.24.0
websockets==12.0
psutil==5.9.6
pydantic==2.5.0

# 개발용 추가 도구 (선택사항)
# pytest==7.4.3
# black==23.11.0
# flake8==6.1.0