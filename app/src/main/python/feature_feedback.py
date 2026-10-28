"""反馈聊天页：全部界面元素由 Python 调用 Android 原生控件绘制。

约束：
- build_view 在 FeedbackActivity.onCreate 主线程执行，只建 View，不做耗时操作；
- 所有 JVM->Python 回调都包 try/except，异常只记日志，绝不穿透杀进程；
- 同一种接口的 dynamic_proxy 类全进程只建一次并缓存，避免二次建类导致回调崩溃；
- 消息仅写入 filesDir/feedback/messages.json，图片仅存入 filesDir/feedback/images，
  不向任何服务器发送。
"""
import json
import os
import time

_DATA_SUBDIR = "feedback"
_IMAGE_SUBDIR = "images"
_REQUEST_PICK_IMAGE = 1001

_PAGE_BG = "#F5F6FA"
_BAR_BG = "#FFFFFF"
_INK = "#111827"
_MUTED = "#6B7280"
_ACCENT = "#1A73E8"
_BUBBLE_BG = "#1A73E8"
_BUBBLE_TEXT = "#FFFFFF"
_AUTHOR_BG = "#FFFFFF"
_AUTHOR_TEXT = "#111827"
_HINT_BG = "#EDE9FE"

_PROXIES = {}
_ACTIVE = {}


def _dp(context, value):
    return int(value * context.getResources().getDisplayMetrics().density + 0.5)


def _color(value):
    from android.graphics import Color
    return Color.parseColor(value)


def _log(where, error):
    detail = "%s failed: %s: %s" % (where, type(error).__name__, error)
    try:
        from android.util import Log
        Log.e("xime-feedback", detail[:500])
    except Exception:
        print(detail[:500])


def _click_class():
    cls = _PROXIES.get("click")
    if cls is None:
        from android.view import View
        from java import dynamic_proxy

        class OnClickProxy(dynamic_proxy(View.OnClickListener)):
            def __init__(self, fn=None):
                super().__init__()
                self.fn = fn

            def onClick(self, _view):
                if self.fn is not None:
                    try:
                        self.fn()
                    except BaseException as error:
                        _log("click", error)

        cls = _PROXIES["click"] = OnClickProxy
    return cls


def _runnable_class():
    cls = _PROXIES.get("runnable")
    if cls is None:
        from java import dynamic_proxy
        from java.lang import Runnable

        class RunnableProxy(dynamic_proxy(Runnable)):
            def __init__(self, fn=None):
                super().__init__()
                self.fn = fn

            def run(self):
                if self.fn is not None:
                    try:
                        self.fn()
                    except BaseException as error:
                        _log("runnable", error)

        cls = _PROXIES["runnable"] = RunnableProxy
    return cls


def _layout_class():
    cls = _PROXIES.get("layout")
    if cls is None:
        from android.view import View
        from java import dynamic_proxy

        class LayoutProxy(dynamic_proxy(View.OnLayoutChangeListener)):
            def __init__(self, fn=None):
                super().__init__()
                self.fn = fn

            def onLayoutChange(self, view, left, top, right, bottom,
                               old_left, old_top, old_right, old_bottom):
                if self.fn is not None:
                    try:
                        self.fn()
                    except BaseException as error:
                        _log("layout", error)

        cls = _PROXIES["layout"] = LayoutProxy
    return cls


def _post(fn):
    """投递到主线程：RPC 线程直接改 View 会崩，必须走 Handler。"""
    from android.os import Handler, Looper
    Handler(Looper.getMainLooper()).post(_runnable_class()(fn))


def _data_dir(context):
    root = os.path.join(str(context.getFilesDir().getAbsolutePath()), _DATA_SUBDIR)
    images = os.path.join(root, _IMAGE_SUBDIR)
    if not os.path.isdir(images):
        os.makedirs(images)
    return root, images


def _messages_path(context):
    root, _ = _data_dir(context)
    return os.path.join(root, "messages.json")


def _load_messages(context):
    path = _messages_path(context)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception as error:
        _log("load messages", error)
        return []


def _save_messages(context, messages):
    try:
        with open(_messages_path(context), "w", encoding="utf-8") as handle:
            json.dump(messages, handle, ensure_ascii=False)
    except Exception as error:
        _log("save messages", error)


def _rounded(context, color, radius_dp, stroke_color=None, stroke_dp=0):
    from android.graphics.drawable import GradientDrawable
    drawable = GradientDrawable()
    drawable.setShape(GradientDrawable.RECTANGLE)
    drawable.setColor(_color(color))
    drawable.setCornerRadius(float(_dp(context, radius_dp)))
    if stroke_color is not None and stroke_dp:
        drawable.setStroke(_dp(context, stroke_dp), _color(stroke_color))
    return drawable


def _time_label(ts):
    try:
        return time.strftime("%H:%M", time.localtime(int(ts) / 1000.0))
    except Exception:
        return ""


def _bubble(context, message):
    from android.graphics import BitmapFactory
    from android.view import ViewGroup, Gravity
    from android.widget import ImageView, LinearLayout, TextView

    match = ViewGroup.LayoutParams.MATCH_PARENT
    wrap = ViewGroup.LayoutParams.WRAP_CONTENT

    # 反馈作者的消息靠左白底，用户自己的消息靠右蓝底
    is_author = str(message.get("sender") or "user") == "author"
    align = Gravity.START if is_author else Gravity.END
    bubble_bg = _AUTHOR_BG if is_author else _BUBBLE_BG
    bubble_text = _AUTHOR_TEXT if is_author else _BUBBLE_TEXT

    row = LinearLayout(context)
    row.setOrientation(LinearLayout.HORIZONTAL)
    row.setGravity(align)
    row.setPadding(0, 0, 0, _dp(context, 10))

    column = LinearLayout(context)
    column.setOrientation(LinearLayout.VERTICAL)
    column.setGravity(align)

    if message.get("type") == "image":
        image = ImageView(context)
        image.setAdjustViewBounds(True)
        image.setScaleType(ImageView.ScaleType.FIT_CENTER)
        image.setBackground(_rounded(context, "#FFFFFF", 12, "#E5E7EB", 1))
        image.setPadding(_dp(context, 4), _dp(context, 4), _dp(context, 4), _dp(context, 4))
        try:
            bitmap = BitmapFactory.decodeFile(str(message.get("path") or ""))
            if bitmap is not None:
                image.setImageBitmap(bitmap)
        except Exception as error:
            _log("decode image", error)
        column.addView(
            image,
            LinearLayout.LayoutParams(_dp(context, 200), wrap),
        )
    else:
        text = TextView(context)
        text.setText(str(message.get("text") or ""))
        text.setTextSize(15)
        text.setTextColor(_color(bubble_text))
        text.setBackground(_rounded(context, bubble_bg, 14))
        text.setPadding(_dp(context, 12), _dp(context, 8), _dp(context, 12), _dp(context, 8))
        text.setMaxWidth(_dp(context, 280))
        column.addView(text, LinearLayout.LayoutParams(wrap, wrap))

    stamp = TextView(context)
    stamp.setText(_time_label(message.get("ts") or 0))
    stamp.setTextSize(10)
    stamp.setTextColor(_color(_MUTED))
    stamp.setGravity(align)
    stamp.setPadding(0, _dp(context, 4), 0, 0)
    column.addView(stamp, LinearLayout.LayoutParams(wrap, wrap))

    row.addView(column, LinearLayout.LayoutParams(wrap, wrap))
    return row


def build_view(context):
    from android.view import ViewGroup, Gravity
    from android.widget import Button, EditText, LinearLayout, ScrollView, TextView
    from android.text import InputType
    from android.graphics import Typeface

    match = ViewGroup.LayoutParams.MATCH_PARENT
    wrap = ViewGroup.LayoutParams.WRAP_CONTENT

    state = {"messages": _load_messages(context)}

    root = LinearLayout(context)
    root.setOrientation(LinearLayout.VERTICAL)
    root.setBackgroundColor(_color(_PAGE_BG))
    root.setLayoutParams(ViewGroup.LayoutParams(match, match))

    # ---- 顶部栏 ----
    bar = LinearLayout(context)
    bar.setOrientation(LinearLayout.HORIZONTAL)
    bar.setGravity(Gravity.CENTER_VERTICAL)
    bar.setBackgroundColor(_color(_BAR_BG))
    bar.setPadding(_dp(context, 10), 0, _dp(context, 12), 0)
    root.addView(bar, LinearLayout.LayoutParams(match, _dp(context, 52)))

    back = TextView(context)
    back.setText("\u2190")
    back.setTextSize(20)
    back.setTextColor(_color(_INK))
    back.setGravity(Gravity.CENTER)
    back.setBackground(_rounded(context, "#E8EAF6", 16))
    back.setLayoutParams(LinearLayout.LayoutParams(_dp(context, 32), _dp(context, 32)))
    bar.addView(back)

    title = TextView(context)
    title.setText("问题反馈")
    title.setTextSize(17)
    title.setTypeface(Typeface.DEFAULT, Typeface.BOLD)
    title.setTextColor(_color(_INK))
    title.setPadding(_dp(context, 10), 0, 0, 0)
    bar.addView(title, LinearLayout.LayoutParams(0, wrap, 1.0))

    # ---- 消息区 ----
    scroll = ScrollView(context)
    scroll.setFillViewport(True)
    root.addView(scroll, LinearLayout.LayoutParams(match, 0, 1.0))

    list_box = LinearLayout(context)
    list_box.setOrientation(LinearLayout.VERTICAL)
    list_box.setPadding(_dp(context, 12), _dp(context, 12), _dp(context, 12), _dp(context, 12))
    scroll.addView(list_box, ViewGroup.LayoutParams(match, wrap))
    # 键盘弹出/收起会改变 ScrollView 高度，布局一变化就重新贴底
    scroll.addOnLayoutChangeListener(
        _layout_class()(lambda: scroll.scrollTo(0, list_box.getHeight()))
    )

    # ---- 输入区 ----
    input_bar = LinearLayout(context)
    input_bar.setOrientation(LinearLayout.HORIZONTAL)
    input_bar.setGravity(Gravity.CENTER_VERTICAL)
    input_bar.setBackgroundColor(_color(_BAR_BG))
    input_bar.setPadding(_dp(context, 8), _dp(context, 8), _dp(context, 8), _dp(context, 8))
    root.addView(input_bar, LinearLayout.LayoutParams(match, wrap))

    pick = Button(context)
    pick.setText("图片")
    pick.setTextSize(13)
    input_bar.addView(pick, LinearLayout.LayoutParams(wrap, _dp(context, 42)))

    editor = EditText(context)
    editor.setHint("输入反馈内容...")
    editor.setTextSize(15)
    editor.setTextColor(_color(_INK))
    editor.setHintTextColor(_color(_MUTED))
    editor.setSingleLine(True)
    editor.setInputType(InputType.TYPE_CLASS_TEXT)
    editor.setBackground(_rounded(context, "#F1F3F5", 10))
    editor.setPadding(_dp(context, 12), _dp(context, 8), _dp(context, 12), _dp(context, 8))
    editor_params = LinearLayout.LayoutParams(0, _dp(context, 42), 1.0)
    editor_params.setMargins(_dp(context, 8), 0, _dp(context, 8), 0)
    input_bar.addView(editor, editor_params)

    send = Button(context)
    send.setText("发送")
    send.setTextSize(13)
    input_bar.addView(send, LinearLayout.LayoutParams(wrap, _dp(context, 42)))

    def scroll_bottom():
        try:
            def to_bottom():
                # fullScroll 依赖可聚焦子控件，气泡列表里没有，只能用内容高度直接贴底
                scroll.scrollTo(0, list_box.getHeight())
            scroll.post(_runnable_class()(to_bottom))
            # 首次 post 时布局还没量完，图片留言会停在半路；再补一帧确保真正贴底。
            scroll.postDelayed(_runnable_class()(to_bottom), 80)
        except Exception as error:
            _log("scroll bottom", error)

    def render():
        list_box.removeAllViews()
        messages = state["messages"]
        if not messages:
            hint = TextView(context)
            hint.setText("还没有反馈记录。消息只保存在本机，不会发送。")
            hint.setTextSize(13)
            hint.setTextColor(_color(_MUTED))
            hint.setGravity(Gravity.CENTER)
            hint.setBackground(_rounded(context, _HINT_BG, 10))
            hint.setPadding(_dp(context, 12), _dp(context, 12), _dp(context, 12), _dp(context, 12))
            params = LinearLayout.LayoutParams(match, wrap)
            params.setMargins(0, _dp(context, 16), 0, 0)
            list_box.addView(hint, params)
            return
        for message in messages:
            list_box.addView(_bubble(context, message), LinearLayout.LayoutParams(match, wrap))

    def append(message):
        state["messages"].append(message)
        _save_messages(context, state["messages"])
        render()
        scroll_bottom()

    def send_text():
        text = str(editor.getText().toString()).strip()
        if not text:
            return
        editor.setText("")
        append({"type": "text", "text": text, "ts": int(time.time() * 1000)})

    def pick_image():
        try:
            from android.content import Intent
            intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.setType("image/*")
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            context.startActivityForResult(intent, _REQUEST_PICK_IMAGE)
        except Exception as error:
            _log("pick image", error)

    back.setOnClickListener(_click_class()(lambda: context.finish()))
    send.setOnClickListener(_click_class()(send_text))
    pick.setOnClickListener(_click_class()(pick_image))

    render()
    scroll_bottom()

    _ACTIVE["context"] = context
    _ACTIVE["append"] = append
    _ACTIVE["state"] = state
    _ACTIVE["render"] = render
    _ACTIVE["scroll_bottom"] = scroll_bottom
    return root


def on_activity_result(context, request_code, result_code, data):
    """Activity 回调入口：把用户选择的系统图片复制到本地并追加为图片消息。"""
    if int(request_code) != _REQUEST_PICK_IMAGE:
        return
    if data is None or int(result_code) != -1:
        return
    try:
        from android.graphics import Bitmap, BitmapFactory
        from java.io import FileOutputStream

        uri = data.getData()
        if uri is None:
            return
        stream = context.getContentResolver().openInputStream(uri)
        bitmap = BitmapFactory.decodeStream(stream)
        stream.close()
        if bitmap is None:
            return
        _, images_dir = _data_dir(context)
        path = os.path.join(images_dir, "img_%d.jpg" % int(time.time() * 1000))
        output = FileOutputStream(path)
        bitmap.compress(Bitmap.CompressFormat.JPEG, 88, output)
        output.close()
        append = _ACTIVE.get("append")
        if append is not None:
            append({"type": "image", "path": path, "ts": int(time.time() * 1000)})
    except Exception as error:
        _log("activity result", error)


def push_message(context, sender, text):
    """RPC 入口：向本机聊天记录追加一条消息（默认来自反馈作者），不联网发送。

    页面未打开时只落盘，下次打开可见；页面打开时投递主线程追加气泡并滚到底。
    """
    try:
        text = str(text or "").strip()
        if not text:
            return json.dumps({"ok": False, "error": "empty_text"})
        sender = str(sender or "author")
        message = {
            "type": "text",
            "text": text,
            "sender": sender,
            "ts": int(time.time() * 1000),
        }
        messages = _load_messages(context)
        messages.append(message)
        _save_messages(context, messages)
        state = _ACTIVE.get("state")
        if state is not None:
            def update():
                state["messages"] = messages
                render = _ACTIVE.get("render")
                if render is not None:
                    render()
                scroller = _ACTIVE.get("scroll_bottom")
                if scroller is not None:
                    scroller()
            _post(update)
        return json.dumps(
            {"ok": True, "count": len(messages), "message": message},
            ensure_ascii=False,
        )
    except Exception as error:
        _log("push message", error)
        return json.dumps({"ok": False, "error": str(error)})


def release_page(context=None):
    """Activity 销毁时解绑活跃页面，避免 RPC 写进已销毁的 View。"""
    _ACTIVE.clear()
