@file:Suppress("DEPRECATION")

package com.kingzcheung.xime.task

import android.bluetooth.BluetoothAdapter
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.display.DisplayManager
import android.media.AudioManager
import android.net.ConnectivityManager
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.view.Display
import com.chaquo.python.Python
import com.kingzcheung.xime.util.FileLogger
import org.json.JSONArray
import org.json.JSONObject

/**
 * 系统广播监听（主进程动态注册一次，无清单接收器）。
 *
 * 与 NetworkEventMonitor 分工：后者只产生网络边沿语义事件，这里覆盖其余系统广播。
 * 控耗电的关键是“默认不进 Python”：
 *  - 收到广播只做两件事：写 Kotlin 侧环形缓冲；仅当 Python 侧有启用的事件任务
 *    订阅了该事件时，才调用 task_manager.fire_event。否则完全是 Java 侧记账。
 *  - 经典广播由系统派发，不轮询、不持 wakelock、不起前台服务；回调跑在独立
 *    HandlerThread 上，不占主线程。
 *
 * 缓冲：上限 500 行，每行 `epoch_ms | event_name | payload_json`，
 * 通过 RPC `task_manager.system_events(limit)` 读取。
 */
object SystemEventMonitor {

    private const val TAG = "SysEventMonitor"
    private const val PY_MODULE = "task_manager"
    private const val PY_METHOD = "fire_event"
    private const val LOG_MAX = 500

    /** 屏幕旋转事件名：Android 没有旋转广播，只能由 DisplayManager 回调产生。 */
    private const val ROTATION_EVENT = "screen_rotation_changed"

    private val lock = Any()
    private val log = ArrayDeque<String>()

    @Volatile
    private var registered: Set<String> = emptySet()
    private var started = false
    private lateinit var handler: Handler

    /** 最近一次默认屏幕旋转值（Surface.ROTATION_*）；-1 表示不可得。 */
    private var lastRotation = -1

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            record(intent)
        }
    }

    /** Python 事件任务变更后回写订阅集合（JSON 数组字符串，如 ["screen_on"]）。 */
    @JvmStatic
    fun syncRegisteredEvents(jsonArray: String?) {
        val set = try {
            val arr = JSONArray(jsonArray ?: "[]")
            (0 until arr.length())
                .map { arr.optString(it) }
                .filter { it.isNotEmpty() }
                .toSet()
        } catch (t: Throwable) {
            FileLogger.w(TAG, "syncRegisteredEvents 解析失败: ${t.javaClass.simpleName}")
            emptySet()
        }
        synchronized(lock) { registered = set }
        FileLogger.i(TAG, "Python 订阅的系统事件: $set")
    }

    /** RPC 回读缓冲：返回 JSON 数组，每个元素是一行文本（含毫秒时间戳）。 */
    @JvmStatic
    fun getEventLog(limit: Int): String {
        val n = if (limit <= 0) LOG_MAX else minOf(limit, LOG_MAX)
        val out = JSONArray()
        synchronized(lock) {
            val from = maxOf(0, log.size - n)
            log.drop(from).forEach { out.put(it) }
        }
        return out.toString()
    }

    @JvmStatic
    fun clearEventLog() {
        synchronized(lock) { log.clear() }
    }

    /** 当前生效的 Python 订阅集合（调试用，JSON 数组字符串）。 */
    @JvmStatic
    fun getRegisteredEvents(): String {
        return JSONArray(synchronized(lock) { registered.sorted() }).toString()
    }

    @JvmStatic
    fun start(context: Context) {
        if (started) return
        started = true
        val thread = HandlerThread("sys-event-monitor").apply { start() }
        handler = Handler(thread.looper)
        val ctx = context.applicationContext

        val base = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_SCREEN_OFF)
            addAction(Intent.ACTION_USER_PRESENT)
            addAction(Intent.ACTION_BATTERY_LOW)
            addAction(Intent.ACTION_BATTERY_OKAY)
            addAction(Intent.ACTION_BATTERY_CHANGED)
            addAction(Intent.ACTION_POWER_CONNECTED)
            addAction(Intent.ACTION_POWER_DISCONNECTED)
            addAction(Intent.ACTION_AIRPLANE_MODE_CHANGED)
            addAction(Intent.ACTION_HEADSET_PLUG)
            addAction(WifiManager.WIFI_STATE_CHANGED_ACTION)
            addAction(ConnectivityManager.CONNECTIVITY_ACTION)
            addAction(AudioManager.RINGER_MODE_CHANGED_ACTION)
            addAction(BluetoothAdapter.ACTION_STATE_CHANGED)
            addAction(Intent.ACTION_SHUTDOWN)
            addAction(Intent.ACTION_LOCALE_CHANGED)
            addAction(Intent.ACTION_TIME_CHANGED)
            addAction(Intent.ACTION_TIMEZONE_CHANGED)
            addAction(Intent.ACTION_INPUT_METHOD_CHANGED)
        }
        val pkg = IntentFilter().apply {
            addAction(Intent.ACTION_PACKAGE_ADDED)
            addAction(Intent.ACTION_PACKAGE_REMOVED)
            addAction(Intent.ACTION_PACKAGE_REPLACED)
            addDataScheme("package")
        }
        val media = IntentFilter().apply {
            addAction(Intent.ACTION_MEDIA_MOUNTED)
            addAction(Intent.ACTION_MEDIA_UNMOUNTED)
            addAction(Intent.ACTION_MEDIA_REMOVED)
            addAction(Intent.ACTION_MEDIA_BAD_REMOVAL)
            addAction(Intent.ACTION_MEDIA_EJECT)
            addDataScheme("file")
        }

        register(ctx, base)
        register(ctx, pkg)
        register(ctx, media)
        startRotationListener(ctx)
        FileLogger.i(TAG, "系统广播监听已注册，缓冲上限 $LOG_MAX")
    }

    private fun register(context: Context, filter: IntentFilter) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                context.registerReceiver(
                    receiver, filter, null, handler, Context.RECEIVER_EXPORTED,
                )
            } else {
                context.registerReceiver(receiver, filter, null, handler)
            }
        } catch (t: Throwable) {
            FileLogger.e(TAG, "注册广播失败: ${filter.countActions()} actions", t)
        }
    }

    private fun record(intent: Intent) {
        val action = intent.action ?: return
        val name = nameOf(action) ?: return
        append(name, buildPayload(intent, action))
    }

    /**
     * 屏幕旋转监听：旋转不是广播，只能用 DisplayManager 回调。
     *
     * onDisplayChanged 在很多场景（刷新率、HDR、分辨率）都会回调，所以只在
     * 默认屏幕的 rotation 真正变化时记一行；180° 翻转也能被捕获。
     */
    private fun startRotationListener(context: Context) {
        try {
            val dm = context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager
            lastRotation = rotationOf(dm)
            val listener = object : DisplayManager.DisplayListener {
                override fun onDisplayAdded(displayId: Int) {}
                override fun onDisplayRemoved(displayId: Int) {}

                override fun onDisplayChanged(displayId: Int) {
                    if (displayId != Display.DEFAULT_DISPLAY) return
                    val rotation = rotationOf(dm)
                    if (rotation < 0 || rotation == lastRotation) return
                    lastRotation = rotation
                    append(
                        ROTATION_EVENT,
                        JSONObject()
                            .put("ts", System.currentTimeMillis())
                            .put("rotation", rotation)
                            .put("degrees", rotation * 90),
                    )
                }
            }
            dm.registerDisplayListener(listener, handler)
            FileLogger.i(TAG, "屏幕旋转监听已注册，初始 rotation=$lastRotation")
        } catch (t: Throwable) {
            FileLogger.w(TAG, "屏幕旋转监听注册失败: ${t.javaClass.simpleName}")
        }
    }

    private fun rotationOf(dm: DisplayManager): Int = try {
        dm.getDisplay(Display.DEFAULT_DISPLAY)?.rotation ?: -1
    } catch (t: Throwable) {
        -1
    }

    /** 写环形缓冲；仅当 Python 侧有启用的事件任务订阅了该事件时才投递。 */
    private fun append(name: String, payload: JSONObject) {
        val line = "${System.currentTimeMillis()} | $name | $payload"
        val deliver: Boolean
        synchronized(lock) {
            log.addLast(line)
            while (log.size > LOG_MAX) log.removeFirst()
            deliver = registered.contains(name)
        }
        if (!deliver) return
        deliverToPython(name, payload.toString())
    }

    private fun deliverToPython(name: String, payload: String) {
        if (!Python.isStarted()) return
        try {
            Python.getInstance().getModule(PY_MODULE).callAttr(PY_METHOD, name, payload)
        } catch (t: Throwable) {
            FileLogger.w(TAG, "fire_event 投递失败 $name: ${t.javaClass.simpleName}")
        }
    }

    private fun nameOf(action: String): String? = when (action) {
        Intent.ACTION_SCREEN_ON -> "screen_on"
        Intent.ACTION_SCREEN_OFF -> "screen_off"
        Intent.ACTION_USER_PRESENT -> "user_present"
        Intent.ACTION_BATTERY_LOW -> "battery_low"
        Intent.ACTION_BATTERY_OKAY -> "battery_okay"
        Intent.ACTION_BATTERY_CHANGED -> "battery_changed"
        Intent.ACTION_POWER_CONNECTED -> "power_connected"
        Intent.ACTION_POWER_DISCONNECTED -> "power_disconnected"
        Intent.ACTION_AIRPLANE_MODE_CHANGED -> "airplane_mode_changed"
        Intent.ACTION_HEADSET_PLUG -> "headset_plug"
        Intent.ACTION_PACKAGE_ADDED -> "package_added"
        Intent.ACTION_PACKAGE_REMOVED -> "package_removed"
        Intent.ACTION_PACKAGE_REPLACED -> "package_replaced"
        WifiManager.WIFI_STATE_CHANGED_ACTION -> "wifi_state_changed"
        ConnectivityManager.CONNECTIVITY_ACTION -> "connectivity_changed"
        AudioManager.RINGER_MODE_CHANGED_ACTION -> "ringer_mode_changed"
        BluetoothAdapter.ACTION_STATE_CHANGED -> "bluetooth_state_changed"
        Intent.ACTION_MEDIA_MOUNTED -> "media_mounted"
        Intent.ACTION_MEDIA_UNMOUNTED -> "media_unmounted"
        Intent.ACTION_MEDIA_REMOVED -> "media_removed"
        Intent.ACTION_MEDIA_BAD_REMOVAL -> "media_bad_removal"
        Intent.ACTION_MEDIA_EJECT -> "media_eject"
        Intent.ACTION_SHUTDOWN -> "shutdown"
        Intent.ACTION_LOCALE_CHANGED -> "locale_changed"
        Intent.ACTION_TIME_CHANGED -> "time_changed"
        Intent.ACTION_TIMEZONE_CHANGED -> "timezone_changed"
        Intent.ACTION_INPUT_METHOD_CHANGED -> "input_method_changed"
        else -> null
    }

    private fun buildPayload(intent: Intent, action: String): JSONObject {
        val p = JSONObject().put("ts", System.currentTimeMillis()).put("action", action)
        when (action) {
            Intent.ACTION_BATTERY_CHANGED -> p
                .put("level", intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1))
                .put("scale", intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1))
                .put("status", intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1))
                .put("plugged", intent.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1))
                .put("temperature", intent.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1))
                .put("voltage", intent.getIntExtra(BatteryManager.EXTRA_VOLTAGE, -1))
            Intent.ACTION_AIRPLANE_MODE_CHANGED ->
                p.put("state", intent.getBooleanExtra("state", false))
            Intent.ACTION_HEADSET_PLUG -> p
                .put("state", intent.getIntExtra("state", -1))
                .put("microphone", intent.getIntExtra("microphone", -1))
            WifiManager.WIFI_STATE_CHANGED_ACTION ->
                p.put("state", intent.getIntExtra(WifiManager.EXTRA_WIFI_STATE, -1))
            ConnectivityManager.CONNECTIVITY_ACTION ->
                p.put(
                    "no_connectivity",
                    intent.getBooleanExtra(ConnectivityManager.EXTRA_NO_CONNECTIVITY, false),
                )
            AudioManager.RINGER_MODE_CHANGED_ACTION ->
                p.put("mode", intent.getIntExtra(AudioManager.EXTRA_RINGER_MODE, -1))
            BluetoothAdapter.ACTION_STATE_CHANGED ->
                p.put("state", intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1))
            Intent.ACTION_PACKAGE_ADDED,
            Intent.ACTION_PACKAGE_REMOVED,
            Intent.ACTION_PACKAGE_REPLACED -> p
                .put("package", intent.data?.schemeSpecificPart ?: "")
                .put("replacing", intent.getBooleanExtra(Intent.EXTRA_REPLACING, false))
            Intent.ACTION_MEDIA_MOUNTED,
            Intent.ACTION_MEDIA_UNMOUNTED,
            Intent.ACTION_MEDIA_REMOVED,
            Intent.ACTION_MEDIA_BAD_REMOVAL,
            Intent.ACTION_MEDIA_EJECT -> p.put("path", intent.dataString ?: "")
        }
        return p
    }
}
