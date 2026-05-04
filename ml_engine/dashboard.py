"""Flask blueprint for the ML engine: page + JSON API."""
from flask import Flask, jsonify, render_template, request

from ml_engine import config
from ml_engine.models.predictor import predict_all


def register(app: Flask) -> None:
    @app.route("/ml-predictions")
    def ml_predictions_page():
        return render_template("ml_predictions.html",
                               symbols=list(config.SYMBOL_MAP.keys()),
                               default_schema=config.SCHEMA_15M)

    @app.route("/api/ml/predictions")
    def ml_predictions_api():
        schema = request.args.get("schema", config.SCHEMA_15M)
        syms = request.args.get("symbols")
        sym_list = [s.strip() for s in syms.split(",")] if syms else None
        try:
            preds = predict_all(symbols=sym_list, schema=schema)
            return jsonify({"ok": True, "schema": schema, "predictions": preds})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
