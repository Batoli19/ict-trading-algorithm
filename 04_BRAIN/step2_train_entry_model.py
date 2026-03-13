"""
Step 2: Train the Entry Learner (XGBoost).
"""
import pandas as pd
import numpy as np
import xgboost as xgb
import joblib
from sklearn.model_selection import TimeSeriesSplit, cross_val_score
from sklearn.metrics import roc_auc_score
from pathlib import Path

def train_entry_model():
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
        
    cat_cols = ['setup_type', 'kill_zone']
    df_encoded = pd.get_dummies(df, columns=cat_cols, drop_first=True)
    
    drop_cols = ['symbol', 'direction', 'exit_reason', 'outcome_binary', 'outcome_pips']
    X = df_encoded.drop(columns=[c for c in drop_cols if c in df_encoded.columns])
    y = df_encoded['outcome_binary']
    
    # Convert booleans to int for XGBoost compatibility
    for col in X.columns:
        if X[col].dtype == bool:
            X[col] = X[col].astype(int)
            
    print(f"Training on {len(X)} samples with {len(X.columns)} features.")
    
    model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.05,
        tree_method='hist',
        eval_metric='logloss',
        random_state=42
    )
    
    tscv = TimeSeriesSplit(n_splits=5)
    cv_scores = cross_val_score(model, X, y, cv=tscv, scoring='roc_auc')
    
    print("TimeSeries CV ROC-AUC Scores:", cv_scores)
    
    model.fit(X, y)
    
    model_path = MODELS_DIR / "entry_model.pkl"
    joblib.dump({'model': model, 'features': list(X.columns)}, model_path)
    
    importances = model.feature_importances_
    feat_impl_df = pd.DataFrame({
        'Feature': X.columns,
        'Importance': importances
    }).sort_values(by='Importance', ascending=False)
    
    report = (
        f"ENTRY MODEL TRAINING REPORT\n"
        f"===========================\n"
        f"Data Samples: {len(X)}\n"
        f"Features count: {len(X.columns)}\n"
        f"CV Mean ROC-AUC: {np.mean(cv_scores):.4f}\n\n"
        f"Top 10 Feature Importances:\n"
        f"{feat_impl_df.head(10).to_string(index=False)}\n"
    )
    
    print("\n" + report)
    (REPORTS_DIR / "entry_model_report.txt").write_text(report)

if __name__ == "__main__":
    train_entry_model()
