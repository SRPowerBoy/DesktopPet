"""Render the FocusBot robot to icon.png (used by PyInstaller for the .exe icon)."""
import sys

from PySide6.QtWidgets import QApplication

from focusbot import make_icon_pixmap

app = QApplication(sys.argv)
make_icon_pixmap(256).save("icon.png")
print("wrote icon.png")
