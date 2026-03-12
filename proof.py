import psutil
import datetime

print("Running Python Processes:")
print("-" * 60)
for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info', 'create_time']):
    if 'python' in p.info['name'].lower():
        try:
            # interval=0.1 to get actual CPU reading instead of 0.0
            cpu = p.cpu_percent(interval=0.1)
            mem_mb = p.info['memory_info'].rss / 1024 / 1024
            ctime = datetime.datetime.fromtimestamp(p.info['create_time']).strftime("%Y-%m-%d %H:%M:%S")
            print(f"PID: {p.info['pid']:<6} | CPU: {cpu:>5.1f}% | RAM: {mem_mb:>6.1f} MB | Started: {ctime}")
        except:
            pass
