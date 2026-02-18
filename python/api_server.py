"""
Flask API Server - Command Center Bridge
─────────────────────────────────────────
Serves live bot data to the web dashboard via REST API.
Runs in parallel with the trading engine.
"""

import asyncio
import logging
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
from threading import Thread

logger = logging.getLogger("API")

class DashboardAPI:
    def __init__(self, engine, host="0.0.0.0", port=5000):
        self.engine = engine
        self.host = host
        self.port = port
        self.app = Flask(__name__)
        CORS(self.app)
        self._setup_routes()
        
    def _setup_routes(self):
        @self.app.route('/api/status', methods=['GET'])
        def get_status():
            """Returns complete bot state for dashboard"""
            try:
                status = self.engine.get_status()
                # Add server timestamp
                status['server_time'] = datetime.utcnow().isoformat()
                return jsonify(status), 200
            except Exception as e:
                logger.error(f"Error fetching status: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/shutdown', methods=['POST'])
        def shutdown():
            """Initiates graceful bot shutdown"""
            logger.warning("🛑 Shutdown requested via API")
            self.engine.shutdown.set()
            return jsonify({'status': 'shutdown_initiated'}), 200
        
        @self.app.route('/api/force_trade', methods=['POST'])
        def force_trade():
            """Force a test trade (testing only)"""
            data = request.json
            symbol = data.get('symbol', 'EURUSD')
            direction = data.get('direction', 'BUY')
            logger.info(f"📍 Force trade requested: {symbol} {direction}")
            # This will be called by the test mode
            return jsonify({'status': 'trade_queued'}), 200
        
        @self.app.route('/api/health', methods=['GET'])
        def health():
            """Health check endpoint"""
            return jsonify({
                'status': 'online',
                'connected': self.engine.mt5.connected,
                'time': datetime.utcnow().isoformat()
            }), 200
    
    def run_async(self):
        """Run Flask in a separate thread"""
        def run():
            logger.info(f"🌐 Dashboard API starting on http://{self.host}:{self.port}")
            self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)
        
        thread = Thread(target=run, daemon=True)
        thread.start()
        logger.info("✅ API server started")
