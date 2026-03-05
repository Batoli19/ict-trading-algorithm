# 🖱️ Desktop Shortcut Setup — Click & Run Your Bot

## 🎯 What You Want

**Current**: Open VS Code → Open terminal → `cd` to folder → `python main.py`  
**Better**: Double-click desktop icon → Bot starts automatically ✅

---

## ✅ SOLUTION 1: Simple Batch File Shortcut (Easiest)

### Step 1: Create `start_bot.bat`

Create a new file called `start_bot.bat` in your bot folder:  
`C:\Users\user\Documents\BAC\ict_trading_bot\start_bot.bat`

**Paste this:**
```batch
@echo off
title ICT Trading Bot
cd /d C:\Users\user\Documents\BAC\ict_trading_bot\python
echo ========================================
echo    ICT Trading Bot Starting...
echo ========================================
echo.
python main.py
pause
```

**Save the file.**

---

### Step 2: Create Desktop Shortcut

1. **Right-click** on `start_bot.bat` → **Send to** → **Desktop (create shortcut)**
2. A shortcut appears on your desktop
3. **Right-click the shortcut** → **Properties**
4. Click **Change Icon** button
5. Choose an icon from Windows OR use a custom icon (see below)
6. Click **OK**

**Done!** Double-click the shortcut to start your bot.

---

## 🎨 SOLUTION 2: Custom Icon (Professional Look)

### Step 1: Get a Trading Bot Icon

**Option A: Use Windows Built-in Icon**
```
C:\Windows\System32\shell32.dll
```
Contains hundreds of icons — pick one that looks like a robot/chart

**Option B: Download Custom Icon**
- Go to https://www.flaticon.com
- Search: "trading robot" or "bot" or "chart"
- Download as `.ico` format (not PNG or JPG)
- Save to: `C:\Users\user\Documents\BAC\ict_trading_bot\bot_icon.ico`

**Option C: I'll Make You One**
I can create a simple `.ico` file if you want.

---

### Step 2: Assign Icon to Shortcut

1. Right-click your desktop shortcut → **Properties**
2. Click **Change Icon** button
3. Click **Browse**
4. Navigate to your `.ico` file or `shell32.dll`
5. Select your icon
6. Click **OK** → **Apply**

**Your shortcut now has a custom icon!**

---

## 🚀 SOLUTION 3: VBS Script Launcher (Silent Mode)

**This hides the black terminal window completely.**

### Step 1: Create `start_bot_silent.vbs`

Create: `C:\Users\user\Documents\BAC\ict_trading_bot\start_bot_silent.vbs`

**Paste this:**
```vbscript
Set objShell = CreateObject("WScript.Shell")
objShell.CurrentDirectory = "C:\Users\user\Documents\BAC\ict_trading_bot\python"
objShell.Run "python main.py", 0, False
```

**This runs the bot with NO window visible.**

---

### Step 2: Create Shortcut

1. Right-click `start_bot_silent.vbs` → **Send to** → **Desktop**
2. Right-click shortcut → **Properties** → **Change Icon**
3. Assign your custom icon

**Now the bot runs completely in background!**

---

## 📊 SOLUTION 4: System Tray App (Advanced)

**This puts a bot icon in your system tray (next to clock).**

### Create `tray_launcher.pyw`

```python
import sys
import subprocess
from pathlib import Path
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction
from PyQt5.QtGui import QIcon
from PyQt5.QtCore import QProcess

class TradingBotTray:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        
        # Create system tray icon
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon('bot_icon.ico'))  # Your custom icon
        self.tray.setVisible(True)
        self.tray.setToolTip('ICT Trading Bot')
        
        # Create menu
        self.menu = QMenu()
        
        self.start_action = QAction("▶️ Start Bot")
        self.start_action.triggered.connect(self.start_bot)
        self.menu.addAction(self.start_action)
        
        self.stop_action = QAction("⏹️ Stop Bot")
        self.stop_action.triggered.connect(self.stop_bot)
        self.stop_action.setEnabled(False)
        self.menu.addAction(self.stop_action)
        
        self.menu.addSeparator()
        
        self.status_action = QAction("📊 Status: Stopped")
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)
        
        self.menu.addSeparator()
        
        self.quit_action = QAction("❌ Quit")
        self.quit_action.triggered.connect(self.quit_app)
        self.menu.addAction(self.quit_action)
        
        self.tray.setContextMenu(self.menu)
        
        self.process = None
    
    def start_bot(self):
        if self.process is None or self.process.state() == QProcess.NotRunning:
            self.process = QProcess()
            bot_dir = Path(__file__).parent / "python"
            self.process.setWorkingDirectory(str(bot_dir))
            self.process.start("python", ["main.py"])
            
            self.start_action.setEnabled(False)
            self.stop_action.setEnabled(True)
            self.status_action.setText("📊 Status: Running")
            self.tray.showMessage(
                "ICT Trading Bot",
                "Bot started successfully!",
                QSystemTrayIcon.Information,
                2000
            )
    
    def stop_bot(self):
        if self.process and self.process.state() == QProcess.Running:
            self.process.terminate()
            self.process.waitForFinished(5000)
            
            self.start_action.setEnabled(True)
            self.stop_action.setEnabled(False)
            self.status_action.setText("📊 Status: Stopped")
            self.tray.showMessage(
                "ICT Trading Bot",
                "Bot stopped.",
                QSystemTrayIcon.Information,
                2000
            )
    
    def quit_app(self):
        self.stop_bot()
        QApplication.quit()
    
    def run(self):
        sys.exit(self.app.exec_())

if __name__ == '__main__':
    tray_app = TradingBotTray()
    tray_app.run()
```

**Install PyQt5:**
```bash
pip install PyQt5
```

**Run:**
```bash
pythonw tray_launcher.pyw
```

**Features:**
- ✅ Icon in system tray
- ✅ Right-click menu
- ✅ Start/Stop bot
- ✅ Status indicator
- ✅ Notifications

---

## 🎯 MY RECOMMENDATION

### For Beginners: Use **Solution 1** (Batch File)
- ✅ Dead simple
- ✅ Shows terminal output (good for debugging)
- ✅ No extra software needed

### For Intermediate: Use **Solution 2** (Custom Icon)
- ✅ Professional look
- ✅ Easy to identify
- ✅ One-click launch

### For Advanced: Use **Solution 4** (System Tray)
- ✅ Always running in background
- ✅ Start/stop from tray
- ✅ Status notifications
- ✅ No desktop clutter

---

## 🚀 BONUS: Auto-Start on Windows Boot

### Method 1: Startup Folder (Simple)

1. Press `Win + R`
2. Type: `shell:startup`
3. Press Enter
4. **Copy your shortcut** into this folder
5. Bot will start automatically on Windows boot

### Method 2: Task Scheduler (Advanced)

1. Press `Win + R` → type `taskschd.msc`
2. Click **Create Basic Task**
3. Name: "ICT Trading Bot"
4. Trigger: **When I log on**
5. Action: **Start a program**
6. Program: `C:\Users\user\Documents\BAC\ict_trading_bot\start_bot.bat`
7. Finish

**Bot will now start automatically every time Windows starts.**

---

## 📁 Complete File Structure

```
ict_trading_bot/
├── bot_icon.ico           ← Your custom icon
├── start_bot.bat          ← Simple launcher
├── start_bot_silent.vbs   ← Silent launcher
├── tray_launcher.pyw      ← System tray app
├── python/
│   └── main.py
└── config/
    └── settings.json
```

---

## 🎨 Where to Get Icons

**Free Trading Bot Icons:**
- https://www.flaticon.com/search?word=trading+robot
- https://www.iconfinder.com/search?q=bot
- https://icons8.com/icons/set/robot

**Download as `.ico` format (256x256 recommended)**

**Or use Windows built-in icons:**
- Right-click shortcut → Properties → Change Icon
- Browse to: `C:\Windows\System32\shell32.dll`
- Pick any icon you like

---

## ✅ STEP-BY-STEP QUICK START

**5 Minutes to Desktop Shortcut:**

1. **Create** `start_bot.bat` with the batch code above
2. **Right-click** the `.bat` file → **Send to** → **Desktop**
3. **Right-click** desktop shortcut → **Properties**
4. Click **Change Icon**
5. Browse to `shell32.dll` or your custom `.ico`
6. **Done!** Double-click to start bot

**That's it. No more VS Code needed.**

---

## 🎯 TESTING

**Test your shortcut:**
1. Double-click desktop icon
2. Terminal should open
3. Bot should start
4. You should see: "🤖 ICT Trading Bot Starting..."

**If it doesn't work:**
- Check the path in `.bat` file is correct
- Make sure Python is in your PATH
- Right-click shortcut → Run as Administrator

---

**Choose Solution 1 for simplicity, Solution 4 for professionalism. Either way, no more VS Code required!** 🚀
