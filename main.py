import sys

if sys.platform == "win32":
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

from gui import App

if __name__ == "__main__":
    App().mainloop()
