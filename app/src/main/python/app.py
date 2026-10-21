import os
import sys
import traceback
import threading
import json
import tempfile

MAX_LOG_BYTES = 100 * 1024
MAX_LOG_LINES = 1000

try:
    from java import jclass
    RpcUiController = jclass("com.kingzcheung.xime.util.RpcUiController")
except Exception:  # pragma: no cover - chaquopy/python-only test environments may not expose Java.
    RpcUiController = None

def load_rpc_config(config_path):
    defaults = {
        "http_enabled": True,
        "http_port": 1144,
        "http_host": "0.0.0.0",
        "http_key": "",
        "mqtt_enabled": True,
    }
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            values = json.load(config_file)
        if isinstance(values, dict):
            defaults.update(values)
    except (OSError, ValueError):
        pass
    return defaults


def _ensure_multi_mqtt_path():
    submodule_root = os.path.join(os.path.dirname(__file__), "multi_mqtt")
    if submodule_root not in sys.path:
        sys.path.insert(0, submodule_root)


def get_rpc_config_path():
    _ensure_multi_mqtt_path()
    import server_mqtt
    return os.fspath(server_mqtt.RPC_CONFIG_PATH)


def get_rpc_settings():
    _ensure_multi_mqtt_path()
    import server_mqtt

    stored = server_mqtt._read_rpc_config_document()
    try:
        effective = server_mqtt.rpc_config_get()
    except ValueError:
        fallback_stored = dict(stored)
        if not fallback_stored.get("request_topic"):
            fallback_stored.pop("request_topic", None)
        if not fallback_stored.get("pub_key"):
            fallback_stored.pop("pub_key", None)
        effective = server_mqtt._json_rpc_config(
            server_mqtt._normalize_rpc_config(fallback_stored)
        )
    server = globals().get("mqtt_server")
    mqtt_running = bool(
        server is not None
        and getattr(getattr(server, "mqtt_net", None), "clients", None)
    )
    runtime_pub_key = getattr(
        getattr(server, "mqtt_net", None), "server_public_key_bytes", None
    )
    if isinstance(runtime_pub_key, bytes):
        runtime_pub_key = runtime_pub_key.decode("utf-8", errors="replace")
    if not isinstance(runtime_pub_key, str) or not runtime_pub_key:
        runtime_pub_key = effective["pub_key"] or ""
    settings = {
        "http_enabled": True,
        "http_port": 1144,
        "http_host": "0.0.0.0",
        "http_key": "",
        "mqtt_enabled": (
            mqtt_running
            if "mqtt_server" in globals()
            else bool(stored.get("mqtt_enabled", True))
        ),
        "request_topic": effective["request_topic"],
        "mqtt_reply_topic": getattr(
            server, "reply_topic", server_mqtt.DEFAULT_REPLY_TOPIC
        ),
        "pub_key": runtime_pub_key,
    }
    settings.update({
        key: stored[key]
        for key in (
            "http_enabled",
            "http_port",
            "http_host",
            "http_key",
            "mqtt_reply_topic",
        )
        if key in stored
    })
    settings["request_topic"] = (
        stored.get("request_topic") or effective["request_topic"]
    )
    settings["pub_key"] = stored.get("pub_key") or runtime_pub_key
    return json.dumps(settings, ensure_ascii=False)


def save_rpc_settings(settings_json):
    settings = json.loads(settings_json)
    if not isinstance(settings, dict):
        raise TypeError("RPC 设置必须是 JSON object")
    port = settings.get("http_port", 1144)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("HTTP 端口必须在 1 到 65535 之间")

    _ensure_multi_mqtt_path()
    import server_mqtt

    mqtt_updates = {}
    if settings.get("request_topic"):
        mqtt_updates["request_topic"] = settings["request_topic"]
    if settings.get("pub_key"):
        mqtt_updates["pub_key"] = settings["pub_key"]
    if mqtt_updates:
        server_mqtt._write_rpc_config_updates(mqtt_updates)

    path = server_mqtt.RPC_CONFIG_PATH
    path = os.fspath(path)
    with server_mqtt._RPC_CONFIG_LOCK:
        stored = server_mqtt._read_rpc_config_document()
        for key in (
            "http_enabled",
            "http_port",
            "http_host",
            "http_key",
            "mqtt_enabled",
            "mqtt_reply_topic",
        ):
            if key in settings:
                stored[key] = settings[key]

        parent = os.path.dirname(path) or "."
        fd, temp_path = tempfile.mkstemp(
            prefix=".rpc-settings.", suffix=".json", dir=parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as config_file:
                json.dump(stored, config_file, ensure_ascii=False, indent=2)
                config_file.write("\n")
                config_file.flush()
                os.fsync(config_file.fileno())
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise
    return True


class LimitedLogFile:
    def __init__(self, path, max_bytes=MAX_LOG_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.file = open(path, "a+b", buffering=0)

    def write(self, value):
        data = value.encode("utf-8") if isinstance(value, str) else value
        with self.lock:
            self.file.seek(0, 2)
            if self.file.tell() + len(data) > self.max_bytes:
                keep = self.max_bytes // 2
                self.file.seek(-min(keep, self.file.tell()), 2)
                tail = self.file.read()
                self.file.seek(0)
                self.file.truncate()
                self.file.write(tail)
            self.file.seek(0, 2)
            self.file.write(data)

    def flush(self):
        with self.lock:
            self.file.flush()


class Tee:
    def __init__(self, stream, sinks):
        self.stream = stream
        if sinks is None:
            self.sinks = []
        elif isinstance(sinks, (list, tuple)):
            self.sinks = [sink for sink in sinks if sink is not None]
        else:
            self.sinks = [sinks]

    def write(self, value):
        self.stream.write(value)
        for sink in self.sinks:
            sink.write(value)
            sink.flush()

    def flush(self):
        self.stream.flush()
        for sink in self.sinks:
            sink.flush()


class MemoryLogProxy:
    def __init__(self):
        self.buffer = []

    def write(self, value):
        if not value:
            return
        text = value if isinstance(value, str) else value.decode("utf-8", errors="replace")
        if RpcUiController is not None:
            try:
                RpcUiController.appendLog(text)
            except Exception:
                pass
        self.buffer.append(text)
        total_chars = sum(len(part) for part in self.buffer)
        if total_chars > MAX_LOG_BYTES:
            trimmed = ''.join(self.buffer)
            trimmed = trimmed[-MAX_LOG_BYTES:]
            self.buffer = trimmed.splitlines(keepends=True)
        if len(self.buffer) > MAX_LOG_LINES:
            self.buffer = self.buffer[-MAX_LOG_LINES:]

    def flush(self):
        pass


def start(log_path, files_dir=None):
    global mqtt_server,http_server,gms
    memory_sink = MemoryLogProxy()
    file_sink = None
    if log_path:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        file_sink = LimitedLogFile(log_path)

    sinks = [memory_sink]
    if file_sink is not None:
        sinks.append(file_sink)

    sys.stdout = Tee(sys.__stdout__, sinks)
    sys.stderr = Tee(sys.__stderr__, sinks)
    print("[PYTHON] Chaquopy RPC bootstrap started")
    try:
        import runtime_pip
        runtime_pip.configure(files_dir)
        # Import after stdout/stderr redirection so logging.basicConfig in the
        # MQTT and HTTP modules writes into the same log shown by the settings UI.
        import logging
        _ensure_multi_mqtt_path()
        import server_http,server_mqtt
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            force=True,
        )
        config = load_rpc_config(server_mqtt.RPC_CONFIG_PATH)
        mqtt_config = server_mqtt.load_rpc_config()
        mqtt_server = None
        gms = None
        if config.get('http_enabled', True):
            http_server= server_http.start_rpc_server(
                port=int(config.get('http_port', 1144)),
                ip=str(config.get('http_host', '0.0.0.0')),
                key=str(config.get('http_key', '')),
                globals=globals(),
                locals=locals(),
            )
        else:
            http_server = None
            
        if config.get("mqtt_enabled", True):
            reply_topic = str(config.get("mqtt_reply_topic") or server_mqtt.DEFAULT_REPLY_TOPIC)
            mqtt_server = server_mqtt.MQTTServer(
                server_public_key_bytes=mqtt_config["pub_key"],
                reply_topic=reply_topic,
                globals=globals(),
            )
            gms = mqtt_server  # 设置页统计按钮使用此 MQTT 服务实例
            mqtt_server.start(block=False)

        import task_manager
        task_manager.init(files_dir or ".")

        print(f"[app.py] {config} loaded, HTTP={http_server} MQTT={mqtt_server} ")
        return True
    except Exception:
        traceback.print_exc()
        return False


def mqtt_stats_report():
    """设置页按钮入口：执行 gms.mqtt_net.stats.get_report() 并把报告打印进运行日志。"""
    server = globals().get("gms")
    if server is None:
        print("[app.py] MQTT 未启动，暂无连接质量统计")
        return False
    print(gms.mqtt_net.stats.get_report())
    return True
