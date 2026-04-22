"""
Entry point for the Facebook Group Activity Scanner.
"""

import sys

# Enable Windows high-DPI awareness so the UI is crisp on modern displays
if sys.platform == "win32":
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

from gui import App


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
