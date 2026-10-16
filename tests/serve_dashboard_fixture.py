"""Browser QA fixture only. Never used by the real dashboard as a fallback.

Run from project root: python tests/serve_dashboard_fixture.py
Serves a labeled fixture preview on 127.0.0.1:4174. Use /offline to exercise
connection failure, /partial for one failed endpoint, /empty for empty results,
and /slow for loading indicators. No PostgreSQL or external network is used.
"""

from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CITIES = [dict(city_id=i, city_name=name, region=region, latitude=lat, longitude=lon)
          for i, (name, region, lat, lon) in enumerate([
              ("Manila", "National Capital Region", 14.5995, 120.9842),
              ("Quezon City", "National Capital Region", 14.676, 121.0437),
              ("Davao", "Davao Region", 7.1907, 125.4553),
              ("Cebu", "Central Visayas", 10.3157, 123.8854),
              ("Baguio", "Cordillera Administrative Region", 16.4023, 120.596),
              ("Cagayan de Oro", "Northern Mindanao", 8.4542, 124.6319),
          ], 1)]


def rows_for(city, now):
    return [dict(city_id=city["city_id"], city_name=city["city_name"],
                 timestamp_ph=(now - timedelta(hours=168-i)).isoformat(),
                 temperature_c=round(29 + math.sin(i/5)*2 - (city["city_id"] == 5)*10 + city["city_id"]*.2, 1),
                 humidity_pct=75, precipitation_mm=round((i % 8)*.3, 1) if i % 9 < 3 else 0,
                 wind_speed_kmh=9.2, heat_index_c=32.6) for i in range(168)]


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in {"/", "/offline", "/partial", "/empty", "/slow"}:
            mode = parsed.path.strip("/")
            html = (ROOT / "index.html").read_text(encoding="utf-8")
            base = "http://127.0.0.1:4174/" + ((mode + "/") if mode else "") + "api/v1/"
            html = html.replace("https://philippine-weather-analytics.onrender.com/api/v1/", base)
            html = html.replace("<body>", '<body><div class="fixture-banner">BROWSER TEST FIXTURE · Sample data · ' + (mode or "populated") + '</div>')
            self.send_data(html.encode(), "text/html; charset=utf-8")
            return
        if "/api/v1/" in parsed.path:
            mode, endpoint = parsed.path.split("api/v1/", 1)
            if "offline" in mode or ("partial" in mode and endpoint == "weather/historical"):
                self.send_data(json.dumps({"detail": "Fixture endpoint unavailable"}).encode(), "application/json", 503)
                return
            if "slow" in mode:
                time.sleep(2)
            now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            if endpoint == "cities":
                data = CITIES
            elif endpoint == "weather/latest":
                data = [rows_for(city, now)[-1] for city in CITIES]
            elif endpoint == "weather/historical":
                city_name = parse_qs(parsed.query).get("city", ["Manila"])[0]
                data = rows_for(next(city for city in CITIES if city["city_name"] == city_name), now)
            elif endpoint == "weather/comparison":
                data = [dict(city_id=city["city_id"], city_name=city["city_name"], avg_temperature_c=round(29 + city["city_id"]*.2 - (city["city_id"] == 5)*10, 1), temperature_hours=168) for city in CITIES]
            else:
                self.send_error(404)
                return
            if "empty" in mode and endpoint != "cities":
                data = []
            payload = dict(data=data, count=len(data), as_of=now.isoformat(), start=(now-timedelta(days=7)).isoformat(), end=now.isoformat())
            self.send_data(json.dumps(payload).encode(), "application/json")
            return
        super().do_GET()

    def send_data(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print("Labeled browser fixture: http://127.0.0.1:4174", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", 4174), partial(Handler, directory=str(ROOT))).serve_forever()
    except KeyboardInterrupt:
        pass
