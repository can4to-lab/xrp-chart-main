import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.database import db


class TradeDiagnosticsStore:
    """Stop/lose trades için veri odaklı tanı kayıtları tutar."""

    def __init__(self, storage_path: Optional[str | Path] = None):
        self.storage_path = Path(storage_path or "trade_diagnostics.jsonl")
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

    def record_trade(self, **payload: Any) -> None:
        entry = {"timestamp": payload.get("timestamp") or None, **payload}
        self.storage_path.open("a", encoding="utf-8").write(json.dumps(entry) + "\n")
        if getattr(db, "pool", None):
            try:
                import asyncio

                async def _persist() -> None:
                    await db.insert_trade_diagnostic(
                        symbol=str(payload.get("symbol", "UNKNOWN")),
                        side=str(payload.get("side", "UNKNOWN")),
                        pnl=float(payload.get("pnl", 0.0)),
                        reason=str(payload.get("reason", "UNKNOWN")),
                        adx=float(payload.get("adx", 0.0)),
                        regime=str(payload.get("regime", "UNKNOWN")),
                        volume=float(payload.get("volume", 0.0)),
                        vol_sma=float(payload.get("vol_sma", 0.0)),
                        atr=float(payload.get("atr", 0.0)),
                        btc_trend=str(payload.get("btc_trend", "UNKNOWN")),
                    )

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    loop.create_task(_persist())
                else:
                    asyncio.run(_persist())
            except Exception as exc:
                print(f"Diagnostic DB write failed: {exc}")

    def _read_entries(self) -> List[Dict[str, Any]]:
        if not self.storage_path.exists():
            return []
        rows = []
        with self.storage_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def summarize_losses(self) -> Dict[str, Any]:
        entries = [entry for entry in self._read_entries() if float(entry.get("pnl", 0.0)) < 0]
        if not entries:
            return {
                "total_losses": 0,
                "reasons": {},
                "regimes": {},
                "btc_trends": {},
                "adx_bucket": {},
            }

        reasons = Counter(entry.get("reason", "UNKNOWN") for entry in entries)
        regimes = Counter(entry.get("regime", "UNKNOWN") for entry in entries)
        btc_trends = Counter(entry.get("btc_trend", "UNKNOWN") for entry in entries)
        adx_bucket = Counter(self._adx_bucket(entry.get("adx", 0.0)) for entry in entries)

        return {
            "total_losses": len(entries),
            "reasons": dict(reasons),
            "regimes": dict(regimes),
            "btc_trends": dict(btc_trends),
            "adx_bucket": dict(adx_bucket),
        }

    @staticmethod
    def _adx_bucket(adx: float) -> str:
        if adx < 20:
            return "<20"
        if adx < 25:
            return "20-25"
        return ">25"


diagnostics_store = TradeDiagnosticsStore()
