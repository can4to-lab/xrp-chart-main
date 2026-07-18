import json
from pathlib import Path

from core.diagnostics import TradeDiagnosticsStore


def test_loss_summary_groups_contextual_reasons(tmp_path):
    path = tmp_path / "diagnostics.jsonl"
    store = TradeDiagnosticsStore(storage_path=path)

    store.record_trade(
        symbol="SOLUSDT",
        side="LONG",
        pnl=-120.0,
        reason="STOP",
        adx=15.0,
        regime="RANGE",
        volume=1000.0,
        vol_sma=1800.0,
        atr=0.8,
        btc_trend="SIDEWAYS",
    )
    store.record_trade(
        symbol="ETHUSDT",
        side="SHORT",
        pnl=-80.0,
        reason="STOP",
        adx=28.0,
        regime="TREND",
        volume=2200.0,
        vol_sma=2000.0,
        atr=1.4,
        btc_trend="UP",
    )

    summary = store.summarize_losses()

    assert summary["total_losses"] == 2
    assert summary["reasons"]["STOP"] == 2
    assert summary["regimes"]["RANGE"] == 1
    assert summary["regimes"]["TREND"] == 1
    assert summary["btc_trends"]["SIDEWAYS"] == 1

    persisted = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert persisted["reason"] == "STOP"
