import re

with open("ict_strategy.py", "r", encoding="utf-8") as f:
    code = f.read()

# Add rootconfig logic
code = code.replace(
    '    def __init__(self, config: dict):\n        self.cfg = config["ict"]',
    '    def __init__(self, config: dict):\n        self.root_config = config\n        self.cfg = config["ict"]'
)

new_func = """
    def get_min_rr(self, symbol: str, default: float = 2.0) -> float:
        exec_cfg = self.root_config.get("execution", {})
        per_sym = exec_cfg.get("per_symbol", {}).get(symbol, {})
        return float(per_sym.get("min_rr", exec_cfg.get("min_rr", default)))

    def get_pip_size(self, symbol: str) -> float:"""

code = code.replace('    def get_pip_size(self, symbol: str) -> float:', new_func)

code = code.replace('rr = self.cfg.get("rr_ratio", 2.0)', 'rr = self.get_min_rr(symbol, 2.0)')

# Regex to safely replace literal floats multiplying by risk or price differences
code = re.sub(r'\(risk \* ([\d\.]+)\)', r'(risk * self.get_min_rr(symbol, \1))', code)
code = re.sub(r'\(price - sl\) \* ([\d\.]+)', r'(price - sl) * self.get_min_rr(symbol, \1)', code)
code = re.sub(r'\(sl - price\) \* ([\d\.]+)', r'(sl - price) * self.get_min_rr(symbol, \1)', code)

with open("ict_strategy.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Refactored ict_strategy.py successfully.")
