# 通过 RPC 操作问题反馈聊天页

本文说明如何用 RPC 控制输入法菜单里的“问题反馈”入口、红点，以及独立 Python 自绘反馈页里的聊天记录。RPC 执行的是手机端 Python 代码；消息只保存在手机本地，不会发送到任何服务器。

## 页面与数据

- 入口：键盘菜单第五项“问题反馈”，点击后打开独立的 `FeedbackActivity`。
- 界面：全部由 `app/src/main/python/feature_feedback.py` 用 Android 原生控件绘制，Kotlin 只做宿主与选图结果转发。
- 消息文件：`filesDir/feedback/messages.json`，图片：`filesDir/feedback/images/`。
- 消息字段：`type` 为 `text` 或 `image`，`text` 为文字，`path` 为图片绝对路径，`ts` 为毫秒时间戳，`sender` 为 `user` 或 `author`（旧消息无此字段按 `user` 处理）。
- 已读语义：打开反馈页或点击菜单反馈项即清除红点链路，不做未读计数。

## 红点链路

`ime.candidate.logo` 与 `ime.menu.feedback` 的 `badge` 是同一枚红点：给任意一端下发 `badge`，另一端自动镜像；清除任意一端或调用 `uiClearTarget`/`uiClearAll` 会同时清掉。

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
# 两端同时出现红点
print(RpcUiController.uiSet("ime.candidate.logo", "badge", '{"dot":true}'))
# 查看当前所有覆盖
print(RpcUiController.uiList())
# 清掉红点链路
print(RpcUiController.uiClearTarget("ime.menu.feedback"))
'''
```

## 自定义反馈菜单项

`ime.menu.feedback` 支持 `label`、`icon`、`visible`、`badge` 四个属性，菜单第五项实时重组。

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
print(RpcUiController.uiSet("ime.menu.feedback", "label", "意见反馈"))
print(RpcUiController.uiSet("ime.menu.feedback", "visible", "false"))
'''
```

## 打开反馈页

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
print(RpcUiController.openFeedback())
'''
```

## 读取本机消息

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
print(RpcUiController.feedbackMessages())
'''
```

返回示例：

```json
{"ok":true,"count":2,"messages":[
  {"type":"text","text":"hello","ts":1793160040584},
  {"type":"text","text":"已收到你的反馈","sender":"author","ts":1793162104035}
]}
```

## 模拟作者回复

`feedbackPush(text, sender)` 向本机聊天记录追加一条消息，默认 `sender` 为 `author`。页面打开时实时追加并自动滚到底，页面未打开时只落盘，下次打开可见。

```python
code = r'''
from com.kingzcheung.xime.util import RpcUiController
print(RpcUiController.feedbackPush("你好，我是反馈作者，已经收到你的反馈了。"))
print(RpcUiController.feedbackPush("这条算用户自己发的", "user"))
'''
```

返回示例：`{"ok": true, "count": 3, "message": {"type": "text", "text": "...", "sender": "author", "ts": 1793161161415}}`。

## 从外部机器调用

在装有 `multi_mqtt` 的机器上把上面的 `code` 交给 `client_mqtt.rpc` 投递到设备执行：

```python
import sys
sys.path.insert(0, '/root/build_xime_home/multi_mqtt')
from client import client_mqtt

code = (
    "from com.kingzcheung.xime.util import RpcUiController; "
    "print(RpcUiController.feedbackPush('你好，我是反馈作者'))"
)
print(client_mqtt.rpc(code, request_topic='Huawei_Nexus6P_568810', timeout=40, k=2**222, mb=6))
```

## 限制

- RPC 只能推送文字消息，图片消息只能在本机通过“图片”按钮选择系统图片产生。
- 消息与图片不会上传，卸载应用或清除应用数据即丢失。
- 反馈页是普通 Activity，不注入输入法窗口，键盘弹出由 `adjustResize` 保证输入栏不被遮挡。
