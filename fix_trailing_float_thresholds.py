from pathlib import Path

ROOT = Path(__file__).resolve().parent
path = ROOT / "src" / "mexc_tick_scalper" / "hybrid_strategy.py"

text = path.read_text(encoding="utf-8")

old = '''    def _update_trailing_stop(self) -> None:
        candidate: float | None = None

        if self.peak_signed_bps >= self.micro_lock_activation_bps:
            candidate = self.micro_lock_profit_bps

        if self.peak_signed_bps >= self.strong_lock_activation_bps:
            candidate = max(candidate or 0.0, self.strong_lock_profit_bps)

        if self.peak_signed_bps >= self.trailing_activation_bps:
'''
new = '''    def _update_trailing_stop(self) -> None:
        candidate: float | None = None
        eps = 1e-9

        if self.peak_signed_bps + eps >= self.micro_lock_activation_bps:
            candidate = self.micro_lock_profit_bps

        if self.peak_signed_bps + eps >= self.strong_lock_activation_bps:
            candidate = max(candidate or 0.0, self.strong_lock_profit_bps)

        if self.peak_signed_bps + eps >= self.trailing_activation_bps:
'''

if old not in text:
    raise SystemExit("STOP: expected trailing-stop block not found; no file was written")

updated = text.replace(old, new, 1)
compile(updated, str(path), "exec")
path.write_text(updated, encoding="utf-8")

print("OK: floating-point threshold fix applied")
