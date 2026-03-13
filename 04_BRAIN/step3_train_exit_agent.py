"""
Step 3: Train the Exit Agent (Q-Learning).
"""
import pandas as pd
import numpy as np
import joblib
from pathlib import Path

def train_exit_agent():
    TRAIN_DIR = Path("04_BRAIN") / "training_data"
    MODELS_DIR = Path("04_BRAIN") / "models"
    REPORTS_DIR = Path("04_BRAIN") / "reports"
    
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    
    try:
        df = pd.read_csv(TRAIN_DIR / "features.csv")
    except FileNotFoundError:
        print("Features not found. Run Step 1 first.")
        return
        
    # State = setup_type, Action = Target TP (10, 20, 30, 40, 50 pips)
    states = df['setup_type'].dropna().unique()
    actions = [10, 20, 30, 40, 50]
    
    q_table = pd.DataFrame(0.0, index=states, columns=actions)
    
    print(f"Training Q-learning Exit Agent on {len(df)} samples...")
    
    for state in states:
        state_data = df[df['setup_type'] == state]
        for action in actions:
            # Reward: +Action if trade hit it, else -15 SL expectation
            # We estimate hit by check if max pnl (actual outcome) is >= action
            rewards = np.where(state_data['outcome_pips'] >= action, action, -15)
            # Bellman update simplified to offline batch average 
            q_table.loc[state, action] = np.mean(rewards)
            
    best_actions = q_table.idxmax(axis=1)
    
    model_path = MODELS_DIR / "exit_agent.pkl"
    joblib.dump({'q_table': q_table, 'best_actions': best_actions.to_dict()}, model_path)
    
    report = (
        f"EXIT AGENT TRAINING REPORT\n"
        f"==========================\n"
        f"Algorithm: Tabular Q-Learning (Batch Offline)\n\n"
        f"Q-Table (Expected Value of Action):\n{q_table.to_string()}\n\n"
        f"Optimal Take-Profit Actions per Setup:\n{best_actions.to_string()}\n"
    )
    
    print("\n" + report)
    (REPORTS_DIR / "exit_agent_report.txt").write_text(report)

if __name__ == "__main__":
    train_exit_agent()
