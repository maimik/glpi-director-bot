#!/bin/bash
#===============================================================================
# Setup SysVinit Service for Director Bot
# Target: MX Linux (Debian-based, SysVinit)
#===============================================================================

set -e

# Auto-detect project directory (where this script lives)
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DETECTED_USER="$(stat -c '%U' "$SCRIPT_DIR")"

echo "========================================"
echo "  Director Bot SysVinit Setup"
echo "========================================"
echo ""
echo "Project directory: ${SCRIPT_DIR}"
echo "Run as user: ${DETECTED_USER}"
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "ERROR: This script must be run as root (use sudo)"
   exit 1
fi

# Step 1: Stop any running instances
echo "[1/5] Stopping existing bot instances..."
pkill -f "python.*bot.py" 2>/dev/null && echo "  -> Killed running processes" || echo "  -> No running processes found"
sleep 1

# Step 2: Create init.d script
echo "[2/5] Creating SysVinit script: /etc/init.d/director-bot"

cat > /etc/init.d/director-bot << INITEOF
#!/bin/bash
### BEGIN INIT INFO
# Provides:          director-bot
# Required-Start:    \$local_fs \$network \$syslog
# Required-Stop:     \$local_fs \$network \$syslog
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: Director Assistant GLPI Bot
# Description:       Telegram bot for GLPI ticket approval management
### END INIT INFO

NAME="director-bot"
DESC="Director Assistant Bot"
DAEMON="${SCRIPT_DIR}/venv/bin/python3"
DAEMON_ARGS="bot.py"
WORK_DIR="${SCRIPT_DIR}"
PID_FILE="/var/run/director-bot.pid"
LOG_FILE="${SCRIPT_DIR}/logs/service.log"
RUN_USER="${DETECTED_USER}"

. /lib/lsb/init-functions

# Wait for network connectivity (max 60 seconds)
wait_for_network() {
    local max_attempts=30
    local attempt=1
    local wait_time=2

    while [ \$attempt -le \$max_attempts ]; do
        if ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1 || ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1; then
            echo "  -> Network is up (attempt \${attempt}/\${max_attempts})" >> \${LOG_FILE}
            return 0
        fi
        echo "  -> Waiting for network... (attempt \${attempt}/\${max_attempts})" >> \${LOG_FILE}
        sleep \$wait_time
        attempt=\$((attempt + 1))
    done

    echo "  -> WARNING: Network unreachable after 60s, starting anyway..." >> \${LOG_FILE}
    return 1
}

# Clean stale PID file if process is dead
cleanup_stale_pid() {
    if [ -f \${PID_FILE} ]; then
        local old_pid=\$(cat \${PID_FILE} 2>/dev/null)
        if [ -n "\$old_pid" ]; then
            if ! kill -0 \$old_pid 2>/dev/null; then
                echo "\$(date): Removed stale PID file (PID \$old_pid was not running)" >> \${LOG_FILE}
                rm -f \${PID_FILE}
                return 0
            fi
        fi
    fi
    return 0
}

do_start() {
    log_daemon_msg "Starting \${DESC}" "\${NAME}"

    # Ensure log directory exists
    mkdir -p \$(dirname \${LOG_FILE})
    chown \${RUN_USER}:\${RUN_USER} \$(dirname \${LOG_FILE})

    # Clean up stale PID file from crash/power outage
    cleanup_stale_pid

    # Check if already running
    if [ -f \${PID_FILE} ]; then
        local existing_pid=\$(cat \${PID_FILE} 2>/dev/null)
        if kill -0 \$existing_pid 2>/dev/null; then
            echo "  -> Already running (PID: \$existing_pid)" >> \${LOG_FILE}
            log_end_msg 0
            return 0
        fi
    fi

    # Wait for network before starting (critical for Telegram API)
    echo "\$(date): Waiting for network connectivity..." >> \${LOG_FILE}
    wait_for_network

    # Start the daemon
    start-stop-daemon --start --quiet --background --make-pidfile --pidfile \${PID_FILE} --chuid \${RUN_USER} --chdir \${WORK_DIR} --startas /bin/bash -- -c "exec \${DAEMON} \${DAEMON_ARGS} >> \${LOG_FILE} 2>&1"
    RETVAL=\$?
    log_end_msg \${RETVAL}
    return \${RETVAL}
}

do_stop() {
    log_daemon_msg "Stopping \${DESC}" "\${NAME}"
    start-stop-daemon --stop --quiet --retry=TERM/30/KILL/5 --pidfile \${PID_FILE} --name python3
    RETVAL=\$?
    rm -f \${PID_FILE}
    log_end_msg \${RETVAL}
    return \${RETVAL}
}

do_status() {
    if [ -f \${PID_FILE} ]; then
        PID=\$(cat \${PID_FILE})
        if ps -p \${PID} > /dev/null 2>&1; then
            echo "\${NAME} is running (PID: \${PID})"
            return 0
        else
            echo "\${NAME} is not running (stale PID file)"
            return 1
        fi
    else
        echo "\${NAME} is not running"
        return 3
    fi
}

case "\$1" in
    start) do_start ;;
    stop) do_stop ;;
    restart|force-reload) do_stop; sleep 2; do_start ;;
    status) do_status ;;
    *) echo "Usage: \$0 {start|stop|restart|status}"; exit 1 ;;
esac
exit 0
INITEOF

echo "  -> Init script created"

# Step 3: Make executable
echo "[3/5] Setting permissions..."
chmod +x /etc/init.d/director-bot
echo "  -> Made executable"

# Step 4: Register service
echo "[4/5] Registering service with update-rc.d..."
update-rc.d director-bot defaults
echo "  -> Service registered"

# Step 5: Start service
echo "[5/5] Starting service..."
service director-bot start
sleep 2

echo ""
echo "========================================"
service director-bot status || true
echo "========================================"
echo ""
echo "Setup complete!"
echo "Commands: service director-bot {start|stop|restart|status}"
echo "Logs: tail -f ${SCRIPT_DIR}/logs/service.log"
