This folder is the BRAIN. It is an ML (Machine Learning) upgrade to the trading bot that teaches it how to filter out bad trades, optimize exits, and evolve parameters by learning from past performance.

There are 5 steps involved in training the brain, run automatically via `run_full_brain_pipeline.py`.
It will read historical backtest trades, identify what characterizes winning and losing trades, train a win-probability predictor, and validate if the brain is better than the baseline system. No technical knowledge is needed; simply run the pipeline script to train the AI.
