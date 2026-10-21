# 通过 RPC 查询和修改输入法候选栏按钮

本文说明如何通过应用已有的 Python RPC 查询、设置候选栏工具按钮。RPC 执行的是手机端 Python 代码；下面的命令通过应用 `SharedPreferences` 读写正式设置，不是临时 UI 状态。

## 按钮配置的行为

- 设置保存在应用私有 `kime_settings` SharedPreferences 中，键名为 `toolbar_buttons`。
- 值是逗号分隔的按钮 ID；顺序就是自定义按钮的显示顺序。
- 默认值为空，因此默认不显示可选工具按钮。
- 手机当前配置（RPC 查询结果）：`edit`、`home`、`end`、`copy`、`paste`、`clipboard`。
- RPC 写入 SharedPreferences 后会持久保存，应用/进程重启后仍保留；清除应用数据或卸载应用会清除该设置。
- 目前可用 ID：

| ID | 按钮 |
| --- | --- |
| `emoji` | 表情 |
| `clipboard` | 剪贴板 |
| `schema` | 方案选择 |
| `quick_phrase` | 快捷发送 |
| `symbol` | 符号 |
| `float` | 悬浮 |
| `select_all` | 全选 |
| `copy` | 复制 |
| `paste` | 粘贴 |
| `home` | 段首 |
| `end` | 段尾 |
| `handwriting_lookup` | 手写找字 |
| `edit` | 编辑 |
| `voice` | 语音 |

手写找字按钮只有在设备已具备手写模型时才会出现在可选列表中。设置该 ID 不会安装模型或绕过这一条件。

## 查询当前配置

从本地 Python RPC 客户端向目标设备执行：

```python
code = r'''
from com.kingzcheung.xime import XimeApplication
from com.kingzcheung.xime.keyboard import ToolbarButton

app = XimeApplication.getInstance()
prefs = app.getSharedPreferences("kime_settings", 0)
raw = prefs.getString("toolbar_buttons", "")
buttons = [item for item in str(raw).split(",") if item]
available = [str(button.getId()) for button in ToolbarButton.values()]
print({"stored": buttons, "available_ids": available})
'''
```

若 `stored` 是空列表，表示没有配置任何可选按钮。该值与 `RpcUiController.getState()` 无关：`getState()` 读取的是进程内临时 UI 状态，不是这个持久化偏好。

将上面的 `code` 变量传给本地 `client_mqtt.rpc(code, request_topic="设备 topic", ...)` 即可执行；本地 RPC 客户端调用不属于手机端代码。

## 修改按钮

将 `buttons` 改为需要显示的 ID 列表。以下示例设置表情、剪贴板、方案选择，并按该顺序显示：

```python
code = r'''
from com.kingzcheung.xime import XimeApplication
from com.kingzcheung.xime.keyboard import ToolbarButton

app = XimeApplication.getInstance()
prefs = app.getSharedPreferences("kime_settings", 0)
buttons = ["emoji", "clipboard", "schema"]
available = {str(button.getId()) for button in ToolbarButton.values()}
unknown = [button for button in buttons if button not in available]
if unknown:
    raise ValueError("未知候选栏按钮 ID: " + ", ".join(unknown))

prefs.edit().putString("toolbar_buttons", ",".join(buttons)).apply()
print({"saved": [item for item in str(prefs.getString("toolbar_buttons", "")).split(",") if item]})
'''
```

将 `buttons` 设为 `[]` 会清空可选按钮。也可以执行下面这段完整 RPC 代码：

```python
code = r'''
from com.kingzcheung.xime import XimeApplication

app = XimeApplication.getInstance()
prefs = app.getSharedPreferences("kime_settings", 0)
prefs.edit().putString("toolbar_buttons", "").apply()
print({"stored": ""})
'''
```

## 生效时间与验证

输入法设置界面的候选栏自定义面板会通过同一个偏好键保存按钮，并立即更新当前键盘的 UI 状态。通过 RPC 直接写入偏好时，没有 RPC 接口通知活动输入法服务刷新这项状态；`toolbar_buttons` 也没有注册为独立的 SharedPreferences 即时监听键。

因此，RPC 写入成功后应重新触发输入法输入会话（例如切换到其他输入框再回来；必要时关闭并重新打开输入法），然后检查候选栏。服务开始新的输入会话时会重新读取偏好。若只在当前已打开的候选栏里观察，可能暂时仍看到旧按钮。

确认持久化结果时，再运行“查询当前配置”命令。`stored` 反映 SharedPreferences 中保存的真实值；它不单独证明当前屏幕已经刷新。

## 实现位置

- 按钮 ID、名称和默认集合：`app/src/main/java/com/kingzcheung/xime/keyboard/ToolbarButton.kt`
- 偏好键及读写：`app/src/main/java/com/kingzcheung/xime/settings/SettingsPreferences.kt`
- 候选栏按钮自定义 UI：`app/src/main/java/com/kingzcheung/xime/ui/menubar/ToolbarCustomizeView.kt`
- 自定义面板的持久化和即时 UI 更新：`app/src/main/java/com/kingzcheung/xime/service/ImeKeyboardCallbacks.kt`
- 输入会话读取已保存按钮：`app/src/main/java/com/kingzcheung/xime/service/XimeInputMethodService.kt`

当前 `RpcUiController` 没有专门的候选栏按钮 get/set RPC 方法；本文命令使用 `XimeApplication` 提供的应用上下文访问同一份偏好设置。
