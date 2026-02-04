import psutil
import time
from datetime import datetime

class SystemMonitor:
    def __init__(self):
        self.cpu_history = []
        self.ram_history = []
        self.disk_history = []
        self.max_history_points = 60  # Храним данные за последние 60 секунд
        
    def get_system_stats(self):
        """Получить текущую статистику системы"""
        # CPU
        cpu_percent = psutil.cpu_percent(interval=None) # Важно: interval=None для неблокирующего вызова
        
        # RAM
        ram = psutil.virtual_memory()
        ram_percent = ram.percent
        ram_used_gb = ram.used / (1024**3)
        ram_total_gb = ram.total / (1024**3)
        
        # Disk (корневая файловая система)
        disk = psutil.disk_usage('/')
        disk_percent = disk.percent
        disk_used_gb = disk.used / (1024**3)
        disk_total_gb = disk.total / (1024**3)
        
        # Network
        net_io = psutil.net_io_counters()
        net_sent_mb = net_io.bytes_sent / (1024**2)
        net_recv_mb = net_io.bytes_recv / (1024**2)
        
        # System info
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot_time
        
        # Добавляем в историю
        timestamp = int(time.time() * 1000)
        self.cpu_history.append(cpu_percent) # Храним просто значения для графика
        self.ram_history.append(ram_percent)
        self.disk_history.append(disk_percent)
        
        # Ограничиваем размер истории
        if len(self.cpu_history) > self.max_history_points:
            self.cpu_history.pop(0)
            self.ram_history.pop(0)
            self.disk_history.pop(0)
        
        return {
            'timestamp': timestamp,
            'cpu': {
                'percent': cpu_percent,
                'cores': psutil.cpu_count(logical=False),
                'threads': psutil.cpu_count(logical=True)
            },
            'ram': {
                'percent': ram_percent,
                'used_gb': round(ram_used_gb, 2),
                'total_gb': round(ram_total_gb, 2),
            },
            'disk': {
                'percent': disk_percent,
                'used_gb': round(disk_used_gb, 2),
                'total_gb': round(disk_total_gb, 2),
            },
            'network': {
                'sent_mb': round(net_sent_mb, 2),
                'recv_mb': round(net_recv_mb, 2)
            },
            'system': {
                'uptime': str(uptime).split('.')[0],
                'hostname': 'SFA-MNG'
            }
        }
    
    def get_processes(self, limit=10):
        """Получить топ процессов по CPU"""
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                pinfo = proc.info
                # Фильтруем пустые
                if pinfo['cpu_percent'] is not None:
                    processes.append(pinfo)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        # Сортируем по CPU (то, на чем оборвался DeepSeek)
        processes.sort(key=lambda p: p['cpu_percent'] or 0, reverse=True)
        return processes[:limit]
