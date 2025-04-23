import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import socket
import threading
import time
import speech_recognition as sr
from googletrans import Translator as GoogleTranslator # Using the unofficial library for simplicity
import deepl
import queue # For thread-safe communication with GUI

# --- Configuration ---
BOARD_IP = "192.168.1.100"  # !!! استبدل بعنوان IP الخاص باللوحة الإلكترونية !!!
BOARD_PORT = 9999          # !!! استبدل بالمنفذ الذي تستمع إليه اللوحة !!!
RECONNECT_DELAY = 5      # Delay in seconds before attempting reconnection
DEEPL_AUTH_KEY = "YOUR_DEEPL_API_KEY"  # !!! استبدل بمفتاح DeepL API الخاص بك !!!

# --- Language Codes ---
LANGUAGES = {
    "العربية": "ar",
    "English": "en",
    "Français": "fr",
}
# Mapping for DeepL target languages (some might need adjustments like EN-US/EN-GB)
DEEPL_LANG_MAP = {
    "ar": "AR",
    "en": "EN-US", # Or "EN-GB"
    "fr": "FR",
}
# Mapping for Speech Recognition languages
SR_LANG_MAP = {
    "ar": "ar-SA",
    "en": "en-US",
    "fr": "fr-FR",
}

# --- Global Variables & Flags ---
connection_thread = None
mic_thread = None
stop_mic_listening = None # Function to stop background listener
network_queue = queue.Queue() # Queue to send data to network thread safely
gui_queue = queue.Queue() # Queue to send status updates to GUI thread safely

is_connected = False
is_listening = False
app_running = True # Flag to signal threads to stop

# --- Translation Functions ---
def translate_text_google(text, target_lang_code):
    """Translates text using Google Translate library."""
    try:
        translator = GoogleTranslator()
        # Detect source language automatically or specify if needed
        translation = translator.translate(text, dest=target_lang_code)
        return translation.text
    except Exception as e:
        log_to_gui(f"Google Translate Error: {e}")
        return None

def translate_text_deepl(text, target_lang_code):
    """Translates text using DeepL API."""
    if not DEEPL_AUTH_KEY or DEEPL_AUTH_KEY == "YOUR_DEEPL_API_KEY":
        log_to_gui("Error: DeepL API Key not configured.")
        return None
    try:
        translator = deepl.Translator(DEEPL_AUTH_KEY)
        deepl_target = DEEPL_LANG_MAP.get(target_lang_code, "EN-US") # Default to English if map fails
        result = translator.translate_text(text, target_lang=deepl_target)
        return result.text
    except deepl.DeepLException as e:
        log_to_gui(f"DeepL Error: {e}")
        return None
    except Exception as e:
        log_to_gui(f"DeepL General Error: {e}")
        return None

# --- Network Handling ---
def network_manager(host, port):
    """Manages the TCP connection, reconnection, and sending data."""
    global is_connected, app_running
    sock = None

    while app_running:
        if sock is None: # Try to connect if not connected
            is_connected = False
            log_to_gui(f"Attempting to connect to {host}:{port}...")
            try:
                # Create a new socket and connect
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5) # Connection timeout
                sock.connect((host, port))
                sock.settimeout(None) # Reset timeout after connection
                is_connected = True
                log_to_gui("Connection established.")
                update_gui_connection_status(True)

            except socket.error as e:
                log_to_gui(f"Connection failed: {e}. Retrying in {RECONNECT_DELAY}s...")
                sock = None # Ensure socket is None if connection failed
                update_gui_connection_status(False)
                # Wait before retrying, but check if app should stop
                for _ in range(RECONNECT_DELAY):
                    if not app_running: return
                    time.sleep(1)
                continue # Retry connection

        # If connected, process the sending queue
        if sock and is_connected:
            try:
                # Wait for data to send (with timeout to allow checking app_running)
                data_to_send = network_queue.get(timeout=0.5)
                if data_to_send is None: # Sentinel value to stop
                    break
                log_to_gui(f"Sending: {data_to_send}")
                sock.sendall(data_to_send.encode('utf-8'))
                network_queue.task_done() # Mark task as completed

            except queue.Empty:
                # No data to send, loop continues
                continue
            except (socket.error, BrokenPipeError, ConnectionResetError) as e:
                log_to_gui(f"Connection lost: {e}. Reconnecting...")
                is_connected = False
                update_gui_connection_status(False)
                if sock:
                    sock.close()
                sock = None # Trigger reconnection attempt in the next loop
            except Exception as e:
                 log_to_gui(f"Network sending error: {e}")
                 # Decide if this error warrants disconnection
                 is_connected = False
                 update_gui_connection_status(False)
                 if sock:
                     sock.close()
                 sock = None

        # Small delay if not connected to prevent busy-waiting
        if not is_connected:
            time.sleep(0.1)


    # Cleanup on exit
    if sock:
        sock.close()
    is_connected = False
    log_to_gui("Network thread stopped.")
    update_gui_connection_status(False)


# --- Speech Recognition Handling ---
def speech_recognition_manager(lang_code, translator_service, target_lang_code):
    """Manages microphone listening and speech-to-text conversion."""
    global is_listening, stop_mic_listening, app_running

    recognizer = sr.Recognizer()
    # Adjust sensitivity based on environment if needed
    # recognizer.energy_threshold = 4000
    # recognizer.dynamic_energy_threshold = True
    # recognizer.pause_threshold = 0.8 # Seconds of non-speaking audio before phrase is considered complete

    microphone = sr.Microphone()

    # Adjust for ambient noise once when starting
    try:
        with microphone as source:
            log_to_gui("Adjusting for ambient noise... Please wait.")
            recognizer.adjust_for_ambient_noise(source, duration=1)
            log_to_gui("Ambient noise adjustment complete. Ready to listen.")
    except Exception as e:
        log_to_gui(f"Microphone Error: {e}. Cannot start listening.")
        is_listening = False
        update_gui_mic_status(False)
        return

    def audio_callback(recognizer, audio):
        """Callback function executed when speech is detected."""
        global is_listening
        if not is_listening or not app_running: # Check if we should still be processing
             return

        log_to_gui("Processing audio...")
        try:
            # Recognize speech using Google Web Speech API (requires internet)
            # Use the language code selected in the GUI for recognition
            spoken_text = recognizer.recognize_google(audio, language=SR_LANG_MAP.get(lang_code, 'en-US'))
            log_to_gui(f"Recognized: {spoken_text}")

            # Translate the text
            translated_text = None
            if translator_service == "Google":
                translated_text = translate_text_google(spoken_text, target_lang_code)
            elif translator_service == "DeepL":
                 translated_text = translate_text_deepl(spoken_text, target_lang_code)

            if translated_text:
                log_to_gui(f"Translated ({target_lang_code}): {translated_text}")
                # Send translated text to the network thread via queue
                if is_connected:
                    network_queue.put(translated_text)
                else:
                    log_to_gui("Warning: Not connected. Translation not sent.")
            else:
                 log_to_gui("Translation failed.")

        except sr.UnknownValueError:
            log_to_gui("Could not understand audio")
        except sr.RequestError as e:
            log_to_gui(f"Could not request results from Google Speech Recognition service; {e}")
        except Exception as e:
            log_to_gui(f"Error during audio processing or translation: {e}")

    # Start listening in the background
    log_to_gui(f"Starting microphone listener (Lang: {lang_code})...")
    stop_mic_listening = recognizer.listen_in_background(microphone, audio_callback, phrase_time_limit=15) # phrase_time_limit helps break long pauses
    log_to_gui("Microphone is now listening.")

    # Keep the thread alive while listening is active and app is running
    while is_listening and app_running:
        time.sleep(0.1)

    # Cleanup when stopped
    if stop_mic_listening:
        log_to_gui("Stopping microphone listener...")
        stop_mic_listening(wait_for_stop=False) # Stop background listener
        stop_mic_listening = None
    log_to_gui("Microphone thread stopped.")


# --- GUI Application ---
class RemoteControlApp:
    def __init__(self, master):
        self.master = master
        master.title("Remote Control Translator")
        master.geometry("600x500")

        # Style
        self.style = ttk.Style()
        self.style.theme_use('clam') # Or 'alt', 'default', 'classic'

        # --- Frames ---
        control_frame = ttk.Frame(master, padding="10")
        control_frame.pack(pady=5, padx=10, fill=tk.X)

        status_frame = ttk.Frame(master, padding="10")
        status_frame.pack(pady=5, padx=10, fill=tk.X)

        log_frame = ttk.Frame(master, padding="10")
        log_frame.pack(pady=10, padx=10, fill=tk.BOTH, expand=True)

        # --- Control Widgets ---
        # Connection
        self.connect_button = ttk.Button(control_frame, text="Connect", command=self.toggle_connection)
        self.connect_button.pack(side=tk.LEFT, padx=5)

        # Microphone
        self.mic_button = ttk.Button(control_frame, text="Start Listening", command=self.toggle_mic, state=tk.DISABLED)
        self.mic_button.pack(side=tk.LEFT, padx=5)

        # Language Selection
        ttk.Label(control_frame, text="Target Language:").pack(side=tk.LEFT, padx=(10, 2))
        self.lang_var = tk.StringVar(value=list(LANGUAGES.keys())[0]) # Default to first language
        lang_options = list(LANGUAGES.keys())
        self.lang_menu = ttk.OptionMenu(control_frame, self.lang_var, lang_options[0], *lang_options, command=self.update_settings)
        self.lang_menu.pack(side=tk.LEFT, padx=5)

        # Translator Selection
        ttk.Label(control_frame, text="Translator:").pack(side=tk.LEFT, padx=(10, 2))
        self.translator_var = tk.StringVar(value="Google") # Default to Google
        self.translator_menu = ttk.OptionMenu(control_frame, self.translator_var, "Google", "Google", "DeepL", command=self.update_settings)
        self.translator_menu.pack(side=tk.LEFT, padx=5)

        # --- Status Widgets ---
        self.conn_status_label = ttk.Label(status_frame, text="Connection: Disconnected", foreground="red")
        self.conn_status_label.pack(side=tk.LEFT, padx=5)
        self.mic_status_label = ttk.Label(status_frame, text="Microphone: Off", foreground="grey")
        self.mic_status_label.pack(side=tk.LEFT, padx=10)
        self.current_settings_label = ttk.Label(status_frame, text=f"Target: {self.lang_var.get()} via {self.translator_var.get()}")
        self.current_settings_label.pack(side=tk.LEFT, padx=10)

        # --- Log Area ---
        self.log_area = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=15, state=tk.DISABLED)
        self.log_area.pack(fill=tk.BOTH, expand=True)

        # --- Initial State ---
        self.is_connecting = False # To prevent multiple connection attempts
        master.protocol("WM_DELETE_WINDOW", self.on_closing) # Handle window close

        # Start GUI update loop
        self.master.after(100, self.process_gui_queue)

    def log_message(self, message):
        """Appends a message to the log area in a thread-safe way."""
        self.log_area.configure(state=tk.NORMAL)
        self.log_area.insert(tk.END, f"{time.strftime('%H:%M:%S')} - {message}\n")
        self.log_area.configure(state=tk.DISABLED)
        self.log_area.see(tk.END) # Scroll to the bottom

    def process_gui_queue(self):
        """Processes messages sent from other threads to update the GUI."""
        try:
            while True:
                message = gui_queue.get_nowait()
                if isinstance(message, tuple) and len(message) == 2:
                    msg_type, value = message
                    if msg_type == "log":
                        self.log_message(value)
                    elif msg_type == "conn_status":
                        self._update_conn_status_label(value)
                    elif msg_type == "mic_status":
                         self._update_mic_status_label(value)
                else:
                    self.log_message(f"Unknown GUI message: {message}")
                gui_queue.task_done()
        except queue.Empty:
            pass # No messages currently

        # Reschedule the processor
        if app_running:
             self.master.after(100, self.process_gui_queue)

    def _update_conn_status_label(self, connected):
        """Updates the connection status label (called from GUI thread)."""
        if connected:
            self.conn_status_label.config(text="Connection: Connected", foreground="green")
            self.mic_button.config(state=tk.NORMAL) # Enable mic button on connect
            self.connect_button.config(text="Disconnect")
        else:
            self.conn_status_label.config(text="Connection: Disconnected", foreground="red")
            self.mic_button.config(state=tk.DISABLED) # Disable mic button on disconnect
            if is_listening: # Stop listening if connection is lost
                self.toggle_mic()
            self.connect_button.config(text="Connect")
        self.is_connecting = False # Reset connecting flag


    def _update_mic_status_label(self, listening):
         """Updates the microphone status label (called from GUI thread)."""
         if listening:
            self.mic_status_label.config(text="Microphone: Listening", foreground="blue")
            self.mic_button.config(text="Stop Listening")
         else:
            self.mic_status_label.config(text="Microphone: Off", foreground="grey")
            self.mic_button.config(text="Start Listening")
            # Only enable mic button if connected
            if is_connected:
                self.mic_button.config(state=tk.NORMAL)
            else:
                self.mic_button.config(state=tk.DISABLED)

    def toggle_connection(self):
        """Starts or stops the network connection thread."""
        global connection_thread, is_connected, app_running

        if is_connected or self.is_connecting: # If connected or trying to connect, disconnect
            if connection_thread and connection_thread.is_alive():
                 log_to_gui("Disconnecting...")
                 # Signal network thread to stop (details depend on network_manager loop)
                 # A simple way is to put a sentinel value in the queue
                 network_queue.put(None) # Signal thread to exit gracefully if waiting on queue
                 # Optionally add a more robust stopping mechanism if needed

                 # We don't forcefully join here, let the thread finish naturally
                 # based on the sentinel or app_running flag.
                 # Setting is_connected to False should be handled by the thread itself
                 # or via the GUI queue update.

            # Update GUI immediately for responsiveness
            is_connected = False
            self._update_conn_status_label(False)

        else: # If disconnected, try to connect
            self.is_connecting = True
            self.conn_status_label.config(text="Connection: Connecting...", foreground="orange")
            self.connect_button.config(text="Connecting...") # Visually indicate attempt

            # Ensure app_running is true before starting thread
            app_running = True

            # Start the network manager in a separate thread
            connection_thread = threading.Thread(target=network_manager, args=(BOARD_IP, BOARD_PORT), daemon=True)
            connection_thread.start()


    def toggle_mic(self):
        """Starts or stops the microphone listening thread."""
        global mic_thread, is_listening, stop_mic_listening, app_running

        if is_listening:
            # Stop listening
            is_listening = False # Signal the thread/callback to stop processing
            update_gui_mic_status(False) # Update GUI immediately
            if stop_mic_listening:
                log_to_gui("Requesting microphone stop...")
                stop_mic_listening(wait_for_stop=False)
                stop_mic_listening = None
            if mic_thread and mic_thread.is_alive():
                 mic_thread.join(timeout=1.0) # Wait briefly for thread to potentially exit
                 if mic_thread.is_alive():
                      log_to_gui("Warning: Mic thread did not stop cleanly.")


        else:
            # Start listening only if connected
            if not is_connected:
                messagebox.showwarning("Not Connected", "Please connect to the board before starting the microphone.")
                return

            is_listening = True
            update_gui_mic_status(True) # Update GUI immediately

            selected_target_lang = LANGUAGES[self.lang_var.get()]
            selected_translator = self.translator_var.get()
            # Assume spoken language is same as target language for simplicity,
            # or choose a fixed input language e.g. 'en'
            # For auto-detection, SR language might be set differently
            selected_sr_lang = selected_target_lang # Or determine dynamically/configure

            mic_thread = threading.Thread(target=speech_recognition_manager,
                                          args=(selected_sr_lang, selected_translator, selected_target_lang),
                                          daemon=True)
            mic_thread.start()

    def update_settings(self, *args):
        """Updates the current settings display when language or translator changes."""
        self.current_settings_label.config(text=f"Target: {self.lang_var.get()} via {self.translator_var.get()}")
        # If listening, potentially restart listener with new settings (optional)
        if is_listening:
            log_to_gui("Settings changed. Restarting microphone listener...")
            self.toggle_mic() # Stop
            # Need a small delay to ensure thread stops before restarting
            self.master.after(500, self.toggle_mic) # Restart after delay


    def on_closing(self):
        """Handles the application close event."""
        global app_running, is_listening

        if messagebox.askokcancel("Quit", "Do you want to quit?"):
            log_to_gui("Closing application...")
            app_running = False # Signal all threads to stop

            # Stop microphone listening first
            if is_listening:
                 is_listening = False
                 if stop_mic_listening:
                     stop_mic_listening(wait_for_stop=False)

            # Signal network thread to stop
            if connection_thread and connection_thread.is_alive():
                 network_queue.put(None) # Sentinel value

            # Wait briefly for threads to potentially finish
            # time.sleep(1) # Optional: Give threads a moment

            self.master.destroy()

# --- Helper functions to update GUI from other threads ---
def log_to_gui(message):
    """Sends a log message to the GUI queue."""
    try:
        gui_queue.put(("log", message))
    except Exception as e:
        print(f"Error adding log to queue: {e}") # Fallback print

def update_gui_connection_status(connected):
    """Sends connection status update to the GUI queue."""
    try:
        gui_queue.put(("conn_status", connected))
    except Exception as e:
        print(f"Error adding conn_status to queue: {e}")

def update_gui_mic_status(listening):
    """Sends microphone status update to the GUI queue."""
    try:
        gui_queue.put(("mic_status", listening))
    except Exception as e:
        print(f"Error adding mic_status to queue: {e}")


# --- Main Execution ---
if __name__ == "__main__":
    root = tk.Tk()
    app = RemoteControlApp(root)
    root.mainloop()

    # Final cleanup check after GUI closes
    app_running = False # Ensure flag is false
    print("Application has exited.")
