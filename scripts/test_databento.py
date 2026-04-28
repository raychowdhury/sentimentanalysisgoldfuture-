"""Verify Databento API key + fetch GC (gold) front-month daily OHLCV."""
import os
from datetime import date, timedelta
from dotenv import load_dotenv
import databento as db

load_dotenv()

key = os.getenv("DATABENTO_API_KEY")
if not key:
    raise SystemExit("DATABENTO_API_KEY missing from .env")

client = db.Historical(key)

print("Auth check — listing datasets (truncated):")
datasets = client.metadata.list_datasets()
print(datasets[:10], "...")

end = date.today() - timedelta(days=1)
start = end - timedelta(days=10)

print(f"\nFetching GC.FUT ohlcv-1d {start} -> {end}")
data = client.timeseries.get_range(
    dataset="GLBX.MDP3",
    symbols="GC.FUT",
    stype_in="parent",
    schema="ohlcv-1d",
    start=start.isoformat(),
    end=end.isoformat(),
)
df = data.to_df()
print(df.tail())
print(f"\nrows: {len(df)}  cols: {list(df.columns)}")
