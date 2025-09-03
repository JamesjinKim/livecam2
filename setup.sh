# setup.sh - 설치 스크립트 (완전히 새로 작성)
#!/bin/bash
set -e

echo "======================================"
echo "FastAPI 카메라 스트리밍 시스템 설치"
echo "======================================"

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 현재 환경 확인
CURRENT_USER=$(whoami)
PROJECT_DIR=$(pwd)

info "사용자: $CURRENT_USER"
info "프로젝트 디렉토리: $PROJECT_DIR"

# 1. 시스템 패키지 설치
info "시스템 패키지 설치 중..."
sudo apt update
sudo apt install -y python3-pip python3-venv ffmpeg htop bc net-tools
success "시스템 패키지 설치 완료"

# 2. Python 가상환경 생성
info "Python 가상환경 생성 중..."
if [ -d "venv" ]; then
    warning "기존 가상환경 제거 중..."
    rm -rf venv
fi
python3 -m venv venv
source venv/bin/activate
success "가상환경 생성 완료"

# 3. Python 패키지 설치
info "Python 패키지 설치 중..."
pip install --upgrade pip
pip install -r requirements.txt
success "Python 패키지 설치 완료"

# 4. tmpfs 설정 (성능 최적화)
info "tmpfs 설정 중..."
if ! grep -q "/tmp/stream" /etc/fstab; then
    echo "tmpfs /tmp/stream tmpfs defaults,size=100M 0 0" | sudo tee -a /etc/fstab
    sudo mkdir -p /tmp/stream
    sudo mount /tmp/stream
    success "tmpfs 설정 완료"
else
    info "tmpfs 이미 설정됨"
fi

# 5. systemd 서비스 생성
info "시스템 서비스 생성 중..."
sudo tee /etc/systemd/system/fastapi-camera.service > /dev/null << EOF
[Unit]
Description=FastAPI Camera Streaming Server
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/venv/bin
ExecStart=$PROJECT_DIR/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable fastapi-camera
success "시스템 서비스 생성 완료"

# 6. 제어 스크립트 생성
info "제어 스크립트 생성 중..."

# 개발 모드 실행 스크립트
cat > run-dev.sh << 'EOF'
#!/bin/bash
echo "[DEV] 개발 모드로 서버 실행 중..."
echo "[INFO] Ctrl+C로 중지하세요"
echo "[PORT] 접속: http://$(hostname -I | awk '{print $1}'):8000"

# tmpfs 마운트 확인
sudo mount | grep -q "/tmp/stream" || sudo mount /tmp/stream

# 가상환경 활성화 및 실행
source venv/bin/activate
python main.py
EOF

# 시스템 서비스 시작 스크립트  
cat > start.sh << 'EOF'
#!/bin/bash
echo "[SERVICE] 시스템 서비스 시작 중..."

# tmpfs 마운트 확인
sudo mount | grep -q "/tmp/stream" || sudo mount /tmp/stream

# 서비스 시작
sudo systemctl start fastapi-camera

# 상태 확인
sleep 3
if systemctl is-active --quiet fastapi-camera; then
    echo "[SUCCESS] 서비스 시작 완료"
    echo "[WEB] 접속: http://$(hostname -I | awk '{print $1}')"
else
    echo "[ERROR] 서비스 시작 실패"
    echo "[LOG] 로그 확인: sudo journalctl -u fastapi-camera -f"
fi
EOF

# 시스템 서비스 중지 스크립트
cat > stop.sh << 'EOF' 
#!/bin/bash
echo "[SERVICE] 시스템 서비스 중지 중..."
sudo systemctl stop fastapi-camera
sudo rm -rf /tmp/stream/*
echo "[SUCCESS] 서비스 중지 완료"
EOF

# 상태 확인 스크립트
cat > status.sh << 'EOF'
#!/bin/bash
echo "============================="
echo "시스템 상태"
echo "============================="

# 기본 정보
echo "[USER] $(whoami)"
echo "[DIR]  $(pwd)"
echo ""

# 서비스 상태
if systemctl is-active --quiet fastapi-camera; then
    echo -e "[SERVICE] \033[32m실행 중\033[0m"
else
    echo -e "[SERVICE] \033[31m중지됨\033[0m"
fi

# 포트 확인
if netstat -tlnp 2>/dev/null | grep -q ":80 "; then
    echo -e "[PORT]    80번 \033[32m사용 중\033[0m"
elif netstat -tlnp 2>/dev/null | grep -q ":8000 "; then
    echo -e "[PORT]    8000번 \033[32m사용 중\033[0m"
else
    echo -e "[PORT]    \033[31m사용 안 함\033[0m"
fi

# tmpfs 확인
if mount | grep -q "/tmp/stream"; then
    echo -e "[TMPFS]   \033[32m활성화됨\033[0m"
else
    echo -e "[TMPFS]   \033[31m비활성화됨\033[0m"
fi

# 카메라 확인
echo ""
echo "[CAMERA]  감지된 장치:"
for i in {0..1}; do
    if [ -c "/dev/video$i" ]; then
        echo "          video$i: 사용 가능"
    fi
done

echo ""
echo "[ACCESS]  웹 접속:"
echo "          http://$(hostname -I | awk '{print $1}')"
EOF

# 로그 확인 스크립트
cat > logs.sh << 'EOF'
#!/bin/bash
echo "실시간 로그 (Ctrl+C로 중지)"
echo "========================="
sudo journalctl -u fastapi-camera -f --no-pager
EOF

chmod +x *.sh
success "제어 스크립트 생성 완료"

# 7. 라즈베리파이 최적화
info "라즈베리파이 최적화 설정 중..."
BOOT_CONFIG="/boot/config.txt"

if [ -f "$BOOT_CONFIG" ]; then
    # GPU 메모리 최소화
    if ! grep -q "gpu_mem=64" $BOOT_CONFIG; then
        echo "gpu_mem=64" | sudo tee -a $BOOT_CONFIG
    fi
    
    # CPU 성능 최적화
    if ! grep -q "arm_freq=2400" $BOOT_CONFIG; then
        echo "arm_freq=2400" | sudo tee -a $BOOT_CONFIG
    fi
    
    # 온도 제한
    if ! grep -q "temp_limit=70" $BOOT_CONFIG; then
        echo "temp_limit=70" | sudo tee -a $BOOT_CONFIG
    fi
    
    success "라즈베리파이 최적화 완료"
else
    warning "boot/config.txt를 찾을 수 없음 (라즈베리파이가 아닐 수 있음)"
fi

# 8. 방화벽 설정
info "방화벽 설정 중..."
sudo ufw allow 22/tcp   # SSH
sudo ufw allow 80/tcp   # HTTP
sudo ufw allow 8000/tcp # FastAPI 개발
sudo ufw --force enable >/dev/null 2>&1
success "방화벽 설정 완료"

echo ""
echo "======================================"
echo "설치 완료!"
echo "======================================"
echo ""
echo "[NEXT STEPS]"
echo "1. main.py 파일을 현재 디렉토리에 복사하세요"
echo "2. 개발/테스트: ./run-dev.sh"
echo "3. 시스템 서비스: sudo reboot 후 ./start.sh"
echo "4. 상태 확인: ./status.sh"
echo ""
echo "[FILES]"
echo "  ./run-dev.sh  - 개발 모드 실행"
echo "  ./start.sh    - 서비스 시작"
echo "  ./stop.sh     - 서비스 중지"
echo "  ./status.sh   - 상태 확인"
echo "  ./logs.sh     - 로그 확인"
echo ""
echo "[현재 디렉토리] $PROJECT_DIR"
echo "[사용자] $CURRENT_USER"