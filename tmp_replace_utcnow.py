import os
import re

python_dir = r"c:\Users\user\Documents\BAC\ict_trading_bot\python"
regex_utcnow = re.compile(r'datetime\.utcnow\(\)')

for root, _, files in os.walk(python_dir):
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            with open(path, 'r', encoding='utf-8') as file:
                content = file.read()
            
            if 'datetime.utcnow()' in content:
                # Add import timezone if not there
                if 'from datetime import timezone' not in content:
                    # try adding to an existing from datetime import
                    if 'from datetime import ' in content:
                        content = re.sub(r'(from datetime import [^\n]+)', r'\1, timezone', content, count=1)
                    else:
                        content = 'from datetime import timezone\n' + content
                
                content = content.replace('datetime.utcnow()', 'datetime.now(timezone.utc)')
                with open(path, 'w', encoding='utf-8') as file:
                    file.write(content)
                print(f"Updated {f}")
