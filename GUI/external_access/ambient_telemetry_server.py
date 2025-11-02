"""HTTP server that exposes ambient telemetry from AutoLog CSV files."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import threading
from collections import defaultdict, OrderedDict
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIR = _REPO_ROOT / "logs" / "AutoLogs" / "Arduino_duo_server"

DEFAULT_METRICS_DISPLAY: Dict[str, Dict[str, Any]] = {
    "SHTC3_TEMPERATURE": {
        "label": "Ambient Temperature",
        "short_label": "Temperature",
        "unit": "°C",
        "decimals": 2,
        "hero": True,
    },
    "SHTC3_HUMIDITY": {
        "label": "Ambient Humidity",
        "short_label": "Humidity",
        "unit": "%RH",
        "decimals": 2,
        "hero": True,
    },
    "LASER_HEAD_OUTPUT_POWER": {
        "label": "Laser Output Power",
        "short_label": "Laser Power",
        "unit": "W",
        "decimals": 3,
        "hero": True,
    },
    "SEED_MONITOR_ANALOG_READ": {
        "label": "Seed Monitor",
        "short_label": "Seed Monitor",
        "unit": "V",
        "decimals": 3,
        "hero": True,
    },
}

DEFAULT_PAGE_SIZE = 240


@dataclass(frozen=True)
class MetricSample:
    """Single metric sample."""

    timestamp: datetime
    value: float


def _safe_float(value: Any) -> Optional[float]:
    """Convert value to float when possible."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO timestamps from CSV rows."""

    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            logging.debug("Unable to parse timestamp: %s", value)
            return None


class LogMetricsStore:
    """Thread-safe loader for AutoLog CSV metrics."""

    def __init__(self, log_dir: Path, metrics_filter: Optional[Iterable[str]] = None) -> None:
        self._log_dir = log_dir
        self._metrics_filter = {name.upper() for name in metrics_filter} if metrics_filter else None
        self._lock = threading.RLock()
        self._data: Dict[str, List[MetricSample]] = {}
        self._file_cache: Dict[Path, Dict[str, List[MetricSample]]] = {}
        self._file_signatures: Dict[Path, Tuple[float, int]] = {}
        self._ordered_files: List[Path] = []
        self._latest_file: Optional[Path] = None
        self._last_refresh_error: Optional[str] = None

    def refresh_if_needed(self) -> None:
        """Reload when new or updated CSV files are present."""

        files = self._list_metric_files()
        if not files:
            with self._lock:
                self._data = {}
                self._file_cache = {}
                self._file_signatures = {}
                self._ordered_files = []
                self._latest_file = None
                self._last_refresh_error = "No AutoLog CSV files found"
            return

        updated_cache: Dict[Path, Dict[str, List[MetricSample]]] = {}
        updated_signatures: Dict[Path, Tuple[float, int]] = {}
        errors: List[str] = []

        for path in files:
            try:
                stat = path.stat()
            except OSError as exc:
                logging.exception("Failed to stat log file %s", path)
                errors.append(str(exc))
                continue

            signature = (stat.st_mtime, stat.st_size)
            cached_signature = self._file_signatures.get(path)

            if cached_signature == signature and path in self._file_cache:
                updated_cache[path] = self._file_cache[path]
                updated_signatures[path] = signature
                continue

            file_data = self._load_single_file(path)
            if file_data is None:
                errors.append(f"Failed to load {path.name}")
                if path in self._file_cache and path in self._file_signatures:
                    updated_cache[path] = self._file_cache[path]
                    updated_signatures[path] = self._file_signatures[path]
                continue

            updated_cache[path] = file_data
            updated_signatures[path] = signature

        with self._lock:
            self._file_cache = updated_cache
            self._file_signatures = updated_signatures
            self._ordered_files = [path for path in files if path in updated_cache]
            self._latest_file = self._ordered_files[-1] if self._ordered_files else None
            self._data = self._merge_file_cache(self._ordered_files)
            self._last_refresh_error = "; ".join(errors) if errors else None

    def _list_metric_files(self) -> List[Path]:
        if not self._log_dir.exists():
            logging.warning("AutoLog directory %s does not exist", self._log_dir)
            return []

        entries: List[Tuple[datetime, Path]] = []
        for path in self._log_dir.glob("due_metrics_*.csv"):
            timestamp = self._parse_filename_timestamp(path.name)
            if timestamp is None:
                try:
                    timestamp = datetime.fromtimestamp(path.stat().st_mtime)
                except OSError:
                    continue
            entries.append((timestamp, path))

        entries.sort(key=lambda item: item[0])
        return [path for _, path in entries]

    @staticmethod
    def _parse_filename_timestamp(filename: str) -> Optional[datetime]:
        stem = filename[:-4] if filename.endswith(".csv") else filename
        if stem.startswith("due_metrics_"):
            stem = stem[len("due_metrics_") :]
        try:
            return datetime.strptime(stem, "%Y%m%d_%H%M%S")
        except ValueError:
            return None

    def _load_single_file(self, path: Path) -> Optional[Dict[str, List[MetricSample]]]:
        logging.info("Loading metrics from %s", path)
        file_data: Dict[str, List[MetricSample]] = defaultdict(list)

        try:
            with path.open("r", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    name = (row.get("name") or "").strip()
                    if not name:
                        continue
                    metric_key = name.upper()
                    if self._metrics_filter and metric_key not in self._metrics_filter:
                        continue
                    value = _safe_float(row.get("value"))
                    if value is None:
                        continue
                    timestamp = self._extract_timestamp(row)
                    if timestamp is None:
                        continue
                    file_data[metric_key].append(MetricSample(timestamp=timestamp, value=value))
        except Exception:
            logging.exception("Failed to load metrics from %s", path)
            return None

        for series in file_data.values():
            series.sort(key=lambda sample: sample.timestamp)

        return file_data

    def _merge_file_cache(self, ordered_files: Iterable[Path]) -> Dict[str, List[MetricSample]]:
        merged: Dict[str, List[MetricSample]] = defaultdict(list)
        for path in ordered_files:
            file_data = self._file_cache.get(path)
            if not file_data:
                continue
            for metric, series in file_data.items():
                merged[metric].extend(series)
        return {metric: list(series) for metric, series in merged.items()}

    @staticmethod
    def _extract_timestamp(row: Dict[str, Any]) -> Optional[datetime]:
        for key in ("measurement_timestamp", "snapshot_timestamp"):
            parsed = _parse_timestamp(row.get(key))
            if parsed is not None:
                return parsed
        return None

    def get_latest_values(
        self, metrics_order: Iterable[str]
    ) -> Tuple[Dict[str, Optional[MetricSample]], Optional[datetime], Optional[Path], Optional[str]]:
        """Return the latest sample for each requested metric."""

        self.refresh_if_needed()
        latest: Dict[str, Optional[MetricSample]] = {}
        most_recent: Optional[datetime] = None

        with self._lock:
            for metric in metrics_order:
                metric_key = metric.upper()
                series = self._data.get(metric_key)
                sample = series[-1] if series else None
                latest[metric_key] = sample
                if sample and (most_recent is None or sample.timestamp > most_recent):
                    most_recent = sample.timestamp
            return latest, most_recent, self._latest_file, self._last_refresh_error

    def get_series(
        self,
        metric: str,
        page: int,
        page_size: int,
    ) -> Tuple[List[MetricSample], int, int, int, Optional[Path], Optional[str]]:
        """Return a page of samples for a metric, newest page numbered zero."""

        self.refresh_if_needed()
        metric_key = metric.upper()

        with self._lock:
            series = self._data.get(metric_key, [])
            total = len(series)
            if total == 0:
                return [], 0, 0, 0, self._latest_file, self._last_refresh_error
            effective_page_size = page_size if page_size > 0 else DEFAULT_PAGE_SIZE
            total_pages = max(1, math.ceil(total / effective_page_size))
            normalized_page = max(0, min(page, total_pages - 1))
            start_index = max(0, total - (normalized_page + 1) * effective_page_size)
            end_index = total - normalized_page * effective_page_size
            window = series[start_index:end_index]
            return list(window), total_pages, normalized_page, total, self._latest_file, self._last_refresh_error

    def available_metrics(self) -> List[str]:
        """Return the list of metrics currently loaded."""

        self.refresh_if_needed()
        with self._lock:
            return sorted(self._data.keys())


class AmbientTelemetryServer:
    """HTTP server that serves telemetry extracted from AutoLog CSV files."""

    def __init__(
        self,
        logs_dir: Path,
        *,
        http_host: str = "0.0.0.0",
        http_port: int = 9001,
        metrics_display: Optional[Dict[str, Dict[str, Any]]] = None,
        default_page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.http_host = http_host
        self.http_port = http_port
        self.logs_dir = logs_dir
        raw_metrics = metrics_display or DEFAULT_METRICS_DISPLAY
        self.metrics_display = {
            key.upper(): {**value, "hero": bool(value.get("hero", False))}
            for key, value in raw_metrics.items()
        }
        self.metric_order = [key for key, meta in self.metrics_display.items() if meta.get("hero", False)]
        self.default_page_size = default_page_size
        self.store = LogMetricsStore(self.logs_dir)
        self.http_server: Optional[HTTPServer] = None
        self.running = False

    def start(self) -> None:
        """Start the HTTP server."""

        if self.running:
            logging.warning("Server already running")
            return

        TelemetryRequestHandler.store = self.store
        TelemetryRequestHandler.base_metrics_display = {
            key: dict(meta) for key, meta in self.metrics_display.items()
        }
        TelemetryRequestHandler.metrics_display = {
            key: dict(meta) for key, meta in self.metrics_display.items()
        }
        TelemetryRequestHandler.hero_defaults = list(self.metric_order)
        TelemetryRequestHandler.metric_order = list(self.metric_order)
        TelemetryRequestHandler.default_page_size = self.default_page_size

        try:
            self.store.refresh_if_needed()
        except Exception:
            logging.exception("Initial log load failed")

        self.http_server = HTTPServer((self.http_host, self.http_port), TelemetryRequestHandler)
        logging.info("HTTP server listening on http://%s:%d", self.http_host, self.http_port)
        logging.info("AutoLog directory: %s", self.logs_dir)
        logging.info("Metrics exposed: %s", ", ".join(self.metric_order))

        self.running = True

        try:
            self.http_server.serve_forever()
        except KeyboardInterrupt:
            logging.info("Interrupt received, shutting down")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the HTTP server."""

        if not self.running:
            return

        self.running = False

        if self.http_server:
            self.http_server.shutdown()
            self.http_server.server_close()
            self.http_server = None
            logging.info("HTTP server stopped")


class TelemetryRequestHandler(BaseHTTPRequestHandler):
    """Request handler exposing HTML dashboard and JSON APIs."""

    store: Optional[LogMetricsStore] = None
    base_metrics_display: Dict[str, Dict[str, Any]] = {}
    metrics_display: Dict[str, Dict[str, Any]] = {}
    metric_order: List[str] = []
    hero_defaults: List[str] = []
    default_page_size: int = DEFAULT_PAGE_SIZE

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003 - API signature
        logging.info("%s - - %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802 - API signature
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/gui"):
            self._handle_gui()
        elif path == "/api/metrics/latest":
            self._handle_latest_metrics()
        elif path == "/api/metrics/series":
            self._handle_metric_series(parsed.query)
        elif path == "/api/metrics/list":
            self._handle_metrics_list()
        else:
            self.send_error(404, "Not Found")

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_metrics_config(self) -> OrderedDict[str, Dict[str, Any]]:
        if not self.store:
            return OrderedDict()

        base_meta = self.base_metrics_display or {}
        current_meta = self.metrics_display or base_meta
        available_metrics = self.store.available_metrics()
        hero_defaults = self.hero_defaults or [
            metric for metric, meta in base_meta.items() if meta.get("hero", False)
        ]

        combined: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        seen: set[str] = set()

        def add_metric(metric_name: str, hero_flag: bool) -> None:
            metric_key = metric_name.upper()
            if metric_key in seen:
                return
            source_meta = current_meta.get(metric_key) or base_meta.get(metric_key) or {}
            combined[metric_key] = self._normalize_meta(metric_key, source_meta, hero_flag)
            seen.add(metric_key)

        for metric in hero_defaults:
            add_metric(metric, True)

        for metric in available_metrics:
            source_meta = current_meta.get(metric) or base_meta.get(metric) or {}
            hero_flag = bool(source_meta.get("hero", False))
            add_metric(metric, hero_flag)

        for metric, meta in current_meta.items():
            add_metric(metric, bool(meta.get("hero", False)))

        for metric, meta in base_meta.items():
            add_metric(metric, bool(meta.get("hero", False)))

        TelemetryRequestHandler.metrics_display = {
            metric: dict(meta) for metric, meta in combined.items()
        }
        TelemetryRequestHandler.metric_order = list(combined.keys())
        return combined

    @staticmethod
    def _normalize_meta(metric: str, meta: Optional[Dict[str, Any]], hero_flag: bool) -> Dict[str, Any]:
        base_label = " ".join(part.capitalize() for part in metric.split("_"))
        normalized: Dict[str, Any] = dict(meta) if meta else {}

        normalized["label"] = normalized.get("label") or base_label
        normalized["short_label"] = normalized.get("short_label") or normalized["label"]
        unit_value = normalized.get("unit")
        normalized["unit"] = unit_value if isinstance(unit_value, str) else ""

        decimals_value = normalized.get("decimals", 2)
        try:
            decimals_value = int(decimals_value)
        except (TypeError, ValueError):
            decimals_value = 2
        normalized["decimals"] = max(0, decimals_value)

        hero_value = normalized.get("hero") if "hero" in normalized else None
        if hero_flag:
            normalized["hero"] = True
        else:
            normalized["hero"] = bool(hero_value)

        return normalized

    def _handle_metrics_list(self) -> None:
        if not self.store:
            self.send_error(500, "Metrics store not configured")
            return

        config = self._get_metrics_config()
        payload = [
            {
                "metric": metric,
                "label": meta.get("label", metric.title().replace("_", " ")),
                "unit": meta.get("unit", ""),
                "hero": meta.get("hero", False),
            }
            for metric, meta in config.items()
        ]

        self._send_json({"metrics": payload})

    def _handle_latest_metrics(self) -> None:
        if not self.store:
            self.send_error(500, "Metrics store not configured")
            return

        config = self._get_metrics_config()
        order = list(config.keys())
        latest_map, most_recent, source_file, error = self.store.get_latest_values(order)

        metrics_payload: Dict[str, Any] = {}
        for metric_key in order:
            sample = latest_map.get(metric_key)
            meta = config.get(metric_key, {})
            metrics_payload[metric_key] = {
                "value": sample.value if sample else None,
                "timestamp": sample.timestamp.isoformat() if sample else None,
                "label": meta.get("label", metric_key),
                "unit": meta.get("unit", ""),
                "decimals": meta.get("decimals"),
            }

        response: Dict[str, Any] = {
            "metrics": metrics_payload,
            "latest_timestamp": most_recent.isoformat() if most_recent else None,
            "source_file": source_file.name if source_file else None,
            "has_data": bool(most_recent),
        }

        if error:
            response["error"] = error

        self._send_json(response)

    def _handle_metric_series(self, query: str) -> None:
        if not self.store:
            self.send_error(500, "Metrics store not configured")
            return

        config = self._get_metrics_config()
        params = parse_qs(query)
        metric = (params.get("metric") or [""])[0].upper()
        if not metric:
            self.send_error(400, "Missing metric parameter")
            return
        if metric not in config:
            self.send_error(400, f"Unknown metric {metric}")
            return

        page = self._parse_int(params.get("page"), default=0)
        page_size = self._parse_int(params.get("page_size"), default=self.default_page_size)

        window, total_pages, normalized_page, total, source_file, error = self.store.get_series(metric, page, page_size)

        data_payload = [
            {"timestamp": sample.timestamp.isoformat(), "value": sample.value}
            for sample in window
        ]
        meta = config.get(metric, {})

        response: Dict[str, Any] = {
            "metric": metric,
            "label": meta.get("label", metric),
            "unit": meta.get("unit", ""),
            "page": normalized_page,
            "requested_page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "count": len(data_payload),
            "total_samples": total,
            "has_newer": normalized_page > 0,
            "has_older": normalized_page < (total_pages - 1) if total_pages else False,
            "data": data_payload,
            "source_file": source_file.name if source_file else None,
        }

        if error:
            response["error"] = error

        self._send_json(response)

    def _handle_gui(self) -> None:
        if not self.store:
            self.send_error(500, "Metrics store not configured")
            return

        config = self._get_metrics_config()
        metrics_json = json.dumps(
            {
                key: {
                    "label": meta.get("label", key),
                    "short_label": meta.get("short_label", meta.get("label", key)),
                    "unit": meta.get("unit", ""),
                    "decimals": meta.get("decimals", 2),
                    "hero": meta.get("hero", False),
                }
                for key, meta in config.items()
            }
        )

        html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Hardware Telemetry</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            background-color: #050505;
            color: #e8ffe8;
            font-family: Arial, Helvetica, sans-serif;
            margin: 0;
            padding: 0;
        }
        .page {
            max-width: 1200px;
            margin: 0 auto;
            padding: 32px 24px 48px;
        }
        .page-header {
            display: flex;
            flex-direction: column;
            gap: 8px;
            margin-bottom: 24px;
        }
        .page-header h1 {
            font-size: 2rem;
            margin: 0;
            color: #7cff7c;
        }
        .page-header .meta {
            font-size: 0.9rem;
            color: #9f9f9f;
        }
        .hero-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-bottom: 32px;
        }
        .hero-card {
            background-color: #111;
            border: 1px solid #1f4f1f;
            border-radius: 12px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            box-shadow: 0 0 20px rgba(0, 255, 0, 0.08);
        }
        .hero-label {
            font-size: 0.9rem;
            color: #8bd28b;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }
        .hero-value {
            font-size: 2.6rem;
            font-weight: 600;
            color: #7cff7c;
        }
        .hero-unit {
            font-size: 1rem;
            color: #9f9f9f;
        }
        .charts {
            display: flex;
            flex-direction: column;
            gap: 32px;
        }
        .chart-panel {
            background-color: #0b0b0b;
            border: 1px solid #1f4f1f;
            border-radius: 12px;
            padding: 20px 20px 28px;
            box-shadow: 0 0 20px rgba(0, 255, 0, 0.05);
        }
        .chart-header {
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 12px;
        }
        .chart-header h2 {
            margin: 0;
            font-size: 1.4rem;
            color: #7cff7c;
        }
        .chart-header .chart-subtle {
            font-size: 0.85rem;
            color: #9f9f9f;
            margin-top: 4px;
        }
        .chart-controls {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .chart-controls button {
            background-color: #152515;
            color: #7cff7c;
            border: 1px solid #2c7f2c;
            border-radius: 6px;
            padding: 6px 14px;
            cursor: pointer;
            font-size: 0.9rem;
        }
        .chart-controls button[disabled] {
            opacity: 0.4;
            cursor: not-allowed;
        }
        .chart-controls .page-indicator {
            font-size: 0.85rem;
            color: #9f9f9f;
            min-width: 120px;
            text-align: center;
        }
        canvas {
            width: 100%;
            max-height: 360px;
        }
        .chart-status {
            margin-top: 10px;
            font-size: 0.85rem;
            color: #afafaf;
            min-height: 18px;
        }
        @media (max-width: 640px) {
            .page {
                padding: 24px 16px 32px;
            }
            .hero-value {
                font-size: 2rem;
            }
        }
    </style>
</head>
<body>
    <div class="page">
        <header class="page-header">
            <h1>Telemetry Monitor</h1>
            <div class="meta" data-role="last-update">Waiting for data...</div>
            <div class="meta" data-role="file-info"></div>
        </header>
        <section class="hero-grid" data-role="hero-grid"></section>
        <section class="charts" data-role="charts"></section>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script>
        const metricsConfig = __METRICS_JSON__;
        const defaultPageSize = __DEFAULT_PAGE_SIZE__;
        const state = {};

        function formatNumber(value, decimals) {
            if (value === null || value === undefined || Number.isNaN(value)) {
                return "--";
            }
            const options = { maximumFractionDigits: decimals, minimumFractionDigits: decimals };
            return Number(value).toLocaleString(undefined, options);
        }

        function formatTimestamp(iso) {
            if (!iso) {
                return "N/A";
            }
            const date = new Date(iso);
            if (Number.isNaN(date.getTime())) {
                return iso;
            }
            return date.toLocaleString();
        }

        function buildLayout() {
            const heroGrid = document.querySelector('[data-role="hero-grid"]');
            const charts = document.querySelector('[data-role="charts"]');
            const metricsEntries = Object.entries(metricsConfig);

            const heroFragment = document.createDocumentFragment();
            const chartFragment = document.createDocumentFragment();

            metricsEntries.forEach(([metric, meta]) => {
                state[metric] = {
                    page: 0,
                    totalPages: 1,
                    chart: null,
                    autoRefresh: Boolean(meta.hero),
                    initialized: false,
                    loading: false,
                };
            });

            const heroEntries = metricsEntries.filter(([, meta]) => meta.hero);
            if (heroEntries.length === 0) {
                heroGrid.style.display = 'none';
            } else {
                heroEntries.forEach(([metric, meta]) => {
                    const card = document.createElement('article');
                    card.className = 'hero-card';
                    card.dataset.metric = metric;
                    card.innerHTML = `
                        <div class="hero-label">${meta.short_label}</div>
                        <div class="hero-value" data-role="value">--</div>
                        <div class="hero-unit">${meta.unit}</div>
                    `;
                    heroFragment.appendChild(card);
                });
                heroGrid.appendChild(heroFragment);
            }

            metricsEntries.forEach(([metric, meta]) => {
                const panel = document.createElement('section');
                panel.className = 'chart-panel';
                panel.dataset.metric = metric;
                panel.innerHTML = `
                    <div class="chart-header">
                        <div>
                            <h2>${meta.label}</h2>
                            <div class="chart-subtle">${meta.unit ? meta.unit : ""}</div>
                        </div>
                        <div class="chart-controls">
                            <button type="button" data-role="older">Older</button>
                            <span class="page-indicator" data-role="page-indicator">Page 1 / 1</span>
                            <button type="button" data-role="newer" disabled>Newer</button>
                        </div>
                    </div>
                    <canvas></canvas>
                    <div class="chart-status" data-role="status"></div>
                `;
                chartFragment.appendChild(panel);

                const ctx = panel.querySelector('canvas').getContext('2d');
                const chart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: [],
                        datasets: [{
                            label: meta.label,
                            data: [],
                            borderColor: '#7cff7c',
                            borderWidth: 2,
                            backgroundColor: 'rgba(124, 255, 124, 0.15)',
                            pointRadius: 0,
                            pointHoverRadius: 4,
                            tension: 0.25,
                        }],
                    },
                    options: {
                        animation: false,
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: {
                            mode: 'nearest',
                            intersect: false,
                        },
                        scales: {
                            x: {
                                ticks: {
                                    color: '#9f9f9f',
                                },
                                grid: {
                                    color: '#1a1a1a',
                                },
                            },
                            y: {
                                ticks: {
                                    color: '#9f9f9f',
                                },
                                grid: {
                                    color: '#1a1a1a',
                                },
                            },
                        },
                        plugins: {
                            legend: {
                                display: false,
                            },
                        },
                    },
                });
                state[metric].chart = chart;

                const olderBtn = panel.querySelector('[data-role="older"]');
                const newerBtn = panel.querySelector('[data-role="newer"]');

                olderBtn.addEventListener('click', () => {
                    const targetPage = state[metric].page + 1;
                    loadSeries(metric, targetPage, true);
                });

                newerBtn.addEventListener('click', () => {
                    const targetPage = Math.max(0, state[metric].page - 1);
                    loadSeries(metric, targetPage, true);
                });
            });

            charts.appendChild(chartFragment);
        }

        function updateHero(payload) {
            const lastUpdateEl = document.querySelector('[data-role="last-update"]');
            const fileInfoEl = document.querySelector('[data-role="file-info"]');
            if (payload.has_data && payload.latest_timestamp) {
                lastUpdateEl.textContent = `Latest update: ${formatTimestamp(payload.latest_timestamp)}`;
            } else {
                lastUpdateEl.textContent = 'Waiting for data...';
            }
            if (payload.source_file) {
                fileInfoEl.textContent = `Source file: ${payload.source_file}`;
            } else {
                fileInfoEl.textContent = '';
            }
            Object.entries(payload.metrics || {}).forEach(([metric, data]) => {
                const meta = metricsConfig[metric];
                if (!meta || !meta.hero) {
                    return;
                }
                const card = document.querySelector(`.hero-card[data-metric="${metric}"]`);
                if (!card) {
                    return;
                }
                const valueEl = card.querySelector('[data-role="value"]');
                const decimals = Number.isInteger(meta.decimals) ? meta.decimals : 2;
                valueEl.textContent = data && data.value !== null && data.value !== undefined
                    ? formatNumber(data.value, decimals)
                    : '--';
                const timestampText = data && data.timestamp ? formatTimestamp(data.timestamp) : null;
                card.title = timestampText ? `Updated at ${timestampText}` : 'No data yet';
            });
        }

        function updateChartUI(metric, payload) {
            const panel = document.querySelector(`.chart-panel[data-metric="${metric}"]`);
            if (!panel) {
                return;
            }
            const statusEl = panel.querySelector('[data-role="status"]');
            const pageIndicatorEl = panel.querySelector('[data-role="page-indicator"]');
            const olderBtn = panel.querySelector('[data-role="older"]');
            const newerBtn = panel.querySelector('[data-role="newer"]');

            if (!payload.count) {
                statusEl.textContent = 'No data available for this metric.';
            } else {
                const firstPoint = payload.data && payload.data.length ? payload.data[0].timestamp : null;
                const lastPoint = payload.data && payload.data.length ? payload.data[payload.data.length - 1].timestamp : null;
                statusEl.textContent = `Window: ${formatTimestamp(firstPoint)} \u2192 ${formatTimestamp(lastPoint)} (samples: ${payload.count})`;
            }

            const totalPages = payload.total_pages || 0;
            const currentPage = totalPages ? (payload.page || 0) + 1 : 0;
            pageIndicatorEl.textContent = totalPages ? `Page ${currentPage} / ${totalPages}` : 'Page 0 / 0';
                    olderBtn.disabled = !payload.has_older;
                    newerBtn.disabled = !payload.has_newer;
        }

        function loadSeries(metric, page, force = false) {
            const panel = document.querySelector(`.chart-panel[data-metric="${metric}"]`);
            const statusEl = panel ? panel.querySelector('[data-role="status"]') : null;
            const chartState = state[metric];
            if (!chartState) {
                return;
            }
            if (chartState.loading && !force) {
                return;
            }

            chartState.loading = true;
            if (statusEl) {
                statusEl.textContent = 'Loading...';
            }

            fetch(`/api/metrics/series?metric=${encodeURIComponent(metric)}&page=${page}&page_size=${defaultPageSize}`)
                .then((response) => {
                    if (!response.ok) {
                        throw new Error(`Request failed with status ${response.status}`);
                    }
                    return response.json();
                })
                .then((payload) => {
                    chartState.page = payload.page || 0;
                    chartState.totalPages = payload.total_pages || 1;
                    chartState.initialized = true;
                    const chart = chartState.chart;
                    if (chart) {
                        const labels = (payload.data || []).map((point) => formatTimestamp(point.timestamp));
                        const values = (payload.data || []).map((point) => point.value);
                        chart.data.labels = labels;
                        chart.data.datasets[0].data = values;
                        chart.update('none');
                    }
                    updateChartUI(metric, payload);
                })
                .catch((error) => {
                    if (statusEl) {
                        statusEl.textContent = `Unable to load data: ${error.message}`;
                    }
                    console.error('Failed to load series', metric, error);
                })
                .finally(() => {
                    chartState.loading = false;
                });
        }

        function refreshLatest() {
            fetch('/api/metrics/latest')
                .then((response) => {
                    if (!response.ok) {
                        throw new Error(`Request failed with status ${response.status}`);
                    }
                    return response.json();
                })
                .then((payload) => {
                    updateHero(payload);
                    Object.entries(metricsConfig).forEach(([metric, meta]) => {
                        const chartState = state[metric];
                        if (!chartState) {
                            return;
                        }
                        if (!chartState.initialized) {
                            loadSeries(metric, chartState.page, true);
                        } else if (chartState.autoRefresh && chartState.page === 0) {
                            loadSeries(metric, 0);
                        }
                    });
                })
                .catch((error) => {
                    console.error('Failed to load latest metrics', error);
                });
        }

        document.addEventListener('DOMContentLoaded', () => {
            buildLayout();
            refreshLatest();
            setInterval(refreshLatest, 5000);
        });
    </script>
</body>
</html>"""

        html = (
            html_template
            .replace("__METRICS_JSON__", metrics_json)
            .replace("__DEFAULT_PAGE_SIZE__", str(self.default_page_size))
        )

        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _parse_int(values: Optional[List[str]], default: int = 0) -> int:
        if not values:
            return default
        try:
            return int(values[0])
        except (TypeError, ValueError):
            return default


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP server for ambient telemetry from AutoLog CSV files")
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Path to AutoLogs Arduino_duo_server directory",
    )
    parser.add_argument(
        "--http-host",
        default="0.0.0.0",
        help="HTTP server bind address",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=9001,
        help="HTTP server port",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help="Number of samples per chart page",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    server = AmbientTelemetryServer(
        args.logs_dir,
        http_host=args.http_host,
        http_port=args.http_port,
        default_page_size=args.page_size,
    )

    server.start()


if __name__ == "__main__":
    main()
