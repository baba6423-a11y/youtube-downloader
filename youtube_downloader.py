#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تطبيق تحميل فيديوهات يوتيوب
يستخدم yt-dlp للتحميل و customtkinter للواجهة
"""

import os
import sys
import json
import queue
import platform
import threading
import subprocess
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox
import yt_dlp

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# محاولة استيراد مكتبة الإشعارات المناسبة لنظام التشغيل الحالي
HAS_NOTIFIER = False
if IS_WINDOWS:
    try:
        from win10toast import ToastNotifier
        _toast_notifier = ToastNotifier()
        HAS_NOTIFIER = True
    except Exception:
        HAS_NOTIFIER = False
elif IS_MAC:
    try:
        from pync import Notifier
        HAS_NOTIFIER = True
    except Exception:
        HAS_NOTIFIER = False


# ============================================================
# الإعدادات العامة للواجهة
# ============================================================
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

DEFAULT_DOWNLOAD_DIR = str(Path.home() / "Downloads")


def send_system_notification(title, message):
    """إرسال إشعار نظام التشغيل عند اكتمال التحميل (يدعم ويندوز وماك)"""
    try:
        if IS_WINDOWS and HAS_NOTIFIER:
            # threaded=True حتى لا يوقف الإشعار باقي التطبيق أثناء انتظاره
            _toast_notifier.show_toast(title, message, duration=5, threaded=True)
        elif IS_MAC and HAS_NOTIFIER:
            Notifier.notify(message, title=title)
        elif IS_MAC:
            # طريقة بديلة عبر osascript إذا فشلت pync
            script = f'display notification "{message}" with title "{title}"'
            subprocess.run(["osascript", "-e", script], check=False)
    except Exception:
        pass


def format_bytes(num_bytes):
    """تحويل حجم البايتات إلى صيغة مقروءة (مثال: 12.5 MB)"""
    if num_bytes is None:
        return "—"
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def format_eta(eta_seconds):
    """تحويل الوقت المتبقي (بالثواني) إلى صيغة دقيقة:ثانية مقروءة، مع تجاهل القيم غير المنطقية"""
    if eta_seconds is None:
        return "—"
    try:
        eta_seconds = int(eta_seconds)
    except (TypeError, ValueError):
        return "—"

    # تجاهل القيم السالبة أو الكبيرة جداً (تقدير خاطئ من yt-dlp)
    if eta_seconds < 0 or eta_seconds > 24 * 60 * 60:  # أكثر من 24 ساعة = تقدير غير موثوق
        return "جاري الحساب..."

    if eta_seconds < 60:
        return f"{eta_seconds} ثانية"

    minutes, seconds = divmod(eta_seconds, 60)
    if minutes < 60:
        return f"{minutes}:{seconds:02d} دقيقة"

    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d} ساعة"


def is_playlist_url(url):
    """فحص سريع إذا كان الرابط يحتوي مؤشر بليلست (list=) بدون تحميل أي بيانات"""
    return "list=" in url


def extract_playlist_video_urls(playlist_url, max_count=None):
    """
    يرجع قائمة روابط الفيديوهات المنفردة داخل بليلست معين.
    max_count: إذا حُدد، يوقف الاستخراج بعد هذا العدد من الفيديوهات (None = الكل).
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,  # يجلب فقط قائمة الروابط بدون تفاصيل كل فيديو (أسرع بكثير)
        "skip_download": True,
    }
    if max_count:
        ydl_opts["playlistend"] = max_count

    video_urls = []
    playlist_title = playlist_url
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(playlist_url, download=False)
            playlist_title = info.get("title", playlist_url)
            entries = info.get("entries", [])
            for entry in entries:
                if not entry:
                    continue
                video_id = entry.get("id")
                if video_id:
                    video_urls.append(f"https://www.youtube.com/watch?v={video_id}")
                elif entry.get("url"):
                    video_urls.append(entry["url"])
    except Exception as e:
        raise RuntimeError(f"فشل جلب قائمة التشغيل: {e}")

    return playlist_title, video_urls


def extract_video_formats(url):
    """
    جلب قائمة الجودات المتاحة لفيديو معين من يوتيوب.
    يرجع قائمة بصيغة [{'label': '1080p', 'format_id': '137+140'}, ...]
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    results = []
    title = url
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", url)

            seen_heights = set()
            formats = info.get("formats", [])

            # نرتب من الأعلى جودة للأقل، ونفضّل ترميز H.264 (avc1) عند توفره
            # بنفس الجودة (height) لأنه متوافق بشكل كامل مع QuickTime وكل برامج
            # ماك، بعكس VP9/AV1 اللي قد لا تفتح بشكل صحيح بدون تحويل إضافي.
            video_formats = [
                f for f in formats
                if f.get("vcodec") != "none" and f.get("height")
            ]

            def sort_key(f):
                height = f.get("height") or 0
                vcodec = (f.get("vcodec") or "").lower()
                is_h264 = vcodec.startswith("avc1") or vcodec.startswith("h264")
                # نرتب أولاً حسب الجودة تنازلياً، وعند تساوي الجودة نفضّل H.264
                return (height, 1 if is_h264 else 0)

            video_formats.sort(key=sort_key, reverse=True)

            for f in video_formats:
                height = f.get("height")
                if height in seen_heights:
                    continue
                seen_heights.add(height)
                label = f"{height}p"
                if f.get("fps") and f.get("fps") > 30:
                    label += f" {int(f['fps'])}fps"
                vcodec = (f.get("vcodec") or "").lower()
                is_h264 = vcodec.startswith("avc1") or vcodec.startswith("h264")
                if not is_h264:
                    label += " *"  # علامة تنبيه بسيطة: قد تحتاج تحويل إضافي للتوافق
                results.append({
                    "label": label,
                    "format_id": f.get("format_id"),
                    "height": height,
                    "is_h264": is_h264,
                })

            if not results:
                # ما حصلنا جودات منفصلة، نستخدم "أفضل جودة" فقط
                results.append({"label": "أفضل جودة متاحة", "format_id": "best", "height": 0})

    except Exception as e:
        raise RuntimeError(f"فشل جلب معلومات الفيديو: {e}")

    return title, results


# ============================================================
# حالة كل تحميل (Pause / Cancel) عبر threading.Event
# ============================================================
class DownloadTask:
    """يمثل مهمة تحميل واحدة لرابط واحد، مع إمكانية الإيقاف المؤقت والإلغاء"""

    def __init__(self, url, mode, quality_format_id, download_thumbnail,
                 save_dir, on_progress, on_status, on_finished):
        self.url = url
        self.mode = mode  # "video" or "audio"
        self.quality_format_id = quality_format_id
        self.download_thumbnail = download_thumbnail
        self.save_dir = save_dir

        self.on_progress = on_progress      # callback(percent, speed_text, eta_text)
        self.on_status = on_status          # callback(status_text)
        self.on_finished = on_finished      # callback(success: bool, message: str)

        self.pause_event = threading.Event()
        self.pause_event.set()  # set = غير موقوف (شغال)
        self.cancel_event = threading.Event()
        self.thread = None
        self._final_filename = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def pause(self):
        self.pause_event.clear()
        self.on_status("متوقف مؤقتاً ⏸")

    def resume(self):
        self.pause_event.set()
        self.on_status("جاري التحميل...")

    def is_paused(self):
        return not self.pause_event.is_set()

    def cancel(self):
        self.cancel_event.set()
        self.pause_event.set()  # حتى لو كان متوقف، نخليه يكمل عشان يوصل لفحص الإلغاء
        self.on_status("جاري الإلغاء...")

    def _progress_hook(self, d):
        # فحص الإلغاء
        if self.cancel_event.is_set():
            raise yt_dlp.utils.DownloadError("CANCELLED_BY_USER")

        # فحص الإيقاف المؤقت (ننتظر هنا لين يرجع المستخدم يكمل أو يلغي)
        while not self.pause_event.is_set():
            if self.cancel_event.is_set():
                raise yt_dlp.utils.DownloadError("CANCELLED_BY_USER")
            threading.Event().wait(0.2)

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            percent = 0
            if total:
                percent = downloaded / total * 100
            speed = d.get("speed")
            speed_text = f"{format_bytes(speed)}/s" if speed else "—"
            eta_text = format_eta(d.get("eta"))
            self.on_progress(percent, speed_text, eta_text)
            self.on_status("جاري التحميل...")

        elif d["status"] == "finished":
            self.on_progress(100, "—", "—")
            self.on_status("جاري المعالجة...")

    def _build_ydl_opts(self):
        outtmpl = os.path.join(self.save_dir, "%(title)s.%(ext)s")

        opts = {
            "outtmpl": outtmpl,
            "progress_hooks": [self._progress_hook],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "writethumbnail": self.download_thumbnail,
            # تحميل عدة أجزاء (fragments) بنفس الوقت بدل التسلسل، يسرّع التحميل بشكل ملحوظ
            "concurrent_fragment_downloads": 5,
            # حذف ملفات الأجزاء المؤقتة (.part-FragXXX) تلقائياً فور اكتمال الدمج
            "keep_fragments": False,
            # حذف الملفات الأصلية (فيديو/صوت منفصلين) بعد دمجهما بنجاح
            "keepvideo": False,
        }

        if self.mode == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            fmt_id = self.quality_format_id
            # نفضّل صوت بترميز AAC/m4a لأنه متوافق بشكل كامل مع QuickTime Player
            # وكل برامج ماك، بعكس Opus اللي يوتيوب كثيراً ما يستخدمه كصوت افتراضي
            # (الملف يكتمل وينزل بنجاح لكن يفتح بدون صورة أو بدون صوت في QuickTime).
            if fmt_id and fmt_id != "best":
                opts["format"] = (
                    f"{fmt_id}+bestaudio[ext=m4a]/"  # الجودة المختارة + صوت متوافق
                    f"{fmt_id}+bestaudio/"            # احتياط: أي صوت إذا ما توفر m4a
                    f"best"
                )
            else:
                opts["format"] = (
                    "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/"  # H.264 + AAC (الأفضل توافقاً)
                    "bestvideo+bestaudio/"
                    "best"
                )
            opts["merge_output_format"] = "mp4"

        if self.download_thumbnail:
            # تضمين الصورة المصغرة كملف منفصل (وليس مدمجة داخل الفيديو)
            opts["postprocessors"] = opts.get("postprocessors", []) + [
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg"}
            ]

        return opts

    def _cleanup_leftover_fragments(self):
        """
        حذف أي ملفات مؤقتة متبقية (مثل xxx.part أو xxx.part-FragNNN) بنفس مجلد الحفظ.
        طبقة حماية إضافية في حال لم ينظفها yt-dlp تلقائياً (مثلاً بسبب انقطاع مؤقت بالشبكة).
        لا تحذف إلا الملفات التي تخص هذا العنوان (self.url) لتفادي حذف ملفات تحميلات أخرى.
        """
        try:
            base_name = None
            if self._final_filename:
                # اسم الملف النهائي بدون الامتداد، نستخدمه كأساس للمطابقة
                base_name = os.path.splitext(os.path.basename(self._final_filename))[0]

            if not base_name:
                return

            for fname in os.listdir(self.save_dir):
                if not fname.startswith(base_name):
                    continue
                if ".part" in fname:  # يطابق xxx.part و xxx.part-Frag12 وغيرها
                    full_path = os.path.join(self.save_dir, fname)
                    try:
                        os.remove(full_path)
                    except Exception:
                        pass
        except Exception:
            pass

    def _ensure_quicktime_compatible(self):
        """
        تفحص ترميز الفيديو (codec) للملف النهائي، وإذا لم يكن H.264 (المتوافق
        بشكل كامل مع QuickTime Player وكل برامج ماك)، تحوّله تلقائياً.
        إذا كان الملف أصلاً H.264، تتجاوز الخطوة فوراً بدون أي وقت إضافي.
        لا تُطبَّق إلا على ملفات الفيديو (mode == "video")، وتُتجاهل بصمت عند أي خطأ
        حتى لا تفشل عملية تحميل كاملة بسبب فحص توافقية إضافي.

        ملاحظة: هذه الخطوة خاصة بماك فقط (QuickTime Player حساس لترميز VP9/AV1).
        على ويندوز، برامج التشغيل الشائعة (VLC, Windows Media Player الحديث)
        تدعم VP9 بشكل جيد، فلا حاجة لهذا التحويل الإضافي البطيء.

        ملاحظة إضافية: بما أن قائمة الجودات أصبحت تُفضّل H.264 من البداية عند
        الاختيار، هذه الخطوة تصير نادرة الحدوث حتى على ماك (فقط لو الجودة
        المختارة لم تتوفر أصلاً بترميز H.264).
        """
        if not IS_MAC:
            return
        if self.mode != "video" or not self._final_filename:
            return
        if not os.path.exists(self._final_filename):
            return
        if self.cancel_event.is_set():
            return

        try:
            # نفحص ترميز أول مسار فيديو بالملف عبر ffprobe (يأتي مع ffmpeg)
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    self._final_filename,
                ],
                capture_output=True, text=True, timeout=30,
            )
            codec = probe.stdout.strip().lower()

            if codec in ("h264", "avc1", ""):
                return  # متوافق أصلاً (أو تعذر تحديد الترميز)، لا حاجة للتحويل

            # نحسب مدة الفيديو حتى نقدر نعرض تقدّم تقريبي للتحويل بدل رسالة ثابتة
            duration_probe = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    self._final_filename,
                ],
                capture_output=True, text=True, timeout=30,
            )
            try:
                total_duration = float(duration_probe.stdout.strip())
            except (ValueError, TypeError):
                total_duration = None

            base, ext = os.path.splitext(self._final_filename)
            temp_output = f"{base}.h264_tmp{ext}"

            # preset "veryfast" بدل "fast" لتقليل وقت التحويل بشكل ملحوظ (الفرق
            # بجودة الصورة النهائية غير محسوس تقريباً للاستخدام العادي)
            process = subprocess.Popen(
                [
                    "ffmpeg", "-y", "-i", self._final_filename,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                    "-c:a", "aac", "-b:a", "192k",
                    "-progress", "pipe:1", "-nostats",
                    temp_output,
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )

            for line in process.stdout:
                if self.cancel_event.is_set():
                    process.terminate()
                    break
                line = line.strip()
                if line.startswith("out_time_ms=") and total_duration:
                    try:
                        done_seconds = int(line.split("=")[1]) / 1_000_000
                        percent = min(99, (done_seconds / total_duration) * 100)
                        self.on_status(f"جاري ضمان التوافقية مع QuickTime... ({int(percent)}%)")
                        self.on_progress(percent, "—", "—")
                    except (ValueError, ZeroDivisionError):
                        pass

            process.wait(timeout=60 * 30)  # حد أقصى 30 دقيقة كحماية أخيرة

            if process.returncode == 0 and os.path.exists(temp_output):
                os.replace(temp_output, self._final_filename)
            else:
                if os.path.exists(temp_output):
                    os.remove(temp_output)
        except Exception:
            # أي خطأ بالفحص أو التحويل لا يجب أن يفشل التحميل بأكمله
            pass

    def _run(self):
        self.on_status("جاري التحضير...")
        try:
            ydl_opts = self._build_ydl_opts()
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
                self._final_filename = ydl.prepare_filename(info)

            self._cleanup_leftover_fragments()
            self._ensure_quicktime_compatible()

            self.on_progress(100, "—", "—")
            self.on_status("اكتمل ✅")
            title = self._final_filename or self.url
            self.on_finished(True, os.path.basename(title))

        except yt_dlp.utils.DownloadError as e:
            if "CANCELLED_BY_USER" in str(e):
                self.on_status("ملغى ❌")
                self.on_finished(False, "تم الإلغاء من قبل المستخدم")
            else:
                self.on_status("فشل ⚠️")
                self.on_finished(False, str(e))
        except Exception as e:
            self.on_status("فشل ⚠️")
            self.on_finished(False, str(e))


# ============================================================
# صف واجهة لكل رابط تحميل (شريط تقدم + حالة + أزرار تحكم)
# ============================================================
class DownloadRow(ctk.CTkFrame):
    def __init__(self, master, url, app_settings, on_remove):
        super().__init__(master, fg_color=("gray90", "gray17"), corner_radius=10)
        self.url = url
        self.app_settings = app_settings
        self.on_remove = on_remove
        self.task = None
        self.finished = False

        self.grid_columnconfigure(0, weight=1)

        # سطر العنوان (الرابط/اسم الفيديو) + الحالة
        self.title_label = ctk.CTkLabel(
            self, text=url, anchor="e", justify="right",
            font=ctk.CTkFont(size=13, weight="bold"), wraplength=420
        )
        self.title_label.grid(row=0, column=0, columnspan=4, sticky="ew", padx=12, pady=(10, 2))

        self.status_label = ctk.CTkLabel(
            self, text="بالانتظار...", anchor="e", text_color=("gray40", "gray70")
        )
        self.status_label.grid(row=1, column=0, columnspan=4, sticky="ew", padx=12)

        # شريط التقدم
        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=2, column=0, columnspan=4, sticky="ew", padx=12, pady=(6, 2))

        # سطر التفاصيل (السرعة / الوقت المتبقي)
        self.details_label = ctk.CTkLabel(
            self, text="", anchor="e", text_color=("gray40", "gray70"), font=ctk.CTkFont(size=11)
        )
        self.details_label.grid(row=3, column=0, columnspan=4, sticky="ew", padx=12, pady=(0, 8))

        # أزرار التحكم
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=4, column=0, columnspan=4, sticky="ew", padx=12, pady=(0, 10))

        self.start_btn = ctk.CTkButton(btn_frame, text="تحميل ▶", width=90, command=self.start_download)
        self.start_btn.pack(side="right", padx=4)

        self.pause_btn = ctk.CTkButton(btn_frame, text="إيقاف مؤقت ⏸", width=110,
                                        command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="right", padx=4)

        self.cancel_btn = ctk.CTkButton(btn_frame, text="إلغاء ✕", width=80, fg_color="#b3261e",
                                         hover_color="#8c1d17", command=self.cancel_download,
                                         state="disabled")
        self.cancel_btn.pack(side="right", padx=4)

        self.remove_btn = ctk.CTkButton(btn_frame, text="حذف 🗑", width=70, fg_color="gray40",
                                         hover_color="gray30", command=self.remove_row)
        self.remove_btn.pack(side="left", padx=4)

    def start_download(self):
        settings = self.app_settings()
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="إيقاف مؤقت ⏸")
        self.cancel_btn.configure(state="normal")
        self.remove_btn.configure(state="disabled")

        self.task = DownloadTask(
            url=self.url,
            mode=settings["mode"],
            quality_format_id=settings["quality_format_id"],
            download_thumbnail=settings["download_thumbnail"],
            save_dir=settings["save_dir"],
            on_progress=self._on_progress,
            on_status=self._on_status,
            on_finished=self._on_finished,
        )
        self.task.start()

    def toggle_pause(self):
        if not self.task:
            return
        if self.task.is_paused():
            self.task.resume()
            self.pause_btn.configure(text="إيقاف مؤقت ⏸")
        else:
            self.task.pause()
            self.pause_btn.configure(text="استئناف ▶")

    def cancel_download(self):
        if self.task:
            self.task.cancel()
        self.pause_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")

    def remove_row(self):
        if self.task and self.task.thread and self.task.thread.is_alive():
            messagebox.showwarning("تنبيه", "لا يمكن حذف تحميل قيد التنفيذ. ألغِ التحميل أولاً.")
            return
        self.on_remove(self)
        self.destroy()

    # --- هذه الدوال تُستدعى من خيط التحميل، لذلك نستخدم after() للتحديث الآمن للواجهة ---
    def _on_progress(self, percent, speed_text, eta_text):
        def update():
            self.progress_bar.set(min(percent / 100, 1.0))
            self.details_label.configure(text=f"السرعة: {speed_text}   |   المتبقي: {eta_text}")
        self.after(0, update)

    def _on_status(self, status_text):
        def update():
            self.status_label.configure(text=status_text)
        self.after(0, update)

    def _on_finished(self, success, message):
        def update():
            self.finished = True
            self.pause_btn.configure(state="disabled")
            self.cancel_btn.configure(state="disabled")
            self.remove_btn.configure(state="normal")
            if success:
                self.start_btn.configure(state="disabled")
                self.title_label.configure(text=message)
                send_system_notification("اكتمل التحميل ✅", message)
            else:
                # عند الفشل، نتيح إعادة المحاولة بدل تعطيل الزر نهائياً
                self.finished = False
                self.start_btn.configure(state="normal", text="إعادة المحاولة 🔄")
                self.status_label.configure(text=f"فشل: {message}")
        self.after(0, update)


# ============================================================
# النافذة الرئيسية للتطبيق
# ============================================================
class YoutubeDownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("تحميل فيديوهات يوتيوب")
        self.geometry("700x780")
        self.minsize(620, 600)

        self.save_dir = DEFAULT_DOWNLOAD_DIR
        self.rows = []  # قائمة DownloadRow
        self.detected_qualities = []  # قائمة الجودات المكتشفة لأول رابط

        self.grid_columnconfigure(0, weight=1)
        self._build_top_section()
        self._build_options_section()
        self._build_rows_section()
        self._enable_keyboard_shortcuts()

    # ---------------- القسم العلوي: إدخال الروابط ----------------
    def _build_top_section(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 6))
        frame.grid_columnconfigure(0, weight=1)

        label = ctk.CTkLabel(frame, text="ألصق روابط يوتيوب (كل رابط بسطر منفصل):",
                              anchor="e", font=ctk.CTkFont(size=14, weight="bold"))
        label.grid(row=0, column=0, sticky="ew")

        self.url_textbox = ctk.CTkTextbox(frame, height=100)
        self.url_textbox.grid(row=1, column=0, sticky="ew", pady=(6, 6))

        self.fetch_btn = ctk.CTkButton(frame, text="🔍 جلب الجودات وإضافة الروابط",
                                        command=self.on_fetch_clicked)
        self.fetch_btn.grid(row=2, column=0, sticky="ew")

    def _enable_keyboard_shortcuts(self):
        """
        تفعيل اختصارات اللصق/النسخ/القص/تحديد الكل على مستوى التطبيق بأكمله.
        على ماك المفتاح المُستخدم هو Command، وعلى ويندوز هو Control.
        نستخدم bind_all لأن البنية الداخلية لـ CTkTextbox تختلف بين الإصدارات،
        وbind_all يضمن وصول الحدث بغض النظر عن مكان التركيز (focus) طالما هو
        داخل عنصر نص (Entry أو Text).
        """
        def get_focused_text_widget():
            """يرجع العنصر الذي عليه التركيز إذا كان فعلاً صندوق كتابة (Entry/Text)"""
            widget = self.focus_get()
            if widget is None:
                return None
            cls = widget.winfo_class()
            if cls in ("Text", "TkText", "Entry", "TkEntry"):
                return widget
            return None

        def paste(event=None):
            widget = get_focused_text_widget()
            if widget is not None:
                try:
                    widget.event_generate("<<Paste>>")
                except Exception:
                    pass
                return "break"

        def copy(event=None):
            widget = get_focused_text_widget()
            if widget is not None:
                try:
                    widget.event_generate("<<Copy>>")
                except Exception:
                    pass
                return "break"

        def cut(event=None):
            widget = get_focused_text_widget()
            if widget is not None:
                try:
                    widget.event_generate("<<Cut>>")
                except Exception:
                    pass
                return "break"

        def select_all(event=None):
            widget = get_focused_text_widget()
            if widget is not None:
                try:
                    if widget.winfo_class() in ("Text", "TkText"):
                        widget.tag_add("sel", "1.0", "end")
                    else:
                        widget.select_range(0, "end")
                except Exception:
                    pass
                return "break"

        if IS_MAC:
            for seq in ("<Command-v>", "<Command-V>"):
                self.bind_all(seq, paste)
            for seq in ("<Command-c>", "<Command-C>"):
                self.bind_all(seq, copy)
            for seq in ("<Command-x>", "<Command-X>"):
                self.bind_all(seq, cut)
            for seq in ("<Command-a>", "<Command-A>"):
                self.bind_all(seq, select_all)
        else:
            # على ويندوز/لينكس، Ctrl+C/V/X/A تعمل افتراضياً بشكل سليم مع Tkinter
            # في الغالبية العظمى من الحالات، لكن نضيف ربط صريح احتياطياً.
            for seq in ("<Control-v>", "<Control-V>"):
                self.bind_all(seq, paste)
            for seq in ("<Control-c>", "<Control-C>"):
                self.bind_all(seq, copy)
            for seq in ("<Control-x>", "<Control-X>"):
                self.bind_all(seq, cut)
            for seq in ("<Control-a>", "<Control-A>"):
                self.bind_all(seq, select_all)

        # ربط إضافي عبر event.char بدل اسم المفتاح (keysym) أو الحدث القياسي.
        # السبب: لما تكون لغة لوحة المفاتيح عربي، فإن النظام قد يولّد keysym
        # عربي (مثلاً "Arabic_dal" بدل "v") فالاختصار القياسي لا يتطابق إطلاقاً.
        # لكن event.char يبقى الحرف اللاتيني الصحيح بغض النظر عن لغة الكتابة.
        char_handler_map = {"v": paste, "c": copy, "x": cut, "a": select_all}

        def on_any_keypress(event):
            if IS_MAC:
                # event.state يحتوي بت خاص بمفتاح Command على ماك (القيمة المرصودة فعلياً: 8)
                modifier_held = bool(event.state & 0x08) or bool(event.state & 0x100000)
            else:
                # على ويندوز، بت Control يكون عادة القيمة 0x4 ضمن event.state
                modifier_held = bool(event.state & 0x4)
            if not modifier_held:
                return
            handler = char_handler_map.get((event.char or "").lower())
            if handler is not None:
                return handler(event)

        self.bind_all("<KeyPress>", on_any_keypress, add="+")

    # ---------------- قسم الخيارات: نوع الملف / الجودة / الصورة المصغرة / المجلد ----------------
    def _build_options_section(self):
        frame = ctk.CTkFrame(self, corner_radius=10)
        frame.grid(row=1, column=0, sticky="ew", padx=16, pady=6)
        frame.grid_columnconfigure((0, 1), weight=1)

        # نوع الملف: فيديو أو صوت
        mode_label = ctk.CTkLabel(frame, text="نوع التحميل:", anchor="e")
        mode_label.grid(row=0, column=1, sticky="e", padx=12, pady=(12, 0))

        self.mode_var = ctk.StringVar(value="video")
        mode_seg = ctk.CTkSegmentedButton(
            frame, values=["فيديو (MP4)", "صوت فقط (MP3)"],
            command=self._on_mode_changed
        )
        mode_seg.set("فيديو (MP4)")
        mode_seg.grid(row=1, column=1, sticky="ew", padx=12, pady=(4, 12))
        self.mode_seg = mode_seg

        # قائمة الجودة
        quality_label = ctk.CTkLabel(frame, text="الجودة (للفيديو):", anchor="e")
        quality_label.grid(row=0, column=0, sticky="e", padx=12, pady=(12, 0))

        self.quality_menu = ctk.CTkOptionMenu(frame, values=["اضغط 'جلب الجودات' أولاً"])
        self.quality_menu.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 12))

        # الصورة المصغرة
        self.thumbnail_var = ctk.BooleanVar(value=False)
        thumb_check = ctk.CTkCheckBox(frame, text="تحميل الصورة المصغرة (Thumbnail)",
                                       variable=self.thumbnail_var, onvalue=True, offvalue=False)
        thumb_check.grid(row=2, column=1, sticky="e", padx=12, pady=(0, 12))

        # تحميل قائمة تشغيل كاملة (Playlist)
        self.playlist_var = ctk.BooleanVar(value=False)
        playlist_check = ctk.CTkCheckBox(
            frame, text="تحميل قائمة التشغيل كاملة (Playlist)",
            variable=self.playlist_var, onvalue=True, offvalue=False,
            command=self._on_playlist_toggle
        )
        playlist_check.grid(row=3, column=1, sticky="e", padx=12, pady=(0, 12))

        self.playlist_count_var = ctk.StringVar(value="الكل")
        self.playlist_count_menu = ctk.CTkOptionMenu(
            frame, values=["الكل", "أول 5", "أول 10", "أول 20", "أول 50"],
            variable=self.playlist_count_var, state="disabled"
        )
        self.playlist_count_menu.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))

        # مجلد الحفظ
        self.save_dir_label = ctk.CTkLabel(frame, text=f"📁 {self.save_dir}",
                                            anchor="e", text_color=("gray40", "gray70"))
        self.save_dir_label.grid(row=4, column=0, sticky="e", padx=12, pady=(0, 12))

        choose_dir_btn = ctk.CTkButton(frame, text="اختيار مجلد الحفظ", command=self.choose_save_dir)
        choose_dir_btn.grid(row=5, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))

    def _on_playlist_toggle(self):
        if self.playlist_var.get():
            self.playlist_count_menu.configure(state="normal")
        else:
            self.playlist_count_menu.configure(state="disabled")

    def _get_playlist_max_count(self):
        """يحول اختيار القائمة المنسدلة لرقم (None = بدون حد أقصى = الكل)"""
        label = self.playlist_count_var.get()
        mapping = {"الكل": None, "أول 5": 5, "أول 10": 10, "أول 20": 20, "أول 50": 50}
        return mapping.get(label)

    def _on_mode_changed(self, value):
        if value == "صوت فقط (MP3)":
            self.quality_menu.configure(state="disabled")
        else:
            self.quality_menu.configure(state="normal")

    def choose_save_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.save_dir)
        if chosen:
            self.save_dir = chosen
            self.save_dir_label.configure(text=f"📁 {self.save_dir}")

    # ---------------- قسم قائمة التحميلات (Scrollable) ----------------
    def _build_rows_section(self):
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.grid(row=2, column=0, sticky="nsew", padx=16, pady=(6, 16))
        self.grid_rowconfigure(2, weight=1)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(1, weight=1)

        header = ctk.CTkLabel(container, text="قائمة التحميلات:", anchor="e",
                               font=ctk.CTkFont(size=14, weight="bold"))
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.rows_scroll = ctk.CTkScrollableFrame(container, fg_color="transparent")
        self.rows_scroll.grid(row=1, column=0, sticky="nsew")
        self.rows_scroll.grid_columnconfigure(0, weight=1)
        self._bind_mousewheel(self.rows_scroll)

        # زر تحميل الكل
        download_all_btn = ctk.CTkButton(container, text="⬇ تحميل كل القائمة",
                                          fg_color="#1f6f3f", hover_color="#175430",
                                          command=self.download_all)
        download_all_btn.grid(row=2, column=0, sticky="ew", pady=(10, 0))

    def _bind_mousewheel(self, scrollable_frame):
        """
        إصلاح مشكلة عدم استجابة عجلة الماوس داخل CTkScrollableFrame.
        نربط الحدث على مستوى التطبيق بأكمله (bind_all)، ونحدد داخل المعالج
        إذا كان مؤشر الماوس فعلياً فوق منطقة قائمة التحميلات قبل التمرير،
        هذا أضمن من محاولة الوصول لأسماء خاصيات داخلية قد تتغير بين الإصدارات.
        """
        self._scrollable_frame_for_wheel = scrollable_frame

        def on_mousewheel(event):
            target = self._scrollable_frame_for_wheel
            # نتأكد إن المؤشر فوق منطقة القائمة فعلاً قبل التمرير
            try:
                x, y = event.x_root, event.y_root
                widget_under_mouse = target.winfo_containing(x, y)
            except Exception:
                widget_under_mouse = None

            if widget_under_mouse is None:
                return

            # نتحقق إن العنصر تحت الماوس هو القائمة نفسها أو أحد عناصرها الفرعية
            w = widget_under_mouse
            is_inside = False
            while w is not None:
                if w == target:
                    is_inside = True
                    break
                w = w.master if hasattr(w, "master") else None

            if not is_inside:
                return

            canvas = getattr(target, "_parent_canvas", None)
            if canvas is None:
                return

            # على ماك event.delta قيمة صغيرة (1 أو -1) لكل "نقرة"، بينما على
            # ويندوز تكون القيمة مضاعفات 120 (مثلاً 120 أو -120)، فنطبّع القيمة
            delta = event.delta
            if delta == 0:
                return
            if IS_WINDOWS:
                scroll_units = -1 * int(delta / 120)
            else:
                scroll_units = -1 * int(delta)
            canvas.yview_scroll(scroll_units, "units")

        # نربط مرة واحدة فقط على مستوى التطبيق
        if not getattr(self, "_mousewheel_bound", False):
            self.bind_all("<MouseWheel>", on_mousewheel)
            self._mousewheel_bound = True

    # ---------------- منطق جلب الجودات وإضافة الروابط ----------------
    def on_fetch_clicked(self):
        text = self.url_textbox.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("تنبيه", "الرجاء لصق رابط يوتيوب واحد على الأقل.")
            return

        urls = [u.strip() for u in text.splitlines() if u.strip()]
        self.fetch_btn.configure(state="disabled", text="جاري الجلب...")

        threading.Thread(target=self._fetch_and_add_rows, args=(urls,), daemon=True).start()

    def _fetch_and_add_rows(self, urls):
        first_url_qualities_set = False
        errors = []
        expand_playlists = self.playlist_var.get()
        max_count = self._get_playlist_max_count()

        # إذا فُعّل خيار البليلست، نوسّع أي رابط فيه list= لروابط الفيديوهات المنفردة بداخله
        final_urls = []
        if expand_playlists:
            for url in urls:
                if is_playlist_url(url):
                    self.after(0, lambda u=url: self.fetch_btn.configure(
                        text=f"جاري جلب قائمة التشغيل..."))
                    try:
                        _, playlist_urls = extract_playlist_video_urls(url, max_count)
                        if playlist_urls:
                            final_urls.extend(playlist_urls)
                        else:
                            errors.append(f"{url}\n→ لم يتم العثور على فيديوهات بقائمة التشغيل")
                    except Exception as e:
                        errors.append(f"{url}\n→ {e}")
                else:
                    final_urls.append(url)
        else:
            final_urls = urls

        for url in final_urls:
            try:
                self.after(0, lambda: self.fetch_btn.configure(text="جاري الجلب..."))
                title, qualities = extract_video_formats(url)
                if not first_url_qualities_set:
                    self.detected_qualities = qualities
                    self.after(0, self._update_quality_menu)
                    first_url_qualities_set = True

                self.after(0, lambda u=url, t=title: self._add_row(u, t))
            except Exception as e:
                errors.append(f"{url}\n→ {e}")

        def finish():
            self.fetch_btn.configure(state="normal", text="🔍 جلب الجودات وإضافة الروابط")
            self.url_textbox.delete("1.0", "end")
            if errors:
                messagebox.showerror("تعذر جلب بعض الروابط", "\n\n".join(errors))

        self.after(0, finish)

    def _update_quality_menu(self):
        labels = [q["label"] for q in self.detected_qualities]
        if not labels:
            labels = ["أفضل جودة متاحة"]
        self.quality_menu.configure(values=labels)
        self.quality_menu.set(labels[0])

    def _add_row(self, url, title):
        row = DownloadRow(self.rows_scroll, url, self._get_settings, self._remove_row)
        row.title_label.configure(text=title)
        row.grid(row=len(self.rows), column=0, sticky="ew", pady=6)
        self.rows.append(row)

    def _remove_row(self, row):
        if row in self.rows:
            self.rows.remove(row)

    def _get_settings(self):
        mode = "video" if self.mode_seg.get() == "فيديو (MP4)" else "audio"

        quality_format_id = "best"
        if mode == "video" and self.detected_qualities:
            selected_label = self.quality_menu.get()
            for q in self.detected_qualities:
                if q["label"] == selected_label:
                    quality_format_id = q["format_id"]
                    break

        return {
            "mode": mode,
            "quality_format_id": quality_format_id,
            "download_thumbnail": self.thumbnail_var.get(),
            "save_dir": self.save_dir,
        }

    def download_all(self):
        if not self.rows:
            messagebox.showinfo("تنبيه", "لا توجد روابط في القائمة بعد. اضغط 'جلب الجودات وإضافة الروابط' أولاً.")
            return
        for row in self.rows:
            if not row.finished and row.start_btn.cget("state") == "normal":
                row.start_download()


# ============================================================
# نقطة بداية تشغيل التطبيق
# ============================================================
if __name__ == "__main__":
    app = YoutubeDownloaderApp()
    app.mainloop()
