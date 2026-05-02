# SPDX-License-Identifier: GPL-3.0-or-later
# Clipboard Docker for Krita
# Adapted from BuliCommander (Grum999) clipboard subsystem

from krita import Krita, DockWidget, DockWidgetFactory, DockWidgetFactoryBase
from .bcclipboard import BCClipboardManager
from .bcclipboarddocker import BCClipboardDockerWidget

DOCKER_ID = 'clipboarddocker_docker'

# ---------------------------------------------------------------------------
# Singleton manager – created once when the module is imported
# ---------------------------------------------------------------------------
_manager = BCClipboardManager()


# ---------------------------------------------------------------------------
# DockWidget – Krita instantiates this class for each window
# ---------------------------------------------------------------------------
class ClipboardDockerDock(DockWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Clipboard')
        self._widget = BCClipboardDockerWidget(_manager)
        self.setWidget(self._widget)
        _manager.start()

    def canvasChanged(self, canvas):
        pass


# ---------------------------------------------------------------------------
# Top-level registration – runs when Krita imports this package
# ---------------------------------------------------------------------------
instance = Krita.instance()
dock_widget_factory = DockWidgetFactory(
    DOCKER_ID,
    DockWidgetFactoryBase.DockRight,
    ClipboardDockerDock
)
instance.addDockWidgetFactory(dock_widget_factory)
