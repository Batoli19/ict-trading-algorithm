import sys
import os

with open("logs/bot.log", "r", encoding="utf-8", errors="replace") as f:
    lines = f.readlines()
    
last_lines = lines[-200:]
with open("bot_tail.log", "w", encoding="utf-8") as f:
    f.writelines(last_lines)
