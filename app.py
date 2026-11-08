import queue
import threading
import time
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import serial
import serial.tools.list_ports
from PIL import Image, ImageTk

DEFAULT_PORT = "COM8"
DEFAULT_BAUD = 115200


class App:
    def __init__(self, root):
        self.root = root
        root.title("Yazici Kontrol + Kamera")
        root.geometry("1100x700")

        self.ser = None
        self.reader_thread = None
        self.ok_event = threading.Event()
        self.send_lock = threading.Lock()
        self.log_q = queue.Queue()
        self.running = False

        self.printing = False
        self.paused = False
        self.stop_flag = False
        self.gcode_lines = []
        self.progress_var = tk.DoubleVar(value=0)

        self.cap = None
        self.cam_thread = None
        self.cam_running = False
        self.latest_frame = None
        self.frame_lock = threading.Lock()
        self.photo = None

        self.build_ui()
        self.root.after(100, self.pump_log)
        self.root.after(40, self.update_video)
        self.root.after(4000, self.poll_temp)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- arayuz ----------
    def build_ui(self):
        left = ttk.Frame(self.root)
        left.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        right = ttk.Frame(self.root)
        right.pack(side="right", fill="both", padx=6, pady=6)

        # kamera
        self.video_label = tk.Label(left, bg="black", text="Kamera kapali",
                                    fg="white")
        self.video_label.pack(fill="both", expand=True)

        cam = ttk.LabelFrame(left, text="Kamera")
        cam.pack(fill="x", pady=4)
        ttk.Label(cam, text="Kamera no:").pack(side="left", padx=4)
        self.cam_idx = tk.StringVar(value="0")
        ttk.Spinbox(cam, from_=0, to=9, width=4,
                    textvariable=self.cam_idx).pack(side="left")
        self.cam_btn = ttk.Button(cam, text="Kamerayi Ac",
                                  command=self.toggle_camera)
        self.cam_btn.pack(side="left", padx=6)
        ttk.Button(cam, text="Fotograf Cek",
                   command=self.snapshot).pack(side="left")

        # baglanti
        conn = ttk.LabelFrame(right, text="Baglanti")
        conn.pack(fill="x", pady=4)
        ttk.Label(conn, text="Port:").grid(row=0, column=0, padx=4, pady=2)
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if DEFAULT_PORT not in ports:
            ports.insert(0, DEFAULT_PORT)
        ttk.Combobox(conn, textvariable=self.port_var, values=ports,
                     width=10).grid(row=0, column=1)
        ttk.Label(conn, text="Baud:").grid(row=1, column=0, padx=4, pady=2)
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        ttk.Combobox(conn, textvariable=self.baud_var,
                     values=["115200", "250000", "57600", "9600"],
                     width=10).grid(row=1, column=1)
        self.conn_btn = ttk.Button(conn, text="Baglan",
                                   command=self.toggle_connect)
        self.conn_btn.grid(row=0, column=2, rowspan=2, padx=8)

        self.temp_label = ttk.Label(right, text="Sicaklik: -")
        self.temp_label.pack(anchor="w", pady=2)

        # baski
        pr = ttk.LabelFrame(right, text="Baski")
        pr.pack(fill="x", pady=4)
        self.file_label = ttk.Label(pr, text="Dosya secilmedi")
        self.file_label.pack(anchor="w", padx=4)
        row = ttk.Frame(pr)
        row.pack(fill="x", pady=2)
        ttk.Button(row, text="G-code Sec", command=self.pick_file).pack(
            side="left", padx=2)
        self.start_btn = ttk.Button(row, text="Baslat",
                                    command=self.start_print)
        self.start_btn.pack(side="left", padx=2)
        self.pause_btn = ttk.Button(row, text="Duraklat",
                                    command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=2)
        ttk.Button(row, text="Durdur", command=self.stop_print).pack(
            side="left", padx=2)
        ttk.Progressbar(pr, variable=self.progress_var,
                        maximum=100).pack(fill="x", padx=4, pady=4)

        # manuel
        mn = ttk.LabelFrame(right, text="Manuel")
        mn.pack(fill="x", pady=4)
        r1 = ttk.Frame(mn)
        r1.pack(fill="x")
        ttk.Button(r1, text="Home", command=lambda: self.manual("G28")).pack(
            side="left", padx=2)
        ttk.Button(r1, text="Motorlari Kapat",
                   command=lambda: self.manual("M84")).pack(side="left", padx=2)
        ttk.Button(r1, text="Isitici Kapat",
                   command=lambda: self.manual("M104 S0\nM140 S0")).pack(
            side="left", padx=2)
        r2 = ttk.Frame(mn)
        r2.pack(fill="x", pady=2)
        self.cmd_var = tk.StringVar()
        e = ttk.Entry(r2, textvariable=self.cmd_var)
        e.pack(side="left", fill="x", expand=True, padx=2)
        e.bind("<Return>", lambda ev: self.send_manual_entry())
        ttk.Button(r2, text="Gonder", command=self.send_manual_entry).pack(
            side="left", padx=2)

        # log
        self.log = tk.Text(right, height=18, width=48, state="disabled")
        self.log.pack(fill="both", expand=True, pady=4)

    # ---------- log ----------
    def logmsg(self, text):
        self.log_q.put(text)

    def pump_log(self):
        try:
            while True:
                t = self.log_q.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", t + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self.pump_log)

    # ---------- seri port ----------
    def toggle_connect(self):
        if self.ser:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        try:
            self.ser = serial.Serial(self.port_var.get(),
                                     int(self.baud_var.get()), timeout=1)
        except Exception as e:
            self.ser = None
            messagebox.showerror("Hata", f"Baglanilamadi:\n{e}")
            return
        self.running = True
        self.reader_thread = threading.Thread(target=self.reader, daemon=True)
        self.reader_thread.start()
        self.conn_btn.configure(text="Baglantiyi Kes")
        self.logmsg(f"Baglandi: {self.port_var.get()} @ {self.baud_var.get()}")
        time.sleep(2)  # kart resetlenir
        self.ok_event.set()

    def disconnect(self):
        self.stop_flag = True
        self.running = False
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.printing = False
        self.conn_btn.configure(text="Baglan")
        self.logmsg("Baglanti kesildi")

    def reader(self):
        while self.running and self.ser:
            try:
                line = self.ser.readline().decode(errors="ignore").strip()
            except Exception:
                break
            if not line:
                continue
            if line.startswith("ok") or " ok" in line[:6]:
                self.ok_event.set()
            m = re.search(r"T:\s*([\d.]+)\s*/\s*([\d.]+)", line)
            b = re.search(r"B:\s*([\d.]+)\s*/\s*([\d.]+)", line)
            if m:
                txt = f"Nozul: {m.group(1)}/{m.group(2)}"
                if b:
                    txt += f"   Tabla: {b.group(1)}/{b.group(2)}"
                self.root.after(0, lambda t=txt: self.temp_label.configure(
                    text=t))
            if not line.startswith("ok T:"):
                self.logmsg("< " + line)

    def send_cmd(self, cmd, timeout=300):
        """Tek satir gonder, ok bekle."""
        if not self.ser:
            return False
        with self.send_lock:
            self.ok_event.clear()
            try:
                self.ser.write((cmd.strip() + "\n").encode())
            except Exception as e:
                self.logmsg(f"Yazma hatasi: {e}")
                return False
            return self.ok_event.wait(timeout)

    def poll_temp(self):
        if self.ser and not self.printing:
            threading.Thread(target=self.send_cmd, args=("M105", 5),
                             daemon=True).start()
        self.root.after(4000, self.poll_temp)

    # ---------- manuel ----------
    def manual(self, cmds):
        if not self.ser:
            messagebox.showinfo("Bilgi", "Once yaziciya baglan.")
            return
        if self.printing:
            messagebox.showinfo("Bilgi", "Baski sirasinda kullanilamaz.")
            return

        def run():
            for c in cmds.split("\n"):
                self.logmsg("> " + c)
                self.send_cmd(c)
        threading.Thread(target=run, daemon=True).start()

    def send_manual_entry(self):
        c = self.cmd_var.get().strip()
        if c:
            self.cmd_var.set("")
            self.manual(c)

    # ---------- baski ----------
    def pick_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("G-code", "*.gcode *.gco *.g *.nc"),
                       ("Hepsi", "*.*")])
        if not path:
            return
        with open(path, "r", errors="ignore") as f:
            lines = []
            for raw in f:
                s = raw.split(";")[0].strip()
                if s:
                    lines.append(s)
        self.gcode_lines = lines
        self.file_label.configure(
            text=f"{path.split('/')[-1].split(chr(92))[-1]} ({len(lines)} satir)")

    def start_print(self):
        if not self.ser:
            messagebox.showinfo("Bilgi", "Once yaziciya baglan.")
            return
        if not self.gcode_lines:
            messagebox.showinfo("Bilgi", "Once G-code sec.")
            return
        if self.printing:
            return
        self.printing = True
        self.paused = False
        self.stop_flag = False
        threading.Thread(target=self.print_worker, daemon=True).start()

    def print_worker(self):
        total = len(self.gcode_lines)
        last_temp = time.time()
        for i, line in enumerate(self.gcode_lines):
            while self.paused and not self.stop_flag:
                time.sleep(0.2)
            if self.stop_flag or not self.ser:
                break
            if not self.send_cmd(line):
                self.logmsg("Yazicidan cevap gelmedi, baski durdu.")
                break
            if time.time() - last_temp > 5:
                self.send_cmd("M105", 5)
                last_temp = time.time()
            if i % 20 == 0:
                p = (i + 1) * 100.0 / total
                self.root.after(0, lambda v=p: self.progress_var.set(v))
        self.printing = False
        if self.ser and self.stop_flag:
            self.send_cmd("M104 S0", 5)
            self.send_cmd("M140 S0", 5)
            self.logmsg("Baski durduruldu.")
        elif self.ser:
            self.root.after(0, lambda: self.progress_var.set(100))
            self.logmsg("Baski bitti.")

    def toggle_pause(self):
        if not self.printing:
            return
        self.paused = not self.paused
        self.pause_btn.configure(text="Devam" if self.paused else "Duraklat")

    def stop_print(self):
        self.stop_flag = True
        self.paused = False
        self.pause_btn.configure(text="Duraklat")

    # ---------- kamera ----------
    def toggle_camera(self):
        if self.cam_running:
            self.close_camera()
        else:
            self.open_camera()

    def open_camera(self):
        try:
            idx = int(self.cam_idx.get())
        except ValueError:
            idx = 0
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            messagebox.showerror("Hata", f"Kamera {idx} acilamadi.")
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.cap = cap
        self.cam_running = True
        self.cam_btn.configure(text="Kamerayi Kapat")
        self.cam_thread = threading.Thread(target=self.cam_loop, daemon=True)
        self.cam_thread.start()

    def cam_loop(self):
        while self.cam_running and self.cap:
            ok, frame = self.cap.read()
            if ok:
                with self.frame_lock:
                    self.latest_frame = frame
            else:
                time.sleep(0.05)

    def close_camera(self):
        self.cam_running = False
        time.sleep(0.1)
        if self.cap:
            self.cap.release()
        self.cap = None
        with self.frame_lock:
            self.latest_frame = None
        self.cam_btn.configure(text="Kamerayi Ac")
        self.video_label.configure(image="", text="Kamera kapali")
        self.photo = None

    def update_video(self):
        frame = None
        with self.frame_lock:
            if self.latest_frame is not None:
                frame = self.latest_frame.copy()
        if frame is not None:
            w = max(self.video_label.winfo_width(), 320)
            h = max(self.video_label.winfo_height(), 240)
            fh, fw = frame.shape[:2]
            scale = min(w / fw, h / fh)
            frame = cv2.resize(frame, (int(fw * scale), int(fh * scale)))
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            self.photo = ImageTk.PhotoImage(Image.fromarray(rgb))
            self.video_label.configure(image=self.photo, text="")
        self.root.after(40, self.update_video)

    def snapshot(self):
        with self.frame_lock:
            frame = None if self.latest_frame is None else \
                self.latest_frame.copy()
        if frame is None:
            messagebox.showinfo("Bilgi", "Kamera acik degil.")
            return
        name = time.strftime("foto_%Y%m%d_%H%M%S.jpg")
        cv2.imwrite(name, frame)
        self.logmsg(f"Kaydedildi: {name}")

    def on_close(self):
        self.stop_flag = True
        self.cam_running = False
        self.running = False
        try:
            if self.cap:
                self.cap.release()
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    r = tk.Tk()
    App(r)
    r.mainloop()
