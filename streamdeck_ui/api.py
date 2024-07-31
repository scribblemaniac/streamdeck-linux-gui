"""Defines the Python API for interacting with the StreamDeck Configuration UI"""
import os
import threading
from copy import deepcopy
from functools import partial
from typing import Dict, List, Optional, Tuple, Union

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, Signal, Slot, SIGNAL, SLOT, QDataStream
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtDBus import QDBusInterface, QDBusConnection, QDBusMessage, QDBusArgument
from StreamDeck.Devices import StreamDeck
from StreamDeck.Transport.Transport import TransportError

from streamdeck_ui.config import (
    DEFAULT_BACKGROUND_COLOR,
    DEFAULT_FONT,
    DEFAULT_FONT_COLOR,
    DEFAULT_FONT_SIZE,
    FONTS_PATH,
    STATE_FILE,
    read_state_from_config,
    write_state_to_config,
)
from streamdeck_ui.dimmer import Dimmer
from streamdeck_ui.display.background_color_filter import BackgroundColorFilter
from streamdeck_ui.display.display_grid import DisplayGrid
from streamdeck_ui.display.filter import Filter
from streamdeck_ui.display.image_filter import ImageFilter
from streamdeck_ui.display.text_filter import TextFilter
from streamdeck_ui.logger import logger
from streamdeck_ui.model import ButtonMultiState, ButtonState, DeckState
from streamdeck_ui.stream_deck_monitor import StreamDeckMonitor


class KeySignalEmitter(QObject):
    key_pressed = Signal(str, int, bool)


class StreamDeckSignalEmitter(QObject):
    attached = Signal(dict)
    "A signal that is raised whenever a new StreamDeck is attached."
    detached = Signal(str)
    "A signal that is raised whenever a StreamDeck is detached. "
    cpu_changed = Signal(str, int)

class SystemLockSignalEmitter(QObject):
    @Slot(QDBusMessage)
    def _receive_dbus_signal(self, message: QDBusMessage) -> None:
        if message.interface() == "org.freedesktop.DBus.Properties" and message.member() == "PropertiesChanged":
            print("Args", message.arguments())
            arguments = message.arguments()
            if len(arguments) >= 2:
                argument = arguments[1]
                if argument.currentType() == QDBusArgument.ElementType.MapType:
                    argument.beginArray()
                    argument.beginMap()
                    argument.beginMapEntry()
                    key = argument.asVariant()
                    argument.beginArray()
                    val = argument.asVariant()
                    argument.endArray()
                    argument.endMapEntry()
                    argument.endMap()
                    argument.endArray()
                if key == "LockedHint" and type(val) == bool:
                    self.lock_changed.emit(val)

    lock_changed = Signal(bool)
    "A signal that is raised whenever the system lock state changes."

class StreamDeckServer:
    """A StreamDeckServer represents the core server logic for interacting and
    managing multiple Stream Decks.
    """

    decks_by_serial: Dict[str, StreamDeck.StreamDeck] = {}
    "Lookup with serial number -> StreamDeck"

    decks_map_id_to_serial: Dict[str, str] = {}
    "Lookup with device.id -> serial number"

    state: Dict[str, DeckState] = {}
    "The data structure holding configuration for all Stream Decks by serial number"

    key_event_lock: threading.Lock
    "Lock to serialize key press events"

    lock: threading.Lock = threading.Lock()
    "Lock to coordinate polling, updates etc to Stream Decks"

    display_handlers: Dict[str, DisplayGrid] = {}
    "Lookup with serial number for each Stream Deck display handler"

    dimmers: Dict[str, Dimmer] = {}
    "Lookup with serial number for each Stream Deck dimmer"

    monitor: Optional[StreamDeckMonitor] = None
    "Monitors for Stream Deck(s) attached to the computer"

    plugevents = StreamDeckSignalEmitter()
    "Use the connect method on the attached and detached methods to subscribe"

    streamdeck_keys = KeySignalEmitter()
    "Use the connect method on the key_pressed signal to subscribe"

    system_lock = SystemLockSignalEmitter()
    system_locked: bool = False
    "Tracks if the system is currently locked (e.g. screen saver) or not"

    def __init__(self) -> None:
        self.decks_by_serial: Dict[str, StreamDeck.StreamDeck] = {}

        # REVIEW: Should we use the same lock as the display? What exactly
        # are we protecting? The UI is signaled via message passing.
        self.key_event_lock = threading.Lock()
        self.display_handlers: Dict[str, DisplayGrid] = {}

        self.lock: threading.Lock = threading.Lock()
        self.dimmers: Dict[str, Dimmer] = {}

        # REVIEW: Should we just create one signal emitter for
        # plug events and key signals?
        self.streamdeck_keys = KeySignalEmitter()
        self.plugevents = StreamDeckSignalEmitter()
        self.system_lock = SystemLockSignalEmitter()

        self.dbus_interface = QDBusInterface("org.freedesktop.login1", "/org/freedesktop/login1/session/_34", "org.freedesktop.DBus.Properties", QDBusConnection.systemBus())
        self.system_lock.lock_changed.connect(self._set_system_lock_state)
        #self.streamdeck_keys.connect(SIGNAL("key_pressed(QString, int, bool)"), self.system_lock, SLOT("_receive_dbus_signal()"))
        self.dbus_interface.connect(SIGNAL("PropertiesChanged(QDBusMessage)"), self.system_lock, SLOT("_receive_dbus_signal(QDBusMessage)"))
        #dbus_interface.connect("PropertiesChanged", self.system_lock._dbus_event)

    def stop_dimmer(self, serial_number: str) -> None:
        """Stops the dimmer for the given Stream Deck

        :param serial_number: The Stream Deck serial number.
        :type serial_number: str
        """
        self.dimmers[serial_number].stop()

    def reset_dimmer(self, serial_number: str) -> bool:
        """Resets the dimmer for the given Stream Deck. This means the display
        will not be dimmed and the timer starts. Reloads configuration.

        Args:
            serial_number (str): The Stream Deck serial number
        Returns:
            bool: Returns True if the dimmer had to be reset (i.e. woken up), False otherwise.
        """
        self.dimmers[serial_number].brightness = self.get_brightness(serial_number)
        self.dimmers[serial_number].brightness_dimmed = self.get_brightness_dimmed(serial_number)
        return self.dimmers[serial_number].reset()

    def toggle_dimmers(self):
        """If at least one Deck is still "on", all will be dimmed off. Otherwise,
        toggles displays on.
        """
        at_least_one = False
        for _serial_number, dimmer in self.dimmers.items():
            if not dimmer.dimmed:
                at_least_one = True
                break

        for _serial_number, dimmer in self.dimmers.items():
            if at_least_one:
                dimmer.dim()
            else:
                dimmer.dim(True)

    def _cpu_usage_callback(self, serial_number: str, cpu_usage: int):
        """An internal method that takes emits a signal on a QObject.

        :param serial_number: The Stream Deck serial number
        :type serial_number: str
        :param cpu_usage: The current CPU usage
        :type cpu_usage: int
        """
        self.plugevents.cpu_changed.emit(serial_number, cpu_usage)

    def _key_change_callback(self, serial_number: str, _deck: StreamDeck.StreamDeck, key: int, state: bool) -> None:
        """Callback whenever a key is pressed.

        Stream Deck key events fire on a background thread. Emit a signal
        to bring it back to UI thread, so we can use Qt objects for timers etc.
        Since multiple keys could fire simultaneously, we need to protect
        shared state with a lock
        """
        with self.key_event_lock:
            self.display_handlers[serial_number].set_keypress(key, state)
            self.streamdeck_keys.key_pressed.emit(serial_number, key, state)

    def get_display_timeout(self, serial_number: str) -> int:
        """Returns the amount of time in seconds before the display gets dimmed."""
        if serial_number not in self.state:
            return 0
        return self.state[serial_number].display_timeout

    def set_display_timeout(self, serial_number: str, timeout: int) -> None:
        """Sets the amount of time in seconds before the display gets dimmed."""
        if serial_number not in self.state:
            return

        if self.state[serial_number].display_timeout == timeout:
            return

        self.state[serial_number].display_timeout = timeout
        self.dimmers[serial_number].timeout = timeout

        self._save_state()

    def _save_state(self):
        self.export_config(STATE_FILE)

    def open_config(self, config_file: str):
        self.state = read_state_from_config(config_file)

    def import_config(self, config_file: str) -> None:
        self.stop()
        self.open_config(config_file)
        self._save_state()
        self.start()

    def export_config(self, output_file: str) -> None:
        write_state_to_config(output_file, self.state)

    def _on_steam_deck_attached(self, streamdeck_id: str, streamdeck: StreamDeck):
        streamdeck.open()
        streamdeck.reset()
        serial_number = streamdeck.get_serial_number()

        self.decks_map_id_to_serial[streamdeck_id] = serial_number
        self.decks_by_serial[serial_number] = streamdeck

        self.set_default_state(serial_number, streamdeck.deck_type())
        self._initialize_stream_deck_page_state(serial_number, 0, streamdeck.key_count())

        streamdeck.set_key_callback(partial(self._key_change_callback, serial_number))
        self._update_streamdeck_filters(serial_number)

        self.dimmers[serial_number] = Dimmer(
            self.get_display_timeout(serial_number),
            self.get_brightness(serial_number),
            self.get_brightness_dimmed(serial_number),
            lambda brightness: self.decks_by_serial[serial_number].set_brightness(brightness),
        )
        self.dimmers[serial_number].reset()

        self.plugevents.attached.emit(
            {
                "id": streamdeck_id,
                "serial_number": serial_number,
                "type": streamdeck.deck_type(),
                "layout": streamdeck.key_layout(),
            }
        )

    def set_default_state(self, serial_number: str, deck_type: str):
        if serial_number in self.state:
            return
        elif deck_type in self.state:
            logger.info(f"no configuration found for {serial_number}, use generic configuration for type: {deck_type}.")
            self.state[serial_number] = deepcopy(self.state[deck_type])

    def _initialize_stream_deck_page_state(self, serial_number: str, page: int, key_count: int):
        """Initializes the state for the given serial number. This allocates
        buttons and pages based on the layout.

        :param serial_number: The Stream Deck serial number
        :type serial_number: str
        :param page: The page of the Stream Deck
        :type page: int
        :param key_count: The total number of buttons on the Stream Deck
        :type key_count: int
        """
        self.state[serial_number] = self.state.setdefault(serial_number, DeckState())
        for button in range(key_count):
            self._button_state(serial_number, page, button)

    def add_new_page(self, serial_number: str):
        """Adds a new page to the Stream Deck

        :param serial_number: The Stream Deck serial number
        :type serial_number: str
        :return: The new page index
        :rtype: int
        """
        pages = self.get_pages(serial_number)
        new_page_index = self._calculate_new_index(pages)
        self._initialize_stream_deck_page_state(
            serial_number, new_page_index, self.decks_by_serial[serial_number].key_count()
        )
        self.display_handlers[serial_number].initialize_page(new_page_index)
        self.display_handlers[serial_number].synchronize()

        return new_page_index

    @staticmethod
    def _calculate_new_index(items: List[int]) -> int:
        """Calculates the next free index for a list of items"""
        items_set = set(items)
        max_item = max(items) if items else 0

        for item_index in range(1, max_item + 2):
            if item_index not in items_set:
                return item_index
        return max_item + 2

    def remove_page(self, serial_number: str, page: int):
        """Removes a page from the Stream Deck

        :param serial_number: The Stream Deck serial number
        :type serial_number: str
        :param page: The page index
        :type page: int
        """
        if len(self.get_pages(serial_number)) == 1:
            return

        del self.state[serial_number].buttons[page]
        self.display_handlers[serial_number].remove_page(page)

    def _on_steam_deck_detached(self, deck_id: str):
        serial_number = self.decks_map_id_to_serial.get(deck_id, None)
        if serial_number:
            self._cleanup(deck_id, serial_number)
            self.plugevents.detached.emit(serial_number)

    def _cleanup(self, deck_id: str, serial_number: str):
        display_grid = self.display_handlers[serial_number]
        display_grid.stop()
        del self.display_handlers[serial_number]

        dimmer = self.dimmers[serial_number]
        dimmer.stop()
        del self.dimmers[serial_number]

        streamdeck = self.decks_by_serial[serial_number]
        try:
            if streamdeck.connected():
                streamdeck.set_brightness(50)
                streamdeck.reset()
                streamdeck.close()
        except TransportError:
            pass

        del self.decks_by_serial[serial_number]
        del self.decks_map_id_to_serial[deck_id]

    def start(self):
        if not self.monitor:
            self.monitor = StreamDeckMonitor(self.lock, self._on_steam_deck_attached, self._on_steam_deck_detached)
        self.monitor.start()

    def stop(self):
        self.monitor.stop()

    def get_deck_layout(self, serial_number: str) -> Tuple[int, int]:
        """Returns a tuple containing the number of rows and columns for the specified Stream Deck"""
        return self.decks_by_serial[serial_number].key_layout()

    def _button_state(self, serial_number: str, page: int, button: int, state: Optional[int] = None) -> ButtonState:
        multi_state = self._button_multi_state(serial_number, page, button)
        # if no state is specified, use the current state
        choose_state = state or multi_state.state
        # if the choose state is not in the states dict, add it
        multi_state.states[choose_state] = multi_state.states.setdefault(choose_state, ButtonState())
        return multi_state.states[choose_state]

    def get_button_state_object(self, serial_number: str, page: int, button: int, state: int) -> ButtonState:
        """Returns the ButtonState object for the given button"""
        return self._button_state(serial_number, page, button, state)

    def _button_multi_state(self, serial_number: str, page: int, button: int) -> ButtonMultiState:
        """Returns the ButtonMultiState for the given button"""
        # if the page is not in the pages dict, add it
        self.state[serial_number].buttons[page] = self.state[serial_number].buttons.setdefault(page, {})
        # if the button is not in the buttons dict, add it with a default state
        self.state[serial_number].buttons[page][button] = (
            self.state[serial_number]
            .buttons[page]
            .setdefault(button, ButtonMultiState(state=0, states={0: ButtonState()}))
        )
        return self.state[serial_number].buttons[page][button]

    def get_button_state(self, serial_number: str, page: int, button: int) -> int:
        """Returns the state of a button"""
        return self._button_multi_state(serial_number, page, button).state

    def get_button_states(self, serial_number: str, page: int, button: int) -> List[int]:
        """Returns the states of a button"""
        return sorted(list(self._button_multi_state(serial_number, page, button).states.keys()))

    def add_new_button_state(self, serial_number: str, page: int, button: int) -> int:
        """Adds a new button state"""
        states = self.get_button_states(serial_number, page, button)
        new_button_state_index = self._calculate_new_index(states)
        self._button_multi_state(serial_number, page, button).states[new_button_state_index] = ButtonState()
        return new_button_state_index

    def remove_button_state(self, serial_number: str, page: int, button: int, state: int) -> None:
        """Removes a button state"""
        if len(self.get_button_states(serial_number, page, button)) == 1:
            return
        del self._button_multi_state(serial_number, page, button).states[state]

    def set_button_state(self, serial_number: str, page: int, button: int, state: int) -> None:
        """Sets the state of a button"""
        if self.get_button_state(serial_number, page, button) != state:
            states = self.get_button_states(serial_number, page, button)
            if state in states:
                self._button_multi_state(serial_number, page, button).state = state
                self._save_state()
                self._update_button_filters(serial_number, page, button)
                display_handler = self.display_handlers[serial_number]
                display_handler.synchronize()

    def get_button_switch_state(self, serial_number: str, page: int, button: int) -> int:
        """Returns the state switch set for the specified button. 0 implies no state switch."""
        return self._button_state(serial_number, page, button).switch_state

    def set_button_switch_state(self, serial_number: str, page: int, button: int, switch_state: int) -> None:
        """Sets the state switch associated with the button"""
        if self.get_button_switch_state(serial_number, page, button) != switch_state:
            self._button_state(serial_number, page, button).switch_state = switch_state
            self._save_state()

    def swap_buttons(self, serial_number: str, page: int, source_button: int, target_button: int) -> None:
        """Swaps the properties of the source and target buttons"""
        temp = self.state[serial_number].buttons[page][source_button]
        self.state[serial_number].buttons[page][source_button] = self.state[serial_number].buttons[page][target_button]
        self.state[serial_number].buttons[page][target_button] = temp
        self._save_state()

        # Update rendering for these two images
        self._update_button_filters(serial_number, page, source_button)
        self._update_button_filters(serial_number, page, target_button)
        display_handler = self.display_handlers[serial_number]
        display_handler.synchronize()

    def set_button_text(self, deck_id: str, page: int, button: int, text: str) -> None:
        """Set the text associated with a button"""
        if self.get_button_text(deck_id, page, button) != text:
            self._button_state(deck_id, page, button).text = text
            self._save_state()
            self._update_button_filters(deck_id, page, button)
            display_handler = self.display_handlers[deck_id]
            display_handler.synchronize()

    def get_button_text(self, deck_id: str, page: int, button: int) -> str:
        """Returns the text set for the specified button"""
        return self._button_state(deck_id, page, button).text

    def set_button_icon(self, deck_id: str, page: int, button: int, icon: str) -> None:
        """Sets the icon associated with a button"""
        if self.get_button_icon(deck_id, page, button) != icon:
            self._button_state(deck_id, page, button).icon = icon
            self._save_state()

            self._update_button_filters(deck_id, page, button)
            display_handler = self.display_handlers[deck_id]
            display_handler.synchronize()

    def get_button_text_vertical_align(self, serial_number: str, page: int, button: int) -> str:
        """Gets the vertical text alignment. Values are bottom, middle-bottom, middle, middle-top, top"""
        return self._button_state(serial_number, page, button).text_vertical_align

    def get_button_text_horizontal_align(self, serial_number: str, page: int, button: int) -> str:
        """Gets the horizontal text alignment. Values are left, center, right"""
        return self._button_state(serial_number, page, button).text_horizontal_align

    def set_button_text_horizontal_align(self, serial_number: str, page: int, button: int, alignment: str) -> None:
        """Gets the horizontal text alignment. Values are left, center, right"""
        if self.get_button_text_horizontal_align(serial_number, page, button) != alignment:
            self._button_state(serial_number, page, button).text_horizontal_align = alignment
            self._save_state()
            self._update_button_filters(serial_number, page, button)
            display_handler = self.display_handlers[serial_number]
            display_handler.synchronize()

    def set_button_text_vertical_align(self, serial_number: str, page: int, button: int, alignment: str) -> None:
        """Gets the vertical text alignment. Values are bottom, middle-bottom, middle, middle-top, top"""
        if self.get_button_text_vertical_align(serial_number, page, button) != alignment:
            self._button_state(serial_number, page, button).text_vertical_align = alignment
            self._save_state()
            self._update_button_filters(serial_number, page, button)
            display_handler = self.display_handlers[serial_number]
            display_handler.synchronize()

    def set_button_font_color(self, serial_number: str, page: int, button: int, color: str) -> None:
        """Sets the text color associated with a button"""
        if self.get_button_font_color(serial_number, page, button) != color:
            # Don't pollute .streamdeck_ui.json with entries of the default value
            if color == DEFAULT_FONT_COLOR:
                color = ""
            self._button_state(serial_number, page, button).font_color = color
            self._save_state()
            self._update_button_filters(serial_number, page, button)

            try:
                display_handler = self.display_handlers[serial_number]
                display_handler.synchronize()
            except KeyError:
                raise ValueError(f"Invalid serial number: {serial_number}")

    def get_button_font_color(self, serial_number: str, page: int, button: int) -> str:
        """Returns the text color set for the specified button"""
        return self._button_state(serial_number, page, button).font_color

    def set_button_background_color(self, serial_number: str, page: int, button: int, color: str) -> None:
        """Sets the background color associated with a button"""
        if self.get_button_background_color(serial_number, page, button) != color:
            # Don't pollute .streamdeck_ui.json with entries of the default value
            if color == DEFAULT_BACKGROUND_COLOR:
                color = ""
            self._button_state(serial_number, page, button).background_color = color
            self._save_state()
            self._update_button_filters(serial_number, page, button)

            try:
                display_handler = self.display_handlers[serial_number]
                display_handler.synchronize()
            except KeyError:
                raise ValueError(f"Invalid serial number: {serial_number}")

    def get_button_background_color(self, serial_number: str, page: int, button: int) -> str:
        """Returns the background color set for the specified button"""
        return self._button_state(serial_number, page, button).background_color

    def get_button_icon_pixmap(self, serial_number: str, page: int, button: int) -> Optional[QPixmap]:
        """Returns the QPixmap value for the given button (streamdeck, page, button)"""
        pil_image = self.display_handlers[serial_number].get_image(page, button)
        if pil_image:
            qt_image = ImageQt(pil_image)
            qt_image = qt_image.convertToFormat(QImage.Format.Format_ARGB32)
            return QPixmap(qt_image)
        return None

    def get_button_icon(self, serial_number: str, page: int, button: int) -> str:
        """Returns the icon path for the specified button"""
        return self._button_state(serial_number, page, button).icon

    def set_button_change_brightness(self, serial_number: str, page: int, button: int, amount: int) -> None:
        """Sets the brightness changing associated with a button"""
        if self.get_button_change_brightness(serial_number, page, button) != amount:
            self._button_state(serial_number, page, button).brightness_change = amount
            self._save_state()

    def get_button_change_brightness(self, serial_number: str, page: int, button: int) -> int:
        """Returns the brightness change set for a particular button"""
        return self._button_state(serial_number, page, button).brightness_change

    def set_button_command(self, serial_number: str, page: int, button: int, command: str) -> None:
        """Sets the command associated with the button"""
        if self.get_button_command(serial_number, page, button) != command:
            self._button_state(serial_number, page, button).command = command
            self._save_state()

    def get_button_command(self, serial_number: str, page: int, button: int) -> str:
        """Returns the command set for the specified button"""
        return self._button_state(serial_number, page, button).command

    def set_button_switch_page(self, serial_number: str, page: int, button: int, switch_page: int) -> None:
        """Sets the page switch associated with the button"""
        if self.get_button_switch_page(serial_number, page, button) != switch_page:
            self._button_state(serial_number, page, button).switch_page = switch_page
            self._save_state()

    def get_button_switch_page(self, serial_number: str, page: int, button: int) -> int:
        """Returns the page switch set for the specified button. 0 implies no page switch."""
        return self._button_state(serial_number, page, button).switch_page

    def set_button_keys(self, serial_number: str, page: int, button: int, keys: str) -> None:
        """Sets the keys associated with the button"""
        if self.get_button_keys(serial_number, page, button) != keys:
            self._button_state(serial_number, page, button).keys = keys
            self._save_state()

    def set_button_font(self, serial_number: str, page: int, button: int, font: str) -> None:
        if self.get_button_font(serial_number, page, button) != font:
            # Don't pollute .streamdeck_ui.json with entries of the default value
            if font.endswith(DEFAULT_FONT):
                font = ""
            self._button_state(serial_number, page, button).font = font
            self._save_state()
            self._update_button_filters(serial_number, page, button)
            display_handler = self.display_handlers[serial_number]
            display_handler.synchronize()

    def get_button_font_size(self, serial_number: str, page: int, button: int) -> int:
        """Returns the font size set for the specified button"""
        return self._button_state(serial_number, page, button).font_size

    def set_button_font_size(self, serial_number: str, page: int, button: int, font_size: int) -> None:
        if self.get_button_font_size(serial_number, page, button) != font_size:
            # Don't pollute .streamdeck_ui.json with entries of the default value
            if font_size == DEFAULT_FONT_SIZE:
                font_size = 0
            self._button_state(serial_number, page, button).font_size = font_size
            self._save_state()
            self._update_button_filters(serial_number, page, button)
            display_handler = self.display_handlers[serial_number]
            display_handler.synchronize()

    def get_button_keys(self, serial_number: str, page: int, button: int) -> str:
        """Returns the keys set for the specified button"""
        return self._button_state(serial_number, page, button).keys

    def get_button_font(self, serial_number: str, page: int, button: int) -> str:
        """Returns the font set for the specified button"""
        return self._button_state(serial_number, page, button).font

    def set_button_write(self, serial_number: str, page: int, button: int, write: str) -> None:
        """Sets the text meant to be written when button is pressed"""
        if self.get_button_write(serial_number, page, button) != write:
            self._button_state(serial_number, page, button).write = write
            self._save_state()

    def get_button_write(self, serial_number: str, page: int, button: int) -> str:
        """Returns the text to be produced when the specified button is pressed"""
        return self._button_state(serial_number, page, button).write

    def set_brightness(self, serial_number: str, brightness: int) -> None:
        """Sets the brightness for every button on the deck"""
        if self.get_brightness(serial_number) != brightness:
            self.decks_by_serial[serial_number].set_brightness(brightness)
            self.state[serial_number].brightness = brightness
            self._save_state()

    def get_brightness(self, serial_number: str) -> int:
        """Gets the brightness that is set for the specified stream deck"""
        return self.state[serial_number].brightness

    def get_brightness_dimmed(self, serial_number: str) -> int:
        """Gets the percentage value of the full brightness that is used when dimming the specified
        stream deck"""
        return self.state[serial_number].brightness_dimmed

    def set_brightness_dimmed(self, serial_number: str, brightness_dimmed: int) -> None:
        """Sets the percentage value that will be used for dimming the full brightness"""
        self.state[serial_number].brightness_dimmed = brightness_dimmed
        self._save_state()

    def change_brightness(self, deck_id: str, amount: int = 1) -> None:
        """Change the brightness of the deck by the specified amount"""
        brightness = max(min(self.get_brightness(deck_id) + amount, 100), 0)
        self.set_brightness(deck_id, brightness)
        self.dimmers[deck_id].brightness = brightness
        self.dimmers[deck_id].reset()

    def get_pages(self, serial_number: str) -> List[int]:
        """Returns pages for the specified stream deck"""
        return sorted(list(self.state[serial_number].buttons.keys()))

    def get_page(self, serial_number: str) -> int:
        """Gets the current page shown on the stream deck"""
        return self.state[serial_number].page

    def set_page(self, serial_number: str, page: int) -> None:
        """Sets the current page shown on the stream deck"""
        if self.get_page(serial_number) != page:
            if page not in self.get_pages(serial_number):
                return
            self.state[serial_number].page = page
            self._save_state()

        display_handler = self.display_handlers[serial_number]

        # Let the display know to process new set of pipelines
        display_handler.set_page(page)
        # Wait for at least one cycle
        display_handler.synchronize()

    def _update_streamdeck_filters(self, serial_number: str):
        """Updates the filters for all the StreamDeck buttons.

        :param serial_number: The StreamDeck serial number.
        :type serial_number: str
        """

        # if deck is not attached then do nothing
        if serial_number not in self.decks_by_serial:
            return

        pages = self.get_pages(serial_number)
        display_handler = self.display_handlers.get(
            serial_number, DisplayGrid(self.lock, self.decks_by_serial[serial_number], pages, self._cpu_usage_callback)
        )
        display_handler.set_page(self.get_page(serial_number))
        self.display_handlers[serial_number] = display_handler

        for page, buttons in self.state[serial_number].buttons.items():
            for button in buttons:
                self._update_button_filters(serial_number, page, button)

        display_handler.start()

    def _update_button_filters(self, serial_number: str, page: int, button: int):
        """Sets the filters for a given button. Any previous filters are replaced.

        :param serial_number: The StreamDeck serial number
        :type serial_number: str
        :param page: The page number
        :type page: int
        :param button: The button to update
        :type button: int
        """
        display_handler = self.display_handlers[serial_number]
        button_settings = self._button_state(serial_number, page, button)
        filters: List[Filter] = []

        background_color = button_settings.background_color or DEFAULT_BACKGROUND_COLOR
        filters.append(BackgroundColorFilter(background_color))

        if button_settings.icon:
            filters.append(ImageFilter(button_settings.icon))

        if button_settings.text:
            font_size = button_settings.font_size or DEFAULT_FONT_SIZE
            font_color = button_settings.font_color or DEFAULT_FONT_COLOR
            font = button_settings.font or DEFAULT_FONT
            # if font is not absolute means a default font, prefix it
            if not font.startswith("/"):
                font = os.path.join(FONTS_PATH, font)
            # add fallback font logic
            filters.append(
                TextFilter(
                    button_settings.text,
                    font,
                    font_size,
                    font_color,
                    button_settings.text_vertical_align,
                    button_settings.text_horizontal_align,
                )
            )

        display_handler.replace(page, button, filters)

    def _set_system_lock_state(self, locked: bool) -> None:
        self.system_locked = locked
