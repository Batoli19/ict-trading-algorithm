import json
from pathlib import Path

settings_path = Path("c:/Users/user/Documents/BAC/ict_trading_bot/config/settings.json")
with open(settings_path, "r") as f:
    data = json.load(f)

# Update times
data["ict"]["kill_zones"]["london_open"]["start"] = "06:00"
data["ict"]["kill_zones"]["london_open"]["end"] = "09:00"

data["ict"]["kill_zones"]["ny_open"]["start"] = "13:30"
data["ict"]["kill_zones"]["ny_open"]["end"] = "16:00"

# Ensure all allowed
data["ict"]["kill_zones"]["allowed_kill_zones"] = ["LONDON_OPEN", "NY_OPEN", "LONDON_CLOSE"]

with open(settings_path, "w") as f:
    json.dump(data, f, indent=2)

print("Updated settings.json with TRUE ICT times: LO 06:00-09:00, NY 13:30-16:00")
