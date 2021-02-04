import logging
import struct
from typing import List, Dict, Tuple, Union

import sysv_ipc
import xcffib
import xcffib.composite
import xcffib.shm
import xcffib.xinput
import xcffib.xproto
from PySide2.QtCore import QThread, Slot, QObject

from runekit.game import GameManager
from .instance import GameInstance, X11GameInstance
from ..overlay import DesktopWideOverlay

MAX_SHM = 10
NET_ACTIVE_WINDOW = "_NET_ACTIVE_WINDOW"


class X11GameManager(GameManager):
    connection: xcffib.Connection

    _instances: Dict[int, X11GameInstance]
    _atom: Dict[bytes, int]
    _shm: List[Tuple[int, sysv_ipc.SharedMemory]]

    def __init__(self, **kwargs):
        super().__init__(*kwargs)
        self._instances = {}
        self._atom = {}
        self._shm = []

        self.connection = xcffib.Connection()
        self.screen = self.connection.get_screen_pointers()[self.connection.pref_screen]
        self.xcomposite = self.connection(xcffib.composite.key)
        self.xshm = self.connection(xcffib.shm.key)
        self.xinput = self.connection(xcffib.xinput.key)
        self._setup_composite()
        self._setup_overlay()

        self.event_thread = QThread(self)

        self.event_worker = X11EventWorker(self)
        self.event_worker.moveToThread(self.event_thread)
        self.event_thread.started.connect(self.event_worker.run)
        self.event_thread.finished.connect(self.event_worker.deleteLater)

        self.event_thread.start()

    def stop(self):
        self.event_thread.requestInterruption()
        self.event_thread.quit()
        self.event_thread.wait()

    def get_instances(self) -> List[GameInstance]:
        def visit(wid: int):
            wm_class = self.get_property(wid, xcffib.xproto.Atom.WM_CLASS)

            if wm_class:
                instance_name, app_name = wm_class.split("\00")
                if app_name == "RuneScape":
                    if wid not in self._instances:
                        instance = X11GameInstance(self, wid, parent=self)
                        self._instances[wid] = instance

            query = self.connection.core.QueryTree(wid).reply()
            for child in query.children:
                visit(child)

        visit(self.screen.root)

        return list(self._instances.values())

    def get_active_window(self) -> int:
        return self.get_property(self.screen.root, "_NET_ACTIVE_WINDOW")

    def _setup_overlay(self):
        self.overlay = DesktopWideOverlay()
        self.overlay.show()

    def _setup_composite(self):
        self.xcomposite.QueryVersion(0, 4, is_checked=True)

    def get_property(
        self,
        wid: int,
        name: str,
        type_=xcffib.xproto.GetPropertyType.Any,
        index=0,
        max_values=1000,
    ):
        reply = self.connection.core.GetProperty(
            False,
            wid,
            self.get_atom(name),
            type_,
            index,
            max_values,
        ).reply()

        if reply.type == xcffib.xproto.Atom.STRING:
            return reply.value.to_string()[:-1]
        elif reply.type in (xcffib.xproto.Atom.WINDOW, xcffib.xproto.Atom.CARDINAL):
            return struct.unpack("=I", reply.value.buf())[0]

        return reply.value

    def get_atom(self, atom: str) -> int:
        if isinstance(atom, int):
            return atom

        if hasattr(xcffib.xproto.Atom, atom):
            return getattr(xcffib.xproto.Atom, atom)

        atom = atom.encode("ascii")

        if atom in self._atom:
            return self._atom[atom]

        out = self.connection.core.InternAtom(False, len(atom), atom).reply().atom
        self._atom[atom] = out

        return out

    def get_shm(self, size: int) -> Tuple[int, sysv_ipc.SharedMemory]:
        for item in self._shm:
            if item[1].size >= size:
                self._shm.remove(item)
                return item

        shm = sysv_ipc.SharedMemory(None, flags=sysv_ipc.IPC_CREX, size=size)
        xid = self.connection.generate_id()
        self.xshm.Attach(xid, shm.id, False, is_checked=True)
        return xid, shm

    def free_shm(self, shm: Tuple[int, sysv_ipc.SharedMemory]):
        self._shm.append(shm)
        self.gc_shm()

    def gc_shm(self):
        while len(self._shm) > MAX_SHM:
            xid, shm = self._shm.pop(0)
            self.xshm.Detach(xid)
            shm.detach()
            shm.remove()


class X11EventWorker(QObject):
    def __init__(self, manager: "X11GameManager", **kwargs):
        super().__init__(**kwargs)
        self.manager = manager
        self.logger = logging.getLogger(__name__ + "." + self.__class__.__name__)

        self.handlers = {
            xcffib.xproto.PropertyNotifyEvent: self.on_property_change,
            xcffib.xproto.ConfigureNotifyEvent: self.on_configure_event,
            xcffib.xproto.KeyPressEvent: self.on_input_event,
            xcffib.xproto.ButtonPressEvent: self.on_input_event,
        }
        self.active_win_id = self.manager.get_active_window()

    @Slot()
    def run(self):
        self.manager.connection.core.ChangeWindowAttributes(
            self.manager.screen.root,
            xcffib.xproto.CW.EventMask,
            [xcffib.xproto.EventMask.PropertyChange],
            is_checked=True,
        )
        current_thread = QThread.currentThread()

        while True:
            if current_thread.isInterruptionRequested():
                return

            evt = self.manager.connection.poll_for_event()
            if evt is None:
                QThread.msleep(10)
                continue

            for wanted_type, handler in self.handlers.items():
                if isinstance(evt, wanted_type):
                    try:
                        handler(evt)
                    except:
                        self.logger.error(
                            "Error handling event %s", repr(evt), exc_info=True
                        )

    def on_property_change(self, evt: xcffib.xproto.PropertyNotifyEvent):
        if evt.atom == self.manager.get_atom(NET_ACTIVE_WINDOW):
            active_win_id = self.manager.get_active_window()

            if self.active_win_id == active_win_id:
                return

            self.active_win_id = active_win_id

            for id_, instance in self.manager._instances.items():
                active = active_win_id == id_

                if active != instance.is_focused:
                    instance.is_focused = active
                    instance.focusChanged.emit(active)

    def on_input_event(
        self, evt: Union[xcffib.xproto.KeyPressEvent, xcffib.xproto.ButtonPressEvent]
    ):
        try:
            self.manager._instances[evt.event].input_signal.emit(evt)
        except KeyError:
            self.logger.debug("Got input event for %d but is not registered", evt.event)

    def on_configure_event(self, evt: xcffib.xproto.ConfigureNotifyEvent):
        try:
            self.manager._instances[evt.window].config_signal.emit(evt)
        except KeyError:
            self.logger.debug(
                "Got configure event for %d but is not registered", evt.window
            )
