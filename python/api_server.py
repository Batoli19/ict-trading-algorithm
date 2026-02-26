"""
Dashboard API Server — Enhanced with AI Memory Data (RUNNABLE)
──────────────────────────────────────────────────────────────
Now includes:
  • Setup performance from AI memory
  • Trade reasoning and lessons
  • Real-time confidence scores
  • SSE streaming (/api/stream)
  • Serves your dashboard.html at /
"""

import json
import time
import logging
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from pathlib import Path
from threading import Thread

from flask import Flask, Response, jsonify, send_from_directory, stream_with_context
from flask_cors import CORS

logger = logging.getLogger("API")


class DashboardAPI:
    def __init__(self, engine, host="0.0.0.0", port=5000):
        self.engine = engine
        self.host = host
        self.port = port
        self.app = Flask(__name__)
        CORS(self.app)
        self._setup_routes()

    @staticmethod
    def _json_default(obj):
        """Convert non-JSON-native values to safe wire formats for SSE."""
        if isinstance(obj, (datetime, date, datetime_time)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, set):
            return list(obj)
        return str(obj)

    def _setup_routes(self):
        web_dir = Path(__file__).parent / "web"

        # ✅ Serve your existing file: web/dashboard.html
        @self.app.route("/", methods=["GET"])
        def home():
            return send_from_directory(web_dir, "dashboard.html")

        @self.app.route("/web/<path:path>", methods=["GET"])
        def web_static(path):
            return send_from_directory(web_dir, path)

        @self.app.route("/api/status", methods=["GET"])
        def get_status():
            status = self._safe_status_payload()
            return jsonify(status)

        @self.app.route("/api/stream", methods=["GET"])
        def stream():
            @stream_with_context
            def generate():
                while True:
                    try:
                        data = self._safe_status_payload()
                        payload = json.dumps(data, default=self._json_default)
                        yield "retry: 2000\n"
                        yield f"data: {payload}\n\n"
                        yield ": ping\n\n"
                        time.sleep(1)
                    except GeneratorExit:
                        logger.info("SSE client disconnected")
                        return
                    except Exception as e:
                        logger.error(f"Stream error: {e}", exc_info=True)
                        err_payload = json.dumps(
                            {
                                **self._empty_status_payload(),
                                "error": str(e),
                                "server_time": datetime.utcnow().isoformat(),
                            },
                            default=self._json_default,
                        )
                        yield "retry: 2000\n"
                        yield f"data: {err_payload}\n\n"
                        time.sleep(2)

            return Response(
                generate(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        @self.app.route("/api/diagnostics", methods=["GET"])
        def diagnostics():
            return jsonify(self._build_diagnostics())

        @self.app.route("/api/shutdown", methods=["POST"])
        def shutdown():
            logger.warning("Shutdown requested via API")
            # engine may not exist in standalone mode; guard it
            try:
                self.engine.shutdown.set()
                return jsonify({"status": "shutdown_requested"})
            except Exception:
                return jsonify({"status": "no_engine_attached"}), 200

    def run_async(self):
        def run():
            logger.info(f"🌐  Dashboard API starting on http://{self.host}:{self.port}")
            self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)
        thread = Thread(target=run, daemon=True)
        thread.start()
        logger.info("✅  API server started")

    def run_blocking(self):
        logger.info(f"🌐  Dashboard API starting on http://{self.host}:{self.port}")
        self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)

    def _empty_status_payload(self):
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "server_time": datetime.utcnow().isoformat(),
            "connected": False,
            "account": {},
            "positions": [],
            "stats": {},
            "trade_log": [],
            "pair_biases": {},
            "setup_performance": [],
            "daily_pnl": 0.0,
            "win_rate": 0.0,
            "trades_today_count": 0,
            "wins_today_count": 0,
            "losses_today_count": 0,
            "profit_factor": 0.0,
            "avg_pnl": 0.0,
            "guard_rails": {
                "daily_max_loss": 0.0,
                "daily_max_trades": 0,
                "risk_per_trade": 0.0,
                "current_daily_pnl": 0.0,
                "remaining_loss_buffer": 0.0,
                "trades_today": 0,
                "triggered": False,
                "reason": "",
            },
            "upcoming_events": [],
            "events_status": "not_configured",
            "upcoming_news": [],
            "analyzer": {"running": False, "last_tick": None},
            "execution_profile": "normal",
            "prop_mode_enabled": False,
            "prop_guardrails": {},
            "sniper_settings": {
                "min_rr": 0.0,
                "min_confidence": 0.0,
                "max_sl_pips": {},
                "max_sl_usd": 0.0,
            },
            "last_skip_reasons": [],
        }

    def _safe_status_payload(self):
        try:
            data = self.engine.get_status()
            if not isinstance(data, dict):
                data = {"error": "engine_status_not_dict"}
            merged = {**self._empty_status_payload(), **data}
            return merged
        except Exception as e:
            logger.error(f"Status error: {e}", exc_info=True)
            return {
                **self._empty_status_payload(),
                "error": str(e),
            }

    def _build_diagnostics(self):
        diagnostics = {
            "server_time": datetime.utcnow().isoformat(),
            "analyzer_running": bool(getattr(self.engine, "analyzer_running", False)),
            "analyzer_last_tick": None,
            "db_counts": {"total_trades": 0, "closed_trades": 0},
            "last_5_trades": [],
        }
        tick = getattr(self.engine, "analyzer_last_tick", None)
        if isinstance(tick, datetime):
            diagnostics["analyzer_last_tick"] = tick.isoformat()
        try:
            memory = getattr(self.engine, "memory", None)
            if memory:
                diagnostics["db_counts"] = memory.get_trade_counts()
                diagnostics["last_5_trades"] = memory.get_last_trades_raw(5)
        except Exception as e:
            diagnostics["db_error"] = str(e)
        return diagnostics


# ✅ THIS is why it was “silent”: you didn’t have an entrypoint.
# This allows running api_server.py directly for UI testing.
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s [%(name)s]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Standalone mode: no engine wired (still serves dashboard + endpoints return error)
    class _DummyEngine:
        def __init__(self):
            class _Shutdown:
                def set(self): pass
            self.shutdown = _Shutdown()

        def get_status(self):
            return {
                "connected": False,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "account": {"balance": 0, "equity": 0, "profit": 0, "currency": "USD", "leverage": 0, "free_margin": 0},
                "stats": {"daily_pnl": 0, "daily_trades": 0, "trades": 0, "winrate": 0, "total_pnl": 0, "avg_win": 0, "avg_loss": 0, "expectancy": 0},
                "positions": [],
                "trade_log": [],
                "upcoming_news": [],
                "setup_performance": [],
                "pair_biases": {}
            }

    api = DashboardAPI(engine=_DummyEngine(), host="127.0.0.1", port=5000)
    api.run_blocking()
