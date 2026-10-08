import logging
import os
import sys
import subprocess
from flask import Flask, render_template_string, redirect, request, jsonify, url_for
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

# Configure Logging
log_level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("zwift_connector")

from app.zwift_client import ZwiftClient
from app.sync import load_tokens, save_tokens, load_history, get_all_activities, get_data_paths
from app.scheduler import scheduler

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "zwift_connector_secret_key_12345")

def ensure_ssl_certs():
    """Generate self-signed SSL certificates if they don't exist yet."""
    paths = get_data_paths()
    config_dir = os.path.dirname(paths["tokens"])
    os.makedirs(config_dir, exist_ok=True)
    
    cert_path = os.path.join(config_dir, "cert.pem")
    key_path = os.path.join(config_dir, "key.pem")

    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        logger.info("Generating self-signed SSL certificate for HTTPS...")
        try:
            cmd = [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", key_path, "-out", cert_path,
                "-days", "365", "-nodes", "-subj", "/CN=localhost"
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info(f"SSL certificate generated successfully at {cert_path}")
        except Exception as e:
            logger.error(f"Failed to generate SSL certificate using openssl: {e}")
            return None

    return (cert_path, key_path)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>dreeve-zwift-connector</title>
    <link rel="icon" type="image/png" href="/static/icon.png">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: rgba(22, 30, 46, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --accent-color: #ff5500;
            --accent-hover: #e04b00;
            --success-color: #10b981;
            --warning-color: #f59e0b;
            --text-primary: #f3f4f6;
            --text-secondary: #9ca3af;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(255, 85, 0, 0.12) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.08) 0px, transparent 50%);
            background-attachment: fixed;
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 2rem 1rem;
        }

        .container {
            width: 100%;
            max-width: 950px;
        }

        header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 2rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid var(--border-color);
        }

        .logo-title {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .logo-img {
            width: 44px;
            height: 44px;
            border-radius: 10px;
            object-fit: cover;
            box-shadow: 0 4px 14px rgba(255, 85, 0, 0.35);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }

        h1 {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.025em;
        }

        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.35rem 0.85rem;
            border-radius: 9999px;
            font-size: 0.85rem;
            font-weight: 600;
        }

        .status-connected {
            background: rgba(16, 185, 129, 0.15);
            color: var(--success-color);
            border: 1px solid rgba(16, 185, 129, 0.3);
        }

        .status-disconnected {
            background: rgba(245, 158, 11, 0.15);
            color: var(--warning-color);
            border: 1px solid rgba(245, 158, 11, 0.3);
        }

        .dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background-color: currentColor;
            display: inline-block;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 1.25rem;
            margin-bottom: 1.5rem;
        }

        .card {
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 1.25rem;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
        }

        .card-label {
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-secondary);
            margin-bottom: 0.5rem;
            font-weight: 600;
        }

        .card-value {
            font-size: 1.5rem;
            font-weight: 700;
        }

        .card-subtext {
            font-size: 0.85rem;
            color: var(--text-secondary);
            margin-top: 0.25rem;
        }

        .control-panel {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 1.25rem;
            margin-bottom: 1.5rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            flex-wrap: wrap;
        }

        .time-selector-group {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .time-label {
            font-size: 0.9rem;
            font-weight: 600;
            color: var(--text-secondary);
        }

        select.time-select {
            background: rgba(15, 23, 42, 0.8);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
            padding: 0.6rem 1rem;
            border-radius: 8px;
            font-size: 0.9rem;
            font-weight: 500;
            outline: none;
            cursor: pointer;
        }

        select.time-select:focus {
            border-color: var(--accent-color);
        }

        .actions {
            display: flex;
            gap: 0.75rem;
            align-items: center;
        }

        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
            padding: 0.65rem 1.25rem;
            border-radius: 9px;
            font-size: 0.9rem;
            font-weight: 600;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.2s ease;
            border: none;
        }

        .btn-primary {
            background: linear-gradient(135deg, var(--accent-color), #e04b00);
            color: white;
            box-shadow: 0 4px 14px rgba(255, 85, 0, 0.3);
        }

        .btn-primary:hover:not(:disabled) {
            transform: translateY(-1px);
            box-shadow: 0 6px 20px rgba(255, 85, 0, 0.4);
        }

        .btn-primary:disabled {
            opacity: 0.65;
            cursor: not-allowed;
        }

        .btn-secondary {
            background: rgba(255, 255, 255, 0.06);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
        }

        .btn-secondary:hover {
            background: rgba(255, 255, 255, 0.1);
        }

        .section-title {
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.9rem;
        }

        th {
            padding: 0.75rem 1rem;
            color: var(--text-secondary);
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            border-bottom: 1px solid var(--border-color);
        }

        td {
            padding: 0.85rem 1rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
        }

        tr:hover td {
            background: rgba(255, 255, 255, 0.02);
        }

        .empty-state {
            text-align: center;
            padding: 3rem 1rem;
            color: var(--text-secondary);
        }

        .alert {
            padding: 1rem;
            border-radius: 10px;
            margin-bottom: 1.5rem;
            font-size: 0.9rem;
        }

        .alert-warning {
            background: rgba(245, 158, 11, 0.15);
            border: 1px solid rgba(245, 158, 11, 0.3);
            color: #fbbf24;
        }

        .alert-success {
            background: rgba(16, 185, 129, 0.15);
            border: 1px solid rgba(16, 185, 129, 0.3);
            color: #34d399;
        }

        .spinner {
            width: 16px;
            height: 16px;
            border: 2px solid rgba(255,255,255,0.3);
            border-top-color: white;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            display: inline-block;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        @media (max-width: 650px) {
            header {
                flex-direction: column;
                align-items: flex-start;
                gap: 1rem;
            }
            .control-panel {
                flex-direction: column;
                align-items: stretch;
            }
            .time-selector-group {
                flex-direction: column;
                align-items: stretch;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo-title">
                <img src="/static/icon.png" class="logo-img" alt="dreeve-zwift-connector logo">
                <div>
                    <h1>dreeve-zwift-connector</h1>
                    <div style="font-size: 0.8rem; color: var(--text-secondary);">Automatic FIT File Downloader</div>
                </div>
            </div>

            {% if authenticated %}
                <div class="status-badge status-connected">
                    <span class="dot"></span> Zwift Configured
                </div>
            {% else %}
                <div class="status-badge status-disconnected">
                    <span class="dot"></span> Not Connected
                </div>
            {% endif %}
        </header>

        {% if message %}
            <div class="alert alert-{{ msg_type }}">
                {{ message }}
            </div>
        {% endif %}

        {% if not missing_env %}
            <div class="grid">
                <div class="card">
                    <div class="card-label">Total FIT Files Downloaded</div>
                    <div class="card-value" id="stat-total-count">{{ total_downloaded }}</div>
                    <div class="card-subtext">Stored in ./activities</div>
                </div>

                <div class="card">
                    <div class="card-label">Auto-Sync Schedule</div>
                    <div class="card-value" style="font-size: 1.1rem; font-family: monospace;">
                        {% if cron_expr %}
                            {{ cron_expr }}
                        {% else %}
                            Disabled
                        {% endif %}
                    </div>
                    <div class="card-subtext" id="stat-next-sync">
                        {% if next_sync_time %}
                            Next run: {{ next_sync_time[:16].replace('T', ' ') }} UTC
                        {% elif last_sync %}
                            Last synced: {{ last_sync[:19] }}
                        {% else %}
                            Daily schedule active
                        {% endif %}
                    </div>
                </div>

                <div class="card">
                    <div class="card-label">Sync Engine Status</div>
                    <div class="card-value" id="stat-engine-status" style="font-size: 1.2rem; color: {% if is_syncing %}var(--accent-color){% else %}var(--success-color){% endif %};">
                        {% if is_syncing %}
                            Syncing...
                        {% else %}
                            Idle / Ready
                        {% endif %}
                    </div>
                    <div class="card-subtext">Incremental & Deduplicated</div>
                </div>
            </div>

            {% if authenticated %}
                <div class="control-panel">
                    <div class="time-selector-group">
                        <span class="time-label">Sync Time Window:</span>
                        <select id="time-window-select" class="time-select">
                            <option value="1_day" {% if default_time_window == '1_day' %}selected{% endif %}>1 Day (Last 24 Hours)</option>
                            <option value="1_week" {% if default_time_window == '1_week' %}selected{% endif %}>1 Week (Last 7 Days)</option>
                            <option value="1_month" {% if default_time_window == '1_month' %}selected{% endif %}>1 Month (Last 30 Days)</option>
                            <option value="1_year" {% if default_time_window == '1_year' %}selected{% endif %}>1 Year (Last 365 Days)</option>
                            <option value="all_time" {% if default_time_window == 'all_time' %}selected{% endif %}>All Time (Full Sync)</option>
                        </select>
                    </div>

                    <div class="actions">
                        <button id="sync-btn" onclick="triggerSync()" class="btn btn-primary" {% if is_syncing %}disabled{% endif %}>
                            <span class="spinner" id="btn-spinner" style="display: {% if is_syncing %}inline-block{% else %}none{% endif %};"></span>
                            <span id="btn-text">{% if is_syncing %}Syncing...{% else %}Sync Now{% endif %}</span>
                        </button>
                        
                    </div>
                </div>
            {% else %}
                <div class="control-panel">
                    <div>
                        <strong>Zwift Credentials Required</strong>
                        <div style="font-size: 0.85rem; color: var(--text-secondary);">Set ZWIFT_EMAIL and ZWIFT_PASSWORD in your environment to start downloading .FIT files.</div>
                    </div>
                    
                </div>
            {% endif %}

            <div class="card">
                <div class="section-title">
                    <span>Downloaded Workout Activities</span>
                    <span style="font-size: 0.85rem; color: var(--text-secondary);" id="stat-table-count">Showing {{ activities|length }} items</span>
                </div>

                {% if activities %}
                    <div style="overflow-x: auto; max-height: 550px;">
                        <table>
                            <thead>
                                <tr>
                                    <th>Workout ID</th>
                                    <th>Workout Date</th>
                                    <th>Filename</th>
                                    <th>File Size</th>
                                    <th>Downloaded At</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for act in activities %}
                                    <tr>
                                        <td><strong>{{ act['id'] }}</strong></td>
                                        <td>{{ act['starts'][:10] if act['starts'] else 'N/A' }}</td>
                                        <td><code>{{ act['filename'] }}</code></td>
                                        <td>{{ act['size_str'] }}</td>
                                        <td>{{ act['downloaded_at'][:19] if act['downloaded_at'] else 'N/A' }}</td>
                                    </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                {% else %}
                    <div class="empty-state">
                        <p>No workout files downloaded yet.</p>
                        {% if authenticated %}
                            <p style="margin-top: 0.5rem; font-size: 0.85rem;">Select a time window above and click "Sync Now".</p>
                        {% else %}
                            <p style="margin-top: 0.5rem; font-size: 0.85rem;">Set your Zwift credentials to get started.</p>
                        {% endif %}
                    </div>
                {% endif %}
            </div>
        {% else %}
            <div class="card" style="border-color: rgba(245, 158, 11, 0.4);">
                <div class="card-label" style="color: var(--warning-color);">Configuration Required</div>
                <p style="margin-top: 0.5rem; line-height: 1.5;">
                    Please set <code>ZWIFT_EMAIL</code> and <code>ZWIFT_PASSWORD</code> environment variables in your <code>.env</code> file or Docker environment to enable Zwift authorization.
                </p>
            </div>
        {% endif %}
    </div>

    <script>
        let wasSyncing = {{ 'true' if is_syncing else 'false' }};

        async function checkStatus() {
            try {
                const response = await fetch('/api/status');
                const data = await response.json();
                
                const btn = document.getElementById('sync-btn');
                const spinner = document.getElementById('btn-spinner');
                const btnText = document.getElementById('btn-text');
                const engineStatus = document.getElementById('stat-engine-status');
                const totalCount = document.getElementById('stat-total-count');
                const nextSync = document.getElementById('stat-next-sync');

                if (totalCount) totalCount.innerText = data.total_downloaded;

                if (data.next_sync_time && nextSync) {
                    nextSync.innerText = 'Next run: ' + data.next_sync_time.substring(0, 16).replace('T', ' ') + ' UTC';
                }

                if (data.is_syncing) {
                    wasSyncing = true;
                    if (btn) {
                        btn.disabled = true;
                        if (spinner) spinner.style.display = 'inline-block';
                        if (btnText) btnText.innerText = 'Syncing...';
                    }
                    if (engineStatus) {
                        engineStatus.innerText = 'Syncing...';
                        engineStatus.style.color = 'var(--accent-color)';
                    }
                } else {
                    if (wasSyncing) {
                        wasSyncing = false;
                        window.location.reload();
                        return;
                    }
                    if (btn) {
                        btn.disabled = false;
                        if (spinner) spinner.style.display = 'none';
                        if (btnText) btnText.innerText = 'Sync Now';
                    }
                    if (engineStatus) {
                        engineStatus.innerText = 'Idle / Ready';
                        engineStatus.style.color = 'var(--success-color)';
                    }
                }
            } catch (err) {
                console.error("Error polling status:", err);
            }
        }

        async function triggerSync() {
            const btn = document.getElementById('sync-btn');
            const spinner = document.getElementById('btn-spinner');
            const btnText = document.getElementById('btn-text');
            const timeSelect = document.getElementById('time-window-select');
            const timeWindow = timeSelect ? timeSelect.value : '1_week';

            if (btn) btn.disabled = true;
            if (spinner) spinner.style.display = 'inline-block';
            if (btnText) btnText.innerText = 'Starting...';

            try {
                const response = await fetch('/api/sync', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ time_window: timeWindow })
                });
                const data = await response.json();
                wasSyncing = true;
                if (btnText) btnText.innerText = 'Syncing...';
            } catch (err) {
                alert('Failed to trigger sync: ' + err);
                if (btn) btn.disabled = false;
                if (spinner) spinner.style.display = 'none';
                if (btnText) btnText.innerText = 'Sync Now';
            }
        }

        // Live status polling every 3 seconds
        setInterval(checkStatus, 3000);
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    email = os.getenv("ZWIFT_EMAIL")
    password = os.getenv("ZWIFT_PASSWORD")
    missing_env = not bool(email and password)

    tokens = load_tokens()
    authenticated = bool(tokens and "access_token" in tokens) or not missing_env
    
    history = load_history()
    activities = get_all_activities()

    sched_status = scheduler.get_status()

    message = request.args.get("message")
    msg_type = request.args.get("msg_type", "info")

    return render_template_string(
        HTML_TEMPLATE,
        authenticated=authenticated,
        missing_env=missing_env,
        total_downloaded=len(activities),
        cron_expr=sched_status.get("cron_expr"),
        next_sync_time=sched_status.get("next_sync_time"),
        is_syncing=sched_status["is_syncing"],
        last_sync=history.get("last_sync"),
        activities=activities,
        message=message,
        msg_type=msg_type,
        default_time_window=os.getenv("SYNC_TIME_WINDOW", "1_week")
    )

@app.route("/api/sync", methods=["POST"])
def api_sync():
    time_window = None
    if request.is_json:
        data = request.get_json() or {}
        time_window = data.get("time_window")
    
    if not time_window:
        time_window = request.args.get("time_window")

    result = scheduler.run_sync(time_window=time_window)
    return jsonify(result)

@app.route("/api/status")
def api_status():
    activities = get_all_activities()
    history = load_history()
    sched_status = scheduler.get_status()
    sched_status["total_downloaded"] = len(activities)
    sched_status["last_sync_history"] = history.get("last_sync")
    return jsonify(sched_status)

def main():
    port = int(os.getenv("PORT", "8085"))
    use_https = os.getenv("USE_HTTPS", "").lower() in ["true", "1", "yes"]
    
    ssl_ctx = None
    if use_https:
        ssl_ctx = ensure_ssl_certs()
        if ssl_ctx:
            logger.info("HTTPS support enabled for web server.")
        else:
            logger.warning("HTTPS requested but SSL certificate generation failed.")

    scheduler.start()

    protocol = "https" if ssl_ctx else "http"
    logger.info(f"Starting dreeve-zwift-connector server on {protocol}://0.0.0.0:{port}...")
    app.run(host="0.0.0.0", port=port, debug=False, ssl_context=ssl_ctx)

if __name__ == "__main__":
    main()
