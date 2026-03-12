import sys
import os
sys.stdout = open("mt5_test_output.txt", "w")
sys.stderr = sys.stdout

try:
    import MetaTrader5 as mt5
    print("MT5 module imported OK")
    
    r = mt5.initialize()
    print(f"initialize() = {r}")
    print(f"last_error = {mt5.last_error()}")
    
    if r:
        # Try login to XM
        auth = mt5.login(1301048395, password="yv#fhV&pG4Kn,6L", server="XMGlobal-MT5 6")
        print(f"login() = {auth}")
        print(f"last_error = {mt5.last_error()}")
        
        if auth:
            info = mt5.account_info()
            print(f"Account: {info.login}, Balance: {info.balance}")
            
            # Quick test: get some GBPUSD M5 data
            from datetime import datetime, timezone
            rates = mt5.copy_rates_from_pos("GBPUSD", mt5.TIMEFRAME_M5, 0, 10)
            if rates is not None:
                print(f"Got {len(rates)} recent GBPUSD M5 bars")
                print(f"Latest bar time: {rates[-1][0]}")
            else:
                print(f"No rates returned: {mt5.last_error()}")
        
        mt5.shutdown()
    else:
        print("MT5 init failed - is MT5 terminal running?")
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()

sys.stdout.close()
