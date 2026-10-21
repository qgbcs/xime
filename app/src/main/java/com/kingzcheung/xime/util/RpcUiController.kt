package com.kingzcheung.xime.util

import android.content.Context
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import com.kingzcheung.xime.BuildConfig
import com.kingzcheung.xime.rime.RimeEngine
import com.kingzcheung.xime.settings.SchemaManager
import com.kingzcheung.xime.settings.SettingsPreferences
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.ArrayDeque

object RpcUiController {
    private const val MAX_LOG_CHARS = 100 * 1024
    private const val MAX_LOG_LINES = 1000
    private const val MEMORY_LOG_KEY = "log.memory"
    private const val LOG_HEIGHT_KEY = "log.height.dp"
    private const val LOG_HEIGHT_OVERRIDE_KEY = "log.height.override.dp"
    private const val MAX_IME_UI_TRACE_EVENTS = 300
    private const val SUPPORT_INTENT_KEY = "settings.support_intent"
    private const val SUPPORT_INTENT_PREFS = "rpc_ui_settings"
    private const val SUPPORT_INTENT_PREF_KEY = "support_intent"
    private val DEFAULT_SUPPORT_INTENT =
        """
{
    "action": "android.intent.action.VIEW",
    "data": "weixin://",
    "package": "com.tencent.mm",
    "title": "联系我",
    "subtitle": "点击打开微信，手动搜索 wxsucks"
}
        """.trimIndent()

    data class SupportIntentDisplay(val title: String, val subtitle: String)

    /**
     * 活动 IME 提供的"输入/候选"直达测试桥。
     * 由 XimeInputMethodService 在创建时挂载、销毁时摘除；RPC 据此直接驱动
     * 真实输入管线（设置编码/敲单键/点候选/读状态），无需 adb 模拟点击。
     */
    interface ImeTestBridge {
        /** 设置整段编码（setInput + 走生产映射刷新候选栏），返回状态 JSON。 */
        fun type(code: String): String
        /** 真实敲入一个字符（processKey 单键管线），返回状态 JSON。 */
        fun pressKey(letter: String): String
        /** 点击候选（走 selectCandidateAsync 生产路径），返回前后状态和实际上屏文本。 */
        fun pick(index: Int, expectedCandidate: String?): String
        /** 翻到 Rime 下一页并返回候选状态。 */
        fun pageDown(): String
        /** 翻到 Rime 上一页并返回候选状态。 */
        fun pageUp(): String
        /** 清空组合与候选栏，返回状态 JSON。 */
        fun reset(): String
        /** 只读当前完整输入/候选状态 JSON。 */
        fun snapshot(): String
        /** 只读候选来源、学习频率和首屏显示映射。 */
        fun diagnostics(): String
        /** 只读当前 IME 窗口、编辑器和输入/候选摘要。 */
        fun uiSnapshot(): String
    }

    private var imeBridge: ImeTestBridge? = null
    private val imeUiTrace = ArrayDeque<JSONObject>()
    private val imeUiTraceLock = Any()

    /** Record one bounded UI/IME event together with the state observed at that moment. */
    @JvmStatic
    fun recordImeUiEvent(event: String, state: JSONObject) {
        val entry = JSONObject(state.toString())
            .put("event", event)
            .put("timestamp_ms", System.currentTimeMillis())
            .put("elapsed_realtime_ms", android.os.SystemClock.elapsedRealtime())
        synchronized(imeUiTraceLock) {
            if (imeUiTrace.size >= MAX_IME_UI_TRACE_EVENTS) imeUiTrace.removeFirst()
            imeUiTrace.addLast(entry)
        }
    }

    /** Read the bounded event trace; optionally clear it after taking the snapshot. */
    @JvmStatic
    @JvmOverloads
    fun imeUiTrace(clear: Boolean = false): String {
        synchronized(imeUiTraceLock) {
            val events = JSONArray()
            imeUiTrace.forEach { events.put(JSONObject(it.toString())) }
            if (clear) imeUiTrace.clear()
            return JSONObject()
                .put("ok", true)
                .put("capacity", MAX_IME_UI_TRACE_EVENTS)
                .put("count", events.length())
                .put("events", events)
                .toString()
        }
    }

    /** RPC：合并只读窗口快照与最近 UI 事件，方便一次调用检查焦点与菜单路径。 */
    @JvmStatic
    fun imeUiDiagnostics(): String {
        val bridge = imeBridge ?: return imeUnavailable()
        val snapshot = JSONObject(bridge.uiSnapshot())
        val trace = JSONObject(imeUiTrace())
        val events = trace.optJSONArray("events") ?: JSONArray()
        val recentEvents = JSONArray()
        val first = (events.length() - 50).coerceAtLeast(0)
        for (index in first until events.length()) {
            recentEvents.put(JSONObject(events.getJSONObject(index).toString()))
        }
        return JSONObject()
            .put("ok", true)
            .put("snapshot", snapshot)
            .put(
                "trace",
                JSONObject()
                    .put("capacity", trace.optInt("capacity"))
                    .put("count", trace.optInt("count"))
                    .put("recent_events", recentEvents)
            )
            .toString()
    }

    /** 供活动 IME 挂载/摘除测试桥。 */
    fun attachIme(bridge: ImeTestBridge) { imeBridge = bridge }
    fun detachIme(bridge: ImeTestBridge) { if (imeBridge === bridge) imeBridge = null }

    private var context: Context? = null
    private val _state = MutableStateFlow<Map<String, String>>(emptyMap())
    val state: StateFlow<Map<String, String>> = _state.asStateFlow()

    fun initialize(appContext: Context) {
        context = appContext.applicationContext
        val logHeight = SettingsPreferences.getRpcLogHeightDp(appContext.applicationContext)
        val savedSupportIntent = appContext.applicationContext
            .getSharedPreferences(SUPPORT_INTENT_PREFS, Context.MODE_PRIVATE)
            .getString(SUPPORT_INTENT_PREF_KEY, null)
        _state.update { current ->
            buildMap {
                putAll(current)
                put(LOG_HEIGHT_KEY, logHeight.toString())
                put(MEMORY_LOG_KEY, current[MEMORY_LOG_KEY].orEmpty())
                if (savedSupportIntent != null) put(SUPPORT_INTENT_KEY, savedSupportIntent)
            }
        }
    }

    @JvmStatic
    fun setState(key: String, value: String) {
        require(key.isNotBlank()) { "UI state key must not be blank" }
        if (key == LOG_HEIGHT_KEY) {
            val heightDp = value.toFloatOrNull()
                ?: throw IllegalArgumentException("RPC log height must be numeric")
            require(heightDp.isFinite() && heightDp >= 0f) {
                "RPC log height must be a finite non-negative value"
            }
            val boundedHeight = heightDp.coerceAtMost(1000f)
            val appContext = checkNotNull(context) { "controller_not_initialized" }
            SettingsPreferences.setRpcLogHeightDp(appContext, boundedHeight)
            _state.update {
                (it + (LOG_HEIGHT_KEY to boundedHeight.toString())) - LOG_HEIGHT_OVERRIDE_KEY
            }
        } else {
            _state.update { it + (key to value) }
        }
    }

    @JvmStatic
    fun appendLog(message: String) {
        if (message.isBlank()) return
        _state.update { current ->
            val previous = current[MEMORY_LOG_KEY].orEmpty()
            val combined = (previous + message)
            val lines = combined.split('\n')
            val trimmed = if (lines.size > MAX_LOG_LINES) {
                lines.takeLast(MAX_LOG_LINES)
            } else {
                lines
            }.joinToString("\n")
            val bounded = if (trimmed.length > MAX_LOG_CHARS) trimmed.takeLast(MAX_LOG_CHARS) else trimmed
            current + (MEMORY_LOG_KEY to bounded)
        }
    }

    @JvmStatic
    fun clearLog() {
        _state.update { it + (MEMORY_LOG_KEY to "") }
    }

    @JvmStatic
    fun removeState(key: String) {
        _state.update { it - key }
    }

    @JvmStatic
    fun getState(key: String): String? = _state.value[key]

    @JvmStatic
    fun supportIntentDisplay(configJson: String?): SupportIntentDisplay {
        val config = JSONObject(configJson ?: DEFAULT_SUPPORT_INTENT)
        val hasCustomConfig = configJson != null
        val title = config.optString("title").takeIf { it.isNotBlank() }
            ?: if (hasCustomConfig) "快捷入口" else "拨打客服电话"
        val subtitle = config.optString("subtitle").takeIf { it.isNotBlank() }
            ?: config.optString("data").takeIf { it.isNotBlank() }
            ?: config.optString("action").takeIf { it.isNotBlank() }
            ?: if (hasCustomConfig) "说明" else "10086"
        return SupportIntentDisplay(title, subtitle)
    }

    /** RPC：保存设置页快捷入口的 Intent 配置，后续点击入口时使用。 */
    @JvmStatic
    fun setSupportIntent(configJson: String): String {
        val config = try {
            JSONObject(configJson)
        } catch (error: org.json.JSONException) {
            return errorResult("invalid_intent_config:${error.message}")
        }
        val action = config.optString("action").takeIf { it.isNotBlank() }
        val component = config.optString("component").takeIf {
            config.has("component") && !config.isNull("component") && it.isNotBlank()
        }
        if (action == null && component == null) {
            return errorResult("intent_action_or_component_required")
        }

        val appContext = context ?: return errorResult("controller_not_initialized")
        val saved = appContext.getSharedPreferences(SUPPORT_INTENT_PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(SUPPORT_INTENT_PREF_KEY, config.toString())
            .commit()
        if (!saved) return errorResult("support_intent_save_failed")
        _state.update { it + (SUPPORT_INTENT_KEY to config.toString()) }
        return JSONObject().put("ok", true).put("saved", true).toString()
    }

    /** RPC：清除自定义入口配置并恢复默认拨号 10086。 */
    @JvmStatic
    fun clearSupportIntent(): String {
        val appContext = context ?: return errorResult("controller_not_initialized")
        val cleared = appContext.getSharedPreferences(SUPPORT_INTENT_PREFS, Context.MODE_PRIVATE)
            .edit()
            .remove(SUPPORT_INTENT_PREF_KEY)
            .commit()
        if (!cleared) return errorResult("support_intent_clear_failed")
        _state.update { it - SUPPORT_INTENT_KEY }
        return JSONObject().put("ok", true).put("saved", true).toString()
    }

    /** 设置页快捷入口点击时调用；未自定义时默认打开拨号盘并填入 10086。 */
    @JvmStatic
    fun launchSupportIntent(): String =
        startIntent(_state.value[SUPPORT_INTENT_KEY] ?: DEFAULT_SUPPORT_INTENT)

    /**
     * RPC：从 JSON 配置启动 Intent，支持 action、data、type、package、component、
     * categories、flags、chooser 和基本类型 extras。
     * 例如：startIntent('{"action":"android.intent.action.DIAL","data":"tel:10086"}')
     */
    @JvmStatic
    fun startIntent(configJson: String): String {
        val (intent, useChooser, chooserTitle) = try {
            val config = JSONObject(configJson)
            val action = config.optString("action").takeIf { it.isNotBlank() }
            val componentName = config.optString("component").takeIf {
                config.has("component") && !config.isNull("component") && it.isNotBlank()
            }
            if (action == null && componentName == null) {
                return errorResult("intent_action_or_component_required")
            }

            val intent = Intent(action).apply {
                config.optString("data").takeIf {
                    config.has("data") && !config.isNull("data") && it.isNotBlank()
                }?.let { data ->
                    val mimeType = config.optString("type").takeIf {
                        config.has("type") && !config.isNull("type") && it.isNotBlank()
                    }
                    if (mimeType == null) setData(Uri.parse(data))
                    else setDataAndType(Uri.parse(data), mimeType)
                } ?: config.optString("type").takeIf {
                    config.has("type") && !config.isNull("type") && it.isNotBlank()
                }?.let { type -> setType(type) }

                config.optString("package").takeIf {
                    config.has("package") && !config.isNull("package") && it.isNotBlank()
                }?.let(::setPackage)
                componentName?.let { flattened ->
                    val component = android.content.ComponentName.unflattenFromString(flattened)
                        ?: return errorResult("invalid_intent_component")
                    setComponent(component)
                }
                config.optJSONArray("categories")?.let { categories ->
                    for (index in 0 until categories.length()) {
                        val category = categories.optString(index).takeIf(String::isNotBlank)
                            ?: return errorResult("invalid_intent_category")
                        addCategory(category)
                    }
                }
                if (config.has("flags") && !config.isNull("flags")) {
                    val flags = config.opt("flags") as? Number
                        ?: return errorResult("invalid_intent_flags")
                    if (flags.toLong() !in Int.MIN_VALUE.toLong()..Int.MAX_VALUE.toLong()) {
                        return errorResult("invalid_intent_flags")
                    }
                    addFlags(flags.toInt())
                }
                config.optJSONObject("extras")?.let { extras ->
                    val keys = extras.keys()
                    while (keys.hasNext()) {
                        val key = keys.next()
                        putRpcIntentExtra(this, key, extras.get(key))
                    }
                }
            }
            Triple(
                intent,
                config.optBoolean("chooser", false),
                config.optString("chooser_title").takeIf {
                    config.has("chooser_title") && !config.isNull("chooser_title")
                }
            )
        } catch (error: org.json.JSONException) {
            return errorResult("invalid_intent_config:${error.message}")
        } catch (error: IllegalArgumentException) {
            return errorResult("invalid_intent_config:${error.message}")
        }

        return try {
            val appContext = context ?: return errorResult("controller_not_initialized")
            val launchIntent = if (useChooser) {
                Intent.createChooser(intent, chooserTitle)
            } else {
                intent
            }
            launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            appContext.startActivity(launchIntent)
            JSONObject().put("ok", true).put("action", intent.action).toString()
        } catch (error: ActivityNotFoundException) {
            errorResult("intent_activity_not_found:${error.message}")
        } catch (error: SecurityException) {
            errorResult("intent_not_allowed:${error.message}")
        }
    }

    private fun putRpcIntentExtra(intent: Intent, key: String, value: Any) {
        when (value) {
            is String -> intent.putExtra(key, value)
            is Boolean -> intent.putExtra(key, value)
            is Int -> intent.putExtra(key, value)
            is Long -> intent.putExtra(key, value)
            is Double -> intent.putExtra(key, value)
            is JSONObject -> intent.putExtra(key, value.toRpcBundle())
            is JSONArray -> when {
                (0 until value.length()).all { value.opt(it) is String } ->
                    intent.putExtra(key, Array(value.length()) { value.getString(it) })
                (0 until value.length()).all { value.opt(it) is Boolean } ->
                    intent.putExtra(key, BooleanArray(value.length()) { value.getBoolean(it) })
                (0 until value.length()).all { value.opt(it) is Int } ->
                    intent.putExtra(key, IntArray(value.length()) { value.getInt(it) })
                (0 until value.length()).all { value.opt(it) is Number } ->
                    intent.putExtra(key, DoubleArray(value.length()) { value.getDouble(it) })
                else -> throw IllegalArgumentException("unsupported_extra_array:$key")
            }
            else -> throw IllegalArgumentException("unsupported_extra_type:$key")
        }
    }

    private fun JSONObject.toRpcBundle(): Bundle = Bundle().also { bundle ->
        val keys = keys()
        while (keys.hasNext()) {
            val key = keys.next()
            when (val value = get(key)) {
                is String -> bundle.putString(key, value)
                is Boolean -> bundle.putBoolean(key, value)
                is Int -> bundle.putInt(key, value)
                is Long -> bundle.putLong(key, value)
                is Double -> bundle.putDouble(key, value)
                is JSONObject -> bundle.putBundle(key, value.toRpcBundle())
                else -> throw IllegalArgumentException("unsupported_bundle_extra:$key")
            }
        }
    }

    @JvmStatic
    fun buildInfo(): String = JSONObject()
        .put("version_name", BuildConfig.VERSION_NAME)
        .put("version_code", BuildConfig.VERSION_CODE)
        .put("git_hash", BuildConfig.GIT_HASH)
        .put("source_fingerprint", BuildConfig.SOURCE_FINGERPRINT)
        .toString()

    @JvmStatic
    fun clearState() {
        val logHeight = context?.let(SettingsPreferences::getRpcLogHeightDp)
        _state.value = buildMap {
            put(MEMORY_LOG_KEY, "")
            if (logHeight != null) put(LOG_HEIGHT_KEY, logHeight.toString())
        }
    }

    /** RPC：控制设置页条目显隐；about_app 默认隐藏。 */
    @JvmStatic
    fun setSettingItemVisible(itemId: String, visible: Boolean) {
        val appContext = checkNotNull(context) { "controller_not_initialized" }
        SettingsPreferences.setSettingItemVisible(appContext, itemId, visible)
        setState("settings.item.$itemId.visible", visible.toString())
    }

    /** RPC：查询设置页条目是否显示；about_app 默认隐藏。 */
    @JvmStatic
    fun isSettingItemVisible(itemId: String): Boolean {
        val appContext = checkNotNull(context) { "controller_not_initialized" }
        return SettingsPreferences.isSettingItemVisible(appContext, itemId)
    }

    @JvmStatic
    fun listSchemas(): String {
        val appContext = context ?: return JSONObject().put("error", "controller_not_initialized").toString()
        val enabled = SchemaManager.getEnabledSchemas(appContext).toSet()
        val current = SettingsPreferences.getCurrentSchema(appContext)
        val schemas = JSONArray()
        SchemaManager.discoverSchemas(appContext).forEach { schema ->
            schemas.put(JSONObject()
                .put("id", schema.schemaId)
                .put("name", schema.name)
                .put("version", schema.version)
                .put("author", schema.author)
                .put("description", schema.description)
                .put("enabled", schema.schemaId in enabled)
                .put("current", schema.schemaId == current))
        }
        return JSONObject()
            .put("current", current)
            .put("enabled", JSONArray(enabled.toList()))
            .put("schemas", schemas)
            .toString()
    }

    @JvmStatic
    fun setOnlySchema(schemaId: String): String {
        val appContext = context ?: return errorResult("controller_not_initialized")
        val schema = SchemaManager.discoverSchemas(appContext)
            .firstOrNull { it.schemaId == schemaId }
            ?: return errorResult("schema_not_found:$schemaId")

        SchemaManager.setEnabledSchemas(appContext, listOf(schema.schemaId))
        SettingsPreferences.setCurrentSchema(appContext, schema.schemaId)

        val rime = if (RimeEngine.isInitialized()) RimeEngine.getInstance() else null
        val switched = rime?.let { engine ->
            schema.schemaId in engine.getAvailableSchemas() && engine.switchSchema(schema.schemaId)
        } ?: false

        return JSONObject()
            .put("ok", true)
            .put("id", schema.schemaId)
            .put("name", schema.name)
            .put("switched", switched)
            .put("requires_deploy", !switched)
            .toString()
    }

    @JvmStatic
    fun listUserDicts(): String {
        val appContext = context ?: return errorResult("controller_not_initialized")
        val names = File(appContext.filesDir, "rime")
            .listFiles { file -> file.exists() && file.name.endsWith(".userdb") }
            ?.map { it.name.removeSuffix(".userdb") }
            ?.sorted()
            ?: emptyList()
        return JSONObject()
            .put("ok", true)
            .put("dicts", JSONArray(names))
            .toString()
    }

    @JvmStatic
    fun readUserDict(dictName: String): String {
        val appContext = context ?: return errorResult("controller_not_initialized")
        if (!RimeEngine.isInitialized()) return errorResult("rime_not_initialized")
        if (!dictName.matches(Regex("[A-Za-z0-9_.-]+"))) {
            return errorResult("invalid_dict_name")
        }

        val rimeDir = File(appContext.filesDir, "rime")
        val source = File(rimeDir, "$dictName.userdb")
        if (!source.exists()) return errorResult("user_dict_not_found:$dictName")

        val exported = File.createTempFile("rpc-$dictName-", ".userdb.txt", appContext.cacheDir)
        return try {
            val count = RimeEngine.getInstance().exportUserDict(dictName, exported.absolutePath)
            if (count < 0 || !exported.isFile) return errorResult("user_dict_export_failed:$dictName")
            JSONObject()
                .put("ok", true)
                .put("dict", dictName)
                .put("count", count)
                .put("tsv", exported.readText(Charsets.UTF_8))
                .toString()
        } finally {
            exported.delete()
        }
    }

    @JvmStatic
    fun readAllUserDicts(): String {
        val listed = JSONObject(listUserDicts())
        if (!listed.optBoolean("ok", false)) return listed.toString()
        val result = JSONArray()
        val dicts = listed.optJSONArray("dicts") ?: JSONArray()
        for (index in 0 until dicts.length()) {
            result.put(JSONObject(readUserDict(dicts.getString(index))))
        }
        return JSONObject()
            .put("ok", true)
            .put("dicts", result)
            .toString()
    }

    /** 将外部备份的原始 .userdb 文件恢复到 Rime 用户词典目录。重启 Rime 后生效。 */
    @JvmStatic
    fun importUserDict(dictName: String, backupPath: String): String {
        val appContext = context ?: return errorResult("controller_not_initialized")
        if (!dictName.matches(Regex("[A-Za-z0-9_.-]+"))) {
            return errorResult("invalid_dict_name")
        }
        val source = File(backupPath)
        if (!source.isFile) return errorResult("backup_not_found:$backupPath")
        if (source.length() == 0L) return errorResult("backup_empty:$dictName")

        return try {
            val targetDir = File(appContext.filesDir, "rime")
            if (!targetDir.exists() && !targetDir.mkdirs()) {
                return errorResult("rime_dir_create_failed")
            }
            val target = File(targetDir, "$dictName.userdb")
            source.copyTo(target, overwrite = true)
            JSONObject()
                .put("ok", true)
                .put("dict", dictName)
                .put("path", target.absolutePath)
                .put("bytes", target.length())
                .put("restart_required", true)
                .toString()
        } catch (error: Exception) {
            errorResult("user_dict_import_failed:${error.message}")
        }
    }

    private fun errorResult(message: String): String =
        JSONObject().put("ok", false).put("error", message).toString()

    // ── 输入/候选直达测试入口（经 Chaquopy RPC 调用，无 IME 时返回错误）──
    private fun imeUnavailable(): String = errorResult("ime_not_active")

    /** RPC：直接设置编码并刷新候选，如 RpcUiController.imeType('hgi')。 */
    @JvmStatic
    fun imeType(code: String): String = imeBridge?.type(code) ?: imeUnavailable()

    /** RPC：真实敲入一个字符，如 RpcUiController.imeKey('h')。 */
    @JvmStatic
    fun imeKey(letter: String): String {
        if (letter.isEmpty()) return errorResult("empty_letter")
        return imeBridge?.pressKey(letter) ?: imeUnavailable()
    }

    /** RPC：按当前候选栏索引选词。 */
    @JvmStatic
    fun imePick(index: Int): String = imeBridge?.pick(index, null) ?: imeUnavailable()

    /** RPC：按索引和候选词身份选词，用于复现 UI 点击路径。 */
    @JvmStatic
    fun imePick(index: Int, expectedCandidate: String): String =
        imeBridge?.pick(index, expectedCandidate) ?: imeUnavailable()

    /** RPC：翻到 Rime 下一页。 */
    @JvmStatic
    fun imePageDown(): String = imeBridge?.pageDown() ?: imeUnavailable()

    /** RPC：翻到 Rime 上一页。 */
    @JvmStatic
    fun imePageUp(): String = imeBridge?.pageUp() ?: imeUnavailable()

    /** RPC：清空组合与候选栏。 */
    @JvmStatic
    fun imeReset(): String = imeBridge?.reset() ?: imeUnavailable()

    /** RPC：读当前完整输入/候选状态。 */
    @JvmStatic
    fun imeState(): String = imeBridge?.snapshot() ?: imeUnavailable()

    /** RPC：只读候选来源、学习频率与首屏英文缩略映射。 */
    @JvmStatic
    fun imeDiagnostics(): String = imeBridge?.diagnostics() ?: imeUnavailable()

    /** RPC：只读键盘窗口、输入连接/编辑器及当前输入候选状态。 */
    @JvmStatic
    fun imeUiSnapshot(): String = imeBridge?.uiSnapshot() ?: imeUnavailable()
}