import os
import time
import shutil
import zipfile
import datetime
import base64
import traceback
import uuid
from os import listdir
from os.path import isfile, join
from functools import partial
from dotenv import load_dotenv
from nicegui import ui, events, app
from datetime import datetime

# Set up initial logging with default paths
# These will be updated after environment variables are loaded
LOGS_DIR = "data/logs"  # Default path
UI_DEBUG_LOG_PATH = "data/logs/ui_debug.log"  # Default path

def ui_debug_log(message, important=False):
    """Write directly to a file even if the container freezes"""
    try:
        # Ensure logs directory exists
        os.makedirs(LOGS_DIR, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        # Also print to stdout for immediate feedback
        print(f"[LOG] {timestamp} - {message}")
        
        with open(UI_DEBUG_LOG_PATH, "a") as f:
            if important:
                line = f"[{timestamp}] !!! CRITICAL OPERATION !!! {message}\n"
            else:
                line = f"[{timestamp}] {message}\n"
            f.write(line)
            f.flush()  # Force write to disk
            os.fsync(f.fileno())  # Ensure OS flushes to disk
    except Exception as e:
        # Print to stdout if logging fails
        print(f"ERROR LOGGING TO FILE: {str(e)}")
        # Try emergency log
        try:
            emergency_log = os.path.join(os.path.dirname(UI_DEBUG_LOG_PATH), "ui_debug_error.log")
            os.makedirs(os.path.dirname(emergency_log), exist_ok=True)
            with open(emergency_log, "a") as f:
                f.write(f"Error in ui_debug_log: {str(e)}\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as inner_e:
            print(f"CRITICAL: All logging attempts failed: {str(inner_e)}")

from data.const import LANGUAGES, INVERTED_LANGUAGES
from src.util import time_estimate
from src.help import (
    help as help_page,
)  # Renamed to avoid conflict with built-in help function

# Load environment variables
load_dotenv()

# Configuration
ONLINE = os.getenv("ONLINE") == "True"
STORAGE_SECRET = os.getenv("STORAGE_SECRET")
ROOT = os.getenv("ROOT")
WINDOWS = os.getenv("WINDOWS") == "True"
SSL_CERTFILE = os.getenv("SSL_CERTFILE")
SSL_KEYFILE = os.getenv("SSL_KEYFILE")
SUMMARIZATION = os.getenv("SUMMARIZATION") == "True"

# Update log paths now that ROOT is defined
if ROOT:
    print(f"Updating log paths using ROOT={ROOT}")
    LOGS_DIR = join(ROOT, "data", "logs")
    UI_DEBUG_LOG_PATH = join(LOGS_DIR, "ui_debug.log")
    
    # Ensure logs directory exists
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        print(f"Created logs directory at {LOGS_DIR}")
    except Exception as e:
        print(f"ERROR creating logs directory: {str(e)}")

# Removed throttling mechanism as it was addressing a symptom, not the root cause

if WINDOWS:
    os.environ["PATH"] += os.pathsep + "ffmpeg/bin"
    os.environ["PATH"] += os.pathsep + "ffmpeg"

BACKSLASHCHAR = "\\"
user_storage = {}


def read_files(user_id):
    """Read in all files of the user and set the file status if known."""
    user_storage[user_id]["file_list"] = []
    in_path = join(ROOT, "data", "in", user_id)
    out_path = join(ROOT, "data", "out", user_id)
    error_path = join(ROOT, "data", "error", user_id)

    if os.path.exists(in_path):
        for f in listdir(in_path):
            if isfile(join(in_path, f)) and f != "hotwords.txt" and f != "language.txt" and not f.endswith(".processing"):
                file_status = [
                    f,
                    "Datei in Warteschlange. Geschätzte Wartezeit: ",
                    0.0,
                    0,
                    os.path.getmtime(join(in_path, f)),
                ]
                if isfile(join(out_path, f + ".html")):
                    file_status[1] = "Datei transkribiert"
                    file_status[2] = 100.0
                    file_status[3] = 0
                else:
                    estimated_time, _ = time_estimate(join(in_path, f), ONLINE)
                    if estimated_time == -1:
                        estimated_time = 0
                    file_status[3] = estimated_time

                user_storage[user_id]["file_list"].append(file_status)

        files_in_queue = []
        for u in user_storage:
            for f in user_storage[u].get("file_list", []):
                if (
                    "updates" in user_storage[u]
                    and len(user_storage[u]["updates"]) > 0
                    and user_storage[u]["updates"][0] == f[0]
                ):
                    f = user_storage[u]["updates"]
                if f[2] < 100.0:
                    files_in_queue.append(f)

        # Sort the queue by modification time (older files first)
        sorted_queue = sorted(files_in_queue, key=lambda x: x[4])

        for file_status in user_storage[user_id]["file_list"]:
            if file_status[2] < 100.0:
                # Get position in queue (1-based)
                queue_position = next((i + 1 for i, f in enumerate(sorted_queue) if f[0] == file_status[0]), 0)
                
                # If currently processing, show as position 1
                if "updates" in user_storage[user_id] and len(user_storage[user_id]["updates"]) > 0 and user_storage[user_id]["updates"][0] == file_status[0]:
                    queue_position = 1
                
                # Get total queue size
                queue_size = len(sorted_queue)
                
                # Calculate estimated wait time
                estimated_wait_time = sum(f[3] for f in files_in_queue if f[4] < file_status[4])
                wait_time_str = str(datetime.timedelta(seconds=round(estimated_wait_time + file_status[3])))
                
                # Update status message with position
                file_status[1] = f"Position {queue_position}/{queue_size} in der Warteschlange. Geschätzte Wartezeit: {wait_time_str}"

    if os.path.exists(error_path):
        for f in listdir(error_path):
            if isfile(join(error_path, f)) and not f.endswith(".txt"):
                text = "Transkription fehlgeschlagen"
                error_file = join(error_path, f + ".txt")
                if isfile(error_file):
                    with open(error_file, "r") as txtf:
                        content = txtf.read()
                        if content:
                            text = content
                file_status = [f, text, -1, 0, os.path.getmtime(join(error_path, f))]
                if f not in user_storage[user_id]["known_errors"]:
                    user_storage[user_id]["known_errors"].add(f)
                user_storage[user_id]["file_list"].append(file_status)

    user_storage[user_id]["file_list"].sort()


async def handle_upload(e: events.UploadEventArguments, user_id):
    """Save the uploaded file to disk."""
    in_path = join(ROOT, "data", "in", user_id)
    out_path = join(ROOT, "data", "out", user_id)
    error_path = join(ROOT, "data", "error", user_id)

    os.makedirs(in_path, exist_ok=True)
    os.makedirs(out_path, exist_ok=True)

    file_name = e.name

    # Clean up error files if re-uploading
    if os.path.exists(error_path):
        if file_name in user_storage[user_id]["known_errors"]:
            user_storage[user_id]["known_errors"].remove(file_name)
        error_file = join(error_path, file_name)
        error_txt_file = error_file + ".txt"
        if os.path.exists(error_file):
            os.remove(error_file)
        if os.path.exists(error_txt_file):
            os.remove(error_txt_file)

    # Ensure unique file names
    original_file_name = file_name
    for i in range(1, 10001):
        if isfile(join(in_path, file_name)):
            name, ext = os.path.splitext(original_file_name)
            file_name = f"{name}_{i}{ext}"
        else:
            break
    else:
        ui.notify("Zu viele Dateien mit dem gleichen Namen.")
        return

    # Save hotwords if provided
    hotwords_content = app.storage.user.get(f"{user_id}_vocab", "").strip()
    hotwords_file = join(in_path, "hotwords.txt")
    if hotwords_content:
        with open(hotwords_file, "w") as f:
            f.write(hotwords_content)
    elif isfile(hotwords_file):
        os.remove(hotwords_file)

    # Save the selected language
    language = app.storage.user.get(f"{user_id}_language", "").strip()
    language_file = join(in_path, "language.txt")
    if language:
        with open(language_file, "w") as f:
            f.write(language)
    else:
        with open(language_file, "w") as f:
            f.write("de")

    # Save the uploaded file
    with open(join(in_path, file_name), "wb") as f:
        f.write(e.content.read())


def handle_reject(e: events.GenericEventArguments):
    ui.notify("Ungültige Datei. Es können nur Audio/Video-Dateien unter 12GB transkribiert werden.")


def handle_added(e: events.GenericEventArguments, user_id, upload_element, refresh_file_view):
    """After a file was added, refresh the GUI."""
    upload_element.run_method("removeUploadedFiles")
    refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=False)


async def prepare_download(file_name, user_id):
    """Add offline functions to the editor before downloading."""
    import asyncio  # Import here to maintain compatibility

    out_user_dir = join(ROOT, "data", "out", user_id)
    full_file_name = join(out_user_dir, file_name + ".html")

    # Read file asynchronously
    async def read_file_async(path):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: open(path, "r", encoding="utf-8").read())
    
    # Write file asynchronously
    async def write_file_async(path, data):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: open(path, "w", encoding="utf-8").write(data))

    try:
        # Read initial HTML content
        content = await read_file_async(full_file_name)

        # Process update file if it exists
        update_file = full_file_name + "update"
        if os.path.exists(update_file):
            new_content = await read_file_async(update_file)
            start_index = content.find("</nav>") + len("</nav>")
            end_index = content.find("var fileName = ")
            content = content[:start_index] + new_content + content[end_index:]

            await write_file_async(full_file_name, content)
            
            # Remove asynchronously
            await asyncio.get_event_loop().run_in_executor(None, lambda: os.remove(update_file) if os.path.exists(update_file) else None)

        # Replace viewer creation link
        content = content.replace(
            "<div>Bitte den Editor herunterladen, um den Viewer zu erstellen.</div>",
            '<a href="#" id="viewer-link" onclick="viewerClick()" class="btn btn-primary">Viewer erstellen</a>',
        )
        
        # Handle base64 encoding of video file
        if "var base64str = " not in content:
            video_file_path = join(out_user_dir, file_name + ".mp4")
            
            # Make base64 encoding non-blocking
            async def encode_video_async(video_path):
                loop = asyncio.get_event_loop()
                def read_and_encode():
                    try:
                        with open(video_path, "rb") as video_file:
                            return base64.b64encode(video_file.read()).decode("utf-8")
                    except Exception as e:
                        print(f"Error encoding video: {str(e)}")
                        return ""
                        
                return await loop.run_in_executor(None, read_and_encode)
            
            video_base64 = await encode_video_async(video_file_path)

            video_content = f"""
var base64str = "{video_base64}";
var binary = atob(base64str);
var len = binary.length;
var buffer = new ArrayBuffer(len);
var view = new Uint8Array(buffer);
for (var i = 0; i < len; i++) {{
    view[i] = binary.charCodeAt(i);
}}

var blob = new Blob([view], {{ type: "video/MP4" }});
var url = URL.createObjectURL(blob);

var video = document.getElementById("player");

setTimeout(function() {{
  video.pause();
  video.setAttribute('src', url);
}}, 100);
</script>
"""
            content = content.replace("</script>", video_content)

        # Write final HTML file
        final_file_name = full_file_name + "final"
        await write_file_async(final_file_name, content)
        print(f"Asynchronously prepared download for {file_name}")
    except Exception as e:
        print(f"Error in async prepare_download: {str(e)}")
        # Fall back to synchronous version if there's an error
        prepare_download_sync(file_name, user_id)

# Synchronous fallback version
def prepare_download_sync(file_name, user_id):
    """Synchronous fallback for prepare_download."""
    try:
        out_user_dir = join(ROOT, "data", "out", user_id)
        full_file_name = join(out_user_dir, file_name + ".html")

        with open(full_file_name, "r", encoding="utf-8") as f:
            content = f.read()

        update_file = full_file_name + "update"
        if os.path.exists(update_file):
            with open(update_file, "r", encoding="utf-8") as f:
                new_content = f.read()
            start_index = content.find("</nav>") + len("</nav>")
            end_index = content.find("var fileName = ")
            content = content[:start_index] + new_content + content[end_index:]

            with open(full_file_name, "w", encoding="utf-8") as f:
                f.write(content)

            os.remove(update_file)

        content = content.replace(
            "<div>Bitte den Editor herunterladen, um den Viewer zu erstellen.</div>",
            '<a href="#" id="viewer-link" onclick="viewerClick()" class="btn btn-primary">Viewer erstellen</a>',
        )
        if "var base64str = " not in content:
            video_file_path = join(out_user_dir, file_name + ".mp4")
            with open(video_file_path, "rb") as video_file:
                video_base64 = base64.b64encode(video_file.read()).decode("utf-8")

            video_content = f"""
var base64str = "{video_base64}";
var binary = atob(base64str);
var len = binary.length;
var buffer = new ArrayBuffer(len);
var view = new Uint8Array(buffer);
for (var i = 0; i < len; i++) {{
    view[i] = binary.charCodeAt(i);
}}

var blob = new Blob([view], {{ type: "video/MP4" }});
var url = URL.createObjectURL(blob);

var video = document.getElementById("player");

setTimeout(function() {{
  video.pause();
  video.setAttribute('src', url);
}}, 100);
</script>
"""
            content = content.replace("</script>", video_content)

        final_file_name = full_file_name + "final"
        with open(final_file_name, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Synchronously prepared download for {file_name}")
    except Exception as e:
        print(f"Error in sync prepare_download: {str(e)}")


async def download_editor(file_name, user_id):
    """Simplified download function using direct src parameter."""
    try:
        # Ensure output directory exists
        out_user_dir = join(ROOT, "data", "out", user_id)
        os.makedirs(out_user_dir, exist_ok=True)
        
        # Check if the source HTML file exists
        html_file = join(out_user_dir, file_name + ".html")
        if not os.path.exists(html_file):
            error_msg = f"Original HTML file not found: {file_name}.html"
            print(error_msg)
            ui.notify(error_msg, color="negative")
            return
            
        # Prepare the final HTML file
        try:
            prepare_download(file_name, user_id)
            print(f"Successfully prepared HTML file for download: {file_name}")
        except Exception as prep_error:
            error_msg = f"Error preparing download file: {str(prep_error)}"
            print(error_msg)
            ui.notify(error_msg, color="negative")
            return
            
        final_file_name = join(out_user_dir, file_name + ".htmlfinal")
        
        # Verify the file exists and has content
        if not os.path.exists(final_file_name):
            error_msg = f"Final HTML file not found: {final_file_name}"
            print(error_msg)
            ui.notify(error_msg, color="negative")
            return
            
        file_size = os.path.getsize(final_file_name)
        if file_size == 0:
            error_msg = f"Generated file is empty: {final_file_name}"
            print(error_msg)
            ui.notify(error_msg, color="negative")
            return
            
        # Use direct src parameter instead of content - more reliable approach for this app
        download_filename = f"{os.path.splitext(file_name)[0]}.html"
        ui.download(
            src=final_file_name,  # Direct source path instead of loading content
            filename=download_filename
        )
        
        # Success notification
        success_msg = f"Download started: {download_filename}"
        print(success_msg)
        ui.notify(success_msg, color="positive")
    except Exception as e:
        # Handle any unexpected errors
        error_msg = f"Download error: {str(e)}"
        print(error_msg)
        ui.notify(error_msg, color="negative")


async def download_srt(file_name, user_id):
    """Simplified download function for SRT files using direct src parameter."""
    try:
        # Ensure output directory exists
        out_user_dir = join(ROOT, "data", "out", user_id)
        os.makedirs(out_user_dir, exist_ok=True)
        
        srt_file = join(out_user_dir, file_name + ".srt")
        
        # Verify the file exists
        if not os.path.exists(srt_file):
            error_msg = f"SRT file not found: {file_name}.srt"
            print(error_msg)
            ui.notify(error_msg, color="negative")
            return
            
        file_size = os.path.getsize(srt_file)
        if file_size == 0:
            error_msg = f"SRT file is empty: {file_name}.srt"
            print(error_msg)
            ui.notify(error_msg, color="negative")
            return
            
        # Use direct src parameter instead of content - more reliable approach for this app
        download_filename = f"{os.path.splitext(file_name)[0]}.srt"
        ui.download(
            src=srt_file,  # Direct source path instead of loading content
            filename=download_filename
        )
        
        # Success notification
        success_msg = f"Download started: {download_filename}"
        print(success_msg)
        ui.notify(success_msg, color="positive")
    except Exception as e:
        # Handle any unexpected errors
        error_msg = f"SRT download error: {str(e)}"
        print(error_msg)
        ui.notify(error_msg, color="negative")


# We will use NiceGUI's built-in static file serving instead of a custom endpoint


async def open_editor(file_name, user_id):
    out_user_dir = join(ROOT, "data", "out", user_id)
    full_file_name = join(out_user_dir, file_name + ".html")
    with open(full_file_name, "r", encoding="utf-8") as f:
        content = f.read()

    video_path = f"/data/{user_id}/{file_name}.mp4"
    content = content.replace(
        '<video id="player" width="100%" style="max-height: 320px" src="" type="video/MP4" controls="controls" position="sticky"></video>',
        f'<video id="player" width="100%" style="max-height: 320px" src="{video_path}" type="video/MP4" controls="controls" position="sticky"></video>',
    )
    content = content.replace(
        '<video id="player" width="100%" style="max-height: 250px" src="" type="video/MP4" controls="controls" position="sticky"></video>',
        f'<video id="player" width="100%" style="max-height: 250px" src="{video_path}" type="video/MP4" controls="controls" position="sticky"></video>',
    )

    user_storage[user_id]["content"] = content
    user_storage[user_id]["full_file_name"] = full_file_name
    ui.open(editor, new_tab=True)


async def download_all(user_id):
    """Asynchronous download function for all files."""
    import asyncio
    
    # Ensure output directory exists
    out_dir = join(ROOT, "data", "out", user_id)
    os.makedirs(out_dir, exist_ok=True)
    
    # Use a timestamp to create a unique filename
    timestamp = int(time.time())
    zip_file_path = join(out_dir, f"transcribed_files_{timestamp}.zip")
    
    # Create the zip file with all completed files in a separate thread
    async def create_zip_async(file_path):
        loop = asyncio.get_event_loop()
        
        def zip_files():
            try:
                completed_files = 0
                with zipfile.ZipFile(file_path, "w", allowZip64=True) as myzip:
                    for file_status in user_storage[user_id]["file_list"]:
                        if file_status[2] == 100.0:
                            try:
                                # Use synchronous version to avoid nested async issues
                                prepare_download_sync(file_status[0], user_id)
                                final_html = join(out_dir, file_status[0] + ".htmlfinal")
                                if os.path.exists(final_html):
                                    myzip.write(final_html, arcname=file_status[0] + ".html")
                                    completed_files += 1
                                    print(f"Added to zip: {file_status[0]}.html")
                            except Exception as e:
                                print(f"Error processing {file_status[0]}: {str(e)}")
                
                print(f"ZIP file created with {completed_files} files")
                return file_path
            except Exception as e:
                print(f"Error creating ZIP file: {str(e)}")
                return None
        
        return await loop.run_in_executor(None, zip_files)
    
    try:
        # Start ZIP creation and wait for it to complete
        result_path = await create_zip_async(zip_file_path)
        
        if result_path and os.path.exists(result_path):
            # Download using direct file reference
            ui.download(result_path)
            ui.notify(f"Download started: transcribed_files.zip", color="positive")
        else:
            ui.notify("Failed to create ZIP archive", color="negative")
    except Exception as e:
        print(f"Error in download_all: {str(e)}")
        ui.notify(f"Download error: {str(e)}", color="negative")


def delete_file(file_name, user_id, refresh_file_view):
    paths_to_delete = [
        join(ROOT, "data", "in", user_id, file_name),
        join(ROOT, "data", "error", user_id, file_name),
        join(ROOT, "data", "error", user_id, file_name + ".txt"),
    ]
    suffixes = ["", ".txt", ".html", ".mp4", ".srt", ".htmlupdate", ".htmlfinal"]
    for suffix in suffixes:
        paths_to_delete.append(join(ROOT, "data", "out", user_id, file_name + suffix))

    # Also delete any processing marker files
    processing_marker = join(ROOT, "data", "in", user_id, file_name + ".processing")
    paths_to_delete.append(processing_marker)
    
    # Try to delete all the paths
    for path in paths_to_delete:
        if os.path.exists(path):
            try:
                os.remove(path)
                print(f"Deleted file: {path}")
            except Exception as e:
                print(f"Failed to delete {path}: {str(e)}")

    # Delete worker progress files that might be related to this file
    worker_user_dir = join(ROOT, "data", "worker", user_id)
    if os.path.exists(worker_user_dir):
        for f in os.listdir(worker_user_dir):
            if f.endswith(f"_{file_name}"):
                try:
                    os.remove(join(worker_user_dir, f))
                    print(f"Deleted worker file: {join(worker_user_dir, f)}")
                except Exception as e:
                    print(f"Failed to delete worker file {join(worker_user_dir, f)}: {str(e)}")

    refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=True)
    ui.notify(f"Datei '{file_name}' wurde entfernt")


def listen(user_id, refresh_file_view):
    """Periodically check if a file is being transcribed and calculate its estimated progress."""
    print(f"[DEBUG-LISTEN] Running listen check for user: {user_id}")
    t_start = time.time()
    
    # Log directly to file in case of container freeze
    ui_debug_log(f"Running listen check for user: {user_id}")
    
    worker_user_dir = join(ROOT, "data", "worker", user_id)
    
    if os.path.exists(worker_user_dir):
        try:
            worker_files = listdir(worker_user_dir)
            ui_debug_log(f"Found {len(worker_files)} worker files in {worker_user_dir}")
            print(f"[DEBUG-LISTEN] Found {len(worker_files)} worker files in {worker_user_dir}")
        except Exception as e:
            ui_debug_log(f"ERROR listing worker dir: {str(e)}", important=True)
            print(f"[DEBUG-LISTEN] Error listing worker dir: {str(e)}")
            return
        
        for f in worker_files:
            file_path = join(worker_user_dir, f)
            print(f"[DEBUG-LISTEN] Checking worker file: {f}")
            
            if isfile(file_path):
                parts = f.split("_")
                if len(parts) < 3:
                    print(f"[DEBUG-LISTEN] Skipping invalid worker file format: {f}")
                    continue
                    
                estimated_time = float(parts[0])
                start = float(parts[1])
                file_name = "_".join(parts[2:])
                progress = min(0.975, (time.time() - start) / estimated_time)
                estimated_time_left = round(max(1, estimated_time - (time.time() - start)))
                print(f"[DEBUG-LISTEN] Processing file {file_name}, progress: {progress*100:.1f}%, time left: {estimated_time_left}s")

                in_file = join(ROOT, "data", "in", user_id, file_name)
                if os.path.exists(in_file):
                    # Show different message for post-processing phase vs normal transcription
                    if progress > 0.95:
                        status_message = f"Position 1/1 in der Warteschlange. Datei wird nachbearbeitet... (SRT-Datei wird erzeugt, Editor wird erstellt)"
                        print(f"[DEBUG-LISTEN] File {file_name} in post-processing phase")
                    else:
                        status_message = f"Position 1/1 in der Warteschlange. Datei wird transkribiert. Geschätzte Bearbeitungszeit: {datetime.timedelta(seconds=estimated_time_left)}"
                        print(f"[DEBUG-LISTEN] File {file_name} in transcription phase")
                    
                    user_storage[user_id]["updates"] = [
                        file_name,
                        status_message,
                        progress * 100,
                        estimated_time_left,
                        os.path.getmtime(in_file),
                    ]
                    
                    # Persist the updates to the file_list
                    updated = False
                    for i, file_status in enumerate(user_storage[user_id]["file_list"]):
                        if file_status[0] == file_name:
                            user_storage[user_id]["file_list"][i] = user_storage[user_id]["updates"]
                            updated = True
                            break
                else:
                    print(f"[DEBUG-LISTEN] Input file no longer exists, removing worker file: {file_path}")
                    os.remove(file_path)
                
                should_refresh_results = (user_storage[user_id].get("file_in_progress") != file_name)
                print(f"[DEBUG-LISTEN] Calling refresh_file_view with refresh_results={should_refresh_results}")
                refresh_file_view(
                    user_id=user_id,
                    refresh_queue=True,
                    refresh_results=should_refresh_results,
                )
                user_storage[user_id]["file_in_progress"] = file_name
                print(f"[DEBUG-LISTEN] Updated file_in_progress to: {file_name}")
                
                t_end = time.time()
                print(f"[DEBUG-LISTEN] listen() function completed in {t_end - t_start:.3f}s")
                return

        # No files being processed - THIS IS THE CRITICAL POINT WHERE DISCONNECT HAPPENS
        ui_debug_log("No worker files found - checking for previous updates", important=True)
        print(f"[DEBUG-LISTEN] No worker files found for processing - checking for previous updates")
        
        # Get system state at this critical point
        try:
            ui_debug_log("Getting system process information before file completion handling")
            os.system("ps aux > ui_processes_before.txt")
        except Exception as e:
            ui_debug_log(f"Error getting process info: {str(e)}")
        
        # Try to force filesystem sync before critical operations
        try:
            os.sync()
            ui_debug_log("Filesystem buffers synced before file completion handling")
        except Exception as e:
            ui_debug_log(f"Error syncing filesystem: {str(e)}")
            
        if user_storage[user_id].get("updates"):
            ui_debug_log("Updates found in user_storage - handling file completion", important=True)
            
            # Store current file_in_progress before clearing
            previous_file = user_storage[user_id].get("file_in_progress")
            ui_debug_log(f"Previous file in progress: {previous_file}")
            print(f"[DEBUG-LISTEN] Previous file in progress: {previous_file}")
            
            # Clear update data - store old value for later checking
            old_updates = user_storage[user_id].get("updates", [])
            old_file_in_progress = user_storage[user_id].get("file_in_progress")
            
            # CRITICAL OPERATION: Update user_storage
            ui_debug_log("CRITICAL OPERATION: Updating user_storage", important=True)
            ui_debug_log(f"Clearing updates: {old_updates}")
            ui_debug_log(f"Clearing file_in_progress: {old_file_in_progress}")
            
            user_storage[user_id]["updates"] = []
            user_storage[user_id]["file_in_progress"] = None
            ui_debug_log("user_storage updates and file_in_progress cleared")
            print(f"[DEBUG-LISTEN] Cleared updates and file_in_progress")
            
            # Only perform full refresh if we actually had a file in progress
            # This reduces the likelihood of disconnections when files finish processing
            if previous_file:
                ui_debug_log(f"File processing completed: {previous_file}", important=True)
                print(f"[DEBUG-LISTEN] File processing completed: {previous_file}")
                
                # Always do a full refresh when a file is completed
                refresh_results = True
                ui_debug_log(f"File processing completed - performing full refresh", important=True)
                print(f"[DEBUG-LISTEN] File processing completed - performing full refresh")
                
                # Always delay slightly before any refresh
                try:
                    ui_debug_log("Adding delay before UI refresh...")
                    time.sleep(0.5)  # Small delay to let UI breathe before refresh
                    ui_debug_log(f"After delay, calling refresh with refresh_results={refresh_results}", important=True)
                    print(f"[DEBUG-LISTEN] After delay, calling refresh with refresh_results={refresh_results}")
                    
                    # Get another process snapshot before the critical call
                    try:
                        ui_debug_log("Getting system process information before refresh call")
                        os.system("ps aux > ui_processes_refresh.txt")
                    except Exception as e:
                        ui_debug_log(f"Error getting process info: {str(e)}")
                    
                    t_refresh_start = time.time()
                    refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=refresh_results)
                    t_refresh_end = time.time()
                    refresh_duration = t_refresh_end - t_refresh_start
                    
                    ui_debug_log(f"Refresh completed in {refresh_duration:.6f}s", important=True)
                    
                    # If refresh took unusually long, log it as important
                    if refresh_duration > 0.5:
                        ui_debug_log(f"WARNING: UI refresh took {refresh_duration:.6f}s", important=True)
                except Exception as e:
                    ui_debug_log(f"ERROR during refresh after completion: {str(e)}\n{traceback.format_exc()}", important=True)
                    print(f"[DEBUG-LISTEN] Error during refresh after completion: {str(e)}")
            else:
                # Less intensive refresh when no file was actually in progress
                ui_debug_log("No actual file was in progress - performing lighter refresh")
                print(f"[DEBUG-LISTEN] No actual file was in progress - lighter refresh")
                
                t_refresh_start = time.time()
                refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=False)
                t_refresh_end = time.time()
                ui_debug_log(f"Light refresh completed in {t_refresh_end - t_refresh_start:.6f}s")
        else:
            ui_debug_log("No updates found - performing routine refresh")
            print(f"[DEBUG-LISTEN] No updates found - performing routine refresh")
            refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=False)
        
        # Get system state after critical operations
        try:
            ui_debug_log("Getting system process information after completion handling")
            os.system("ps aux > ui_processes_after.txt")
        except Exception as e:
            ui_debug_log(f"Error getting process info: {str(e)}")
        
        # Force filesystem sync at the end
        try:
            os.sync()
            ui_debug_log("Final filesystem sync performed")
        except Exception as e:
            ui_debug_log(f"Error syncing: {str(e)}")
            
        t_end = time.time()
        ui_debug_log(f"listen() function completed in {t_end - t_start:.3f}s")
        print(f"[DEBUG-LISTEN] listen() function completed in {t_end - t_start:.3f}s")


def update_hotwords(user_id):
    if "textarea" in user_storage[user_id]:
        app.storage.user[f"{user_id}_vocab"] = user_storage[user_id]["textarea"].value


def update_language(user_id):
    if "language" in user_storage[user_id]:
        app.storage.user[f"{user_id}_language"] = INVERTED_LANGUAGES[user_storage[user_id]["language"].value]


@ui.page("/editor")
async def editor():
    """Prepare and open the editor for online editing."""

    async def handle_save(full_file_name):
        import asyncio
        
        try:
            # Get content from browser in chunks to avoid memory issues
            content = ""
            try:
                for i in range(100):
                    content_chunk = await ui.run_javascript(
                        f"""
    var content = String(document.documentElement.innerHTML);
    var start_index = content.indexOf('<!--start-->') + '<!--start-->'.length;
    content = content.slice(start_index, content.indexOf('var fileName = ', start_index))
    content = content.slice(content.indexOf('</nav>') + '</nav>'.length, content.length)
    return content.slice({i * 500_000}, {(i + 1) * 500_000});
    """,
                        timeout=60.0,
                    )
                    content += content_chunk
                    if len(content_chunk) < 500_000:
                        break
            except Exception as e:
                print(f"Error retrieving editor content: {str(e)}")
                ui.notify(f"Error retrieving content: {str(e)}", color="negative")
                return

            # Write file asynchronously
            update_file = full_file_name + "update"
            
            async def write_file_async(path, data):
                loop = asyncio.get_event_loop()
                def write_file():
                    try:
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(data)
                        return True
                    except Exception as file_error:
                        print(f"Error writing file {path}: {str(file_error)}")
                        return False
                        
                return await loop.run_in_executor(None, write_file)
            
            # Write file in background
            success = await write_file_async(update_file, content.strip())
            
            if success:
                ui.notify("Änderungen gespeichert.", color="positive")
            else:
                ui.notify("Fehler beim Speichern der Änderungen", color="negative")
        except Exception as e:
            print(f"Error in handle_save: {str(e)}")
            ui.notify(f"Unerwarteter Fehler: {str(e)}", color="negative")

    user_id = str(app.storage.browser.get("id", "local")) if ONLINE else "local"

    out_user_dir = join(ROOT, "data", "out", user_id)
    app.add_media_files(f"/data/{user_id}", out_user_dir)
    user_data = user_storage.get(user_id, {})
    full_file_name = user_data.get("full_file_name")

    if full_file_name:
        ui.on("editor_save", lambda e: handle_save(full_file_name))
        ui.add_body_html("<!--start-->")

        content = user_data.get("content", "")
        update_file = full_file_name + "update"
        if os.path.exists(update_file):
            with open(update_file, "r", encoding="utf-8") as f:
                new_content = f.read()
            start_index = content.find("</nav>") + len("</nav>")
            end_index = content.find("var fileName = ")
            content = content[:start_index] + new_content + content[end_index:]

        content = content.replace(
            '<a href ="#" id="viewer-link" onClick="viewerClick()" class="btn btn-primary">Viewer erstellen</a>',
            "<div>Bitte den Editor herunterladen, um den Viewer zu erstellen.</div>",
        )
        content = content.replace(
            '<a href="#" id="viewer-link" onclick="viewerClick()" class="btn btn-primary">Viewer erstellen</a>',
            "<div>Bitte den Editor herunterladen, um den Viewer zu erstellen.</div>",
        )
        ui.add_body_html(content)

        ui.add_body_html(
            """
<script language="javascript">
    var origFunction = downloadClick;
    downloadClick = function downloadClick() {
        emitEvent('editor_save');
    }
</script>
"""
        )
    else:
        ui.label("Session abgelaufen. Bitte öffne den Editor erneut.")


# API endpoint to receive WebSocket errors from client-side
@ui.post('/api/ws_error')
async def handle_ws_error(client_data: dict):
    """Logs WebSocket errors reported from the browser."""
    ui_debug_log(f"CLIENT-SIDE WEBSOCKET ERROR: {client_data}", important=True)
    return {"status": "logged"}

# WebSocket connection event handlers for diagnosing handshake failures
@app.on_connect
def handle_connect(event):
    """Log when a WebSocket connection is established."""
    user_id = str(app.storage.browser.get("id", "unknown"))
    ui_debug_log(f"WebSocket CONNECTED: client={event.socket_id}, user_id={user_id}", important=True)
    
@app.on_disconnect
def handle_disconnect(event):
    """Log when a WebSocket connection is dropped."""
    user_id = str(app.storage.browser.get("id", "unknown"))
    ui_debug_log(f"WebSocket DISCONNECTED: client={event.socket_id}, user_id={user_id}", important=True)

@app.on_reconnect
def handle_reconnect(event):
    """Log when a client attempts to reconnect."""
    user_id = str(app.storage.browser.get("id", "unknown"))
    ui_debug_log(f"WebSocket RECONNECTING: client={event.socket_id}, user_id={user_id}", important=True)

def inspect_docker_container(user_id):
    """Diagnostic function to check Docker container status related to progress updates."""
    try:
        result = ""
        # Try to find information about running docker containers
        print(f"DEBUG: Checking for docker containers that might handle transcription")
        
        # Check if user directories exist
        worker_dir = join(ROOT, "data", "worker")
        if not os.path.exists(worker_dir):
            result += f"Worker directory {worker_dir} does not exist!\n"
        else:
            result += f"Worker directory {worker_dir} exists\n"
            
            # Check user subdirectory
            user_worker_dir = join(worker_dir, user_id)
            if not os.path.exists(user_worker_dir):
                result += f"User worker directory {user_worker_dir} does not exist!\n"
                os.makedirs(user_worker_dir, exist_ok=True)
                result += f"Created user worker directory\n"
            else:
                result += f"User worker directory {user_worker_dir} exists\n"
                
                # Check for progress files
                try:
                    files = listdir(user_worker_dir)
                    result += f"Found {len(files)} files in user worker directory\n"
                    for f in files:
                        result += f"  - {f}\n"
                except Exception as e:
                    result += f"Error listing files in worker directory: {str(e)}\n"
        
        # Check if input files exist
        in_dir = join(ROOT, "data", "in", user_id)
        if os.path.exists(in_dir):
            try:
                files = [f for f in listdir(in_dir) if isfile(join(in_dir, f)) 
                         and f != "hotwords.txt" and f != "language.txt"]
                result += f"Found {len(files)} input files:\n"
                for f in files:
                    result += f"  - {f}\n"
            except Exception as e:
                result += f"Error listing input files: {str(e)}\n"
        else:
            result += f"Input directory {in_dir} does not exist!\n"
            
        return result
    except Exception as e:
        return f"Error in diagnostic function: {str(e)}"


@ui.page("/")
async def main_page():
    """Main page of the application."""

    def refresh_file_view(user_id, refresh_queue, refresh_results):
        num_errors = len(user_storage[user_id]["known_errors"])
        read_files(user_id)
        if refresh_queue:
            display_queue.refresh(user_id=user_id)
        if refresh_results or num_errors < len(user_storage[user_id]["known_errors"]):
            display_results.refresh(user_id=user_id)

    @ui.refreshable
    def display_queue(user_id):
        for file_status in sorted(user_storage[user_id]["file_list"], key=lambda x: (x[2], -x[4], x[0])):
            if user_storage[user_id].get("updates") and user_storage[user_id]["updates"][0] == file_status[0]:
                file_status = user_storage[user_id]["updates"]
            if 0 <= file_status[2] < 100.0:
                ui.markdown(f"<b>{file_status[0].replace('_', BACKSLASHCHAR + '_')}:</b> {file_status[1]}")
                ui.button(
                    "Abbrechen",
                    on_click=partial(
                        delete_file,
                        file_name=file_status[0],
                        user_id=user_id,
                        refresh_file_view=refresh_file_view,
                    ),
                    color="red-5",
                ).props("no-caps")
                ui.linear_progress(value=file_status[2] / 100, show_value=False, size="10px").props("instant-feedback")
                ui.separator()

    @ui.refreshable
    def display_results(user_id):
        any_file_ready = False
        for file_status in sorted(user_storage[user_id]["file_list"], key=lambda x: (x[2], -x[4], x[0])):
            if user_storage[user_id].get("updates") and user_storage[user_id]["updates"][0] == file_status[0]:
                file_status = user_storage[user_id]["updates"]
            if file_status[2] >= 100.0:
                ui.markdown(f"<b>{file_status[0].replace('_', BACKSLASHCHAR + '_')}</b>")
                with ui.row():
                    ui.button(
                        "Editor herunterladen (Lokal)",
                        on_click=partial(download_editor, file_name=file_status[0], user_id=user_id),
                    ).props("no-caps")
                    ui.button(
                        "Editor öffnen (Server)",
                        on_click=partial(open_editor, file_name=file_status[0], user_id=user_id),
                    ).props("no-caps")
                    ui.button(
                        "SRT-Datei",
                        on_click=partial(download_srt, file_name=file_status[0], user_id=user_id),
                    ).props("no-caps")
                    ui.button(
                        "Datei entfernen",
                        on_click=partial(
                            delete_file,
                            file_name=file_status[0],
                            user_id=user_id,
                            refresh_file_view=refresh_file_view,
                        ),
                        color="red-5",
                    ).props("no-caps")
                    any_file_ready = True
                if SUMMARIZATION:
                    with ui.row():
                        summary_create = ui.button(
                            "Zusammenfassung erstellen",
                            on_click=partial(
                                summarize, file_name=file_status[0], user_id=user_id
                            ),
                        ).props("no-caps")
                        summary_create.disable()
                        summary_download = ui.button(
                            "Zusammenfassung herunterladen",
                            on_click=partial(
                                download_summary,
                                file_name=file_status[0],
                                user_id=user_id,
                            ),
                        ).props("no-caps")
                        summary_download.disable()

                        if os.path.isfile(
                            join(
                                ROOT + "data/out",
                                user_id,
                                file_status[0] + ".htmlsummary",
                            )
                        ):
                            summary_download.enable()
                        if not os.path.isfile(
                            join(
                                ROOT + "data/out",
                                user_id,
                                file_status[0] + ".todosummary",
                            )
                        ):
                            summary_create.enable()
                        else:
                            ui.label("in Bearbeitung")
                ui.separator()
            elif file_status[2] == -1:
                ui.markdown(f"<b>{file_status[0].replace('_', BACKSLASHCHAR + '_')}:</b> {file_status[1]}")
                ui.button(
                    "Datei entfernen",
                    on_click=partial(
                        delete_file,
                        file_name=file_status[0],
                        user_id=user_id,
                        refresh_file_view=refresh_file_view,
                    ),
                    color="red-5",
                ).props("no-caps")
                ui.separator()
        if any_file_ready:
            ui.button(
                "Alle Dateien herunterladen",
                on_click=partial(download_all, user_id=user_id),
            ).props("no-caps")

    def display_files(user_id):
        read_files(user_id)
        with ui.card().classes("border p-4").style("width: min(60vw, 700px);"):
            display_queue(user_id=user_id)
            display_results(user_id=user_id)

    if ONLINE:
        # Get existing ID or create a new one
        user_id = str(app.storage.browser.get("id", ""))
        ui_debug_log(f"ONLINE MODE: Got browser user_id: '{user_id}'", important=True)
        
        # Check the browser storage consistency
        if user_id == "":
            # Generate a new ID on first visit
            import uuid
            user_id = str(uuid.uuid4())
            ui_debug_log(f"Generated new user_id: '{user_id}'", important=True)
            app.storage.browser["id"] = user_id
    else:
        user_id = "local"
        ui_debug_log("OFFLINE MODE: Using local user_id", important=True)

    # Log storage state before initialization
    if user_id in user_storage:
        ui_debug_log(f"User '{user_id}' already exists in user_storage", important=True)
    else:
        ui_debug_log(f"Creating new user_storage entry for '{user_id}'", important=True)
    
    # Initialize user storage
    user_storage[user_id] = {
        "uploaded_files": set(),
        "file_list": [],
        "content": "",
        "content_filename": "",
        "file_in_progress": None,
        "known_errors": set(),
    }

    in_user_tmp_dir = join(ROOT, "data", "in", user_id, "tmp")
    if os.path.exists(in_user_tmp_dir):
        shutil.rmtree(in_user_tmp_dir)

    read_files(user_id)

    with ui.column():
        with ui.header(elevated=True).style("background-color: #0070b4;").props("fit=scale-down").classes("q-pa-xs-xs"):
            ui.image(join(ROOT, "data", "banner.png")).style("height: 90px; width: 443px;")
        with ui.row():
            with ui.column():
                with ui.card().classes("border p-4"):
                    with ui.card().style("width: min(40vw, 400px)"):
                        upload_element = (
                            ui.upload(
                                multiple=True,
                                on_upload=partial(handle_upload, user_id=user_id),
                                on_rejected=handle_reject,
                                label="Dateien auswählen",
                                auto_upload=True,
                                max_file_size=12_000_000_000,
                                max_files=100,
                            )
                            .props('accept="video/*, audio/*, .zip"')
                            .tooltip("Dateien auswählen")
                            .classes("w-full")
                            .style("width: 100%;")
                        )
                        upload_element.on(
                            "uploaded",
                            partial(
                                handle_added,
                                user_id=user_id,
                                upload_element=upload_element,
                                refresh_file_view=refresh_file_view,
                            ),
                        )

                ui.label("")
                
                # Quick timer for checking file progress
                ui.timer(
                    1,  # Check frequently for better responsiveness
                    partial(listen, user_id=user_id, refresh_file_view=refresh_file_view),
                )
                user_storage[user_id]["language"] = ui.select(
                    [LANGUAGES[key] for key in LANGUAGES],
                    value="deutsch",
                    on_change=partial(update_language, user_id),
                    label="Gesprochene Sprache",
                ).style("width: min(40vw, 400px)")
                with (
                    ui.expansion("Vokabular", icon="menu_book")
                    .classes("w-full no-wrap")
                    .style("width: min(40vw, 400px)") as expansion
                ):
                    user_storage[user_id]["textarea"] = ui.textarea(
                        label="Vokabular",
                        placeholder="Zürich\nUster\nUitikon",
                        on_change=partial(update_hotwords, user_id),
                    ).classes("w-full h-full")
                    hotwords = app.storage.user.get(f"{user_id}_vocab", "").strip()
                    if hotwords:
                        user_storage[user_id]["textarea"].value = hotwords
                        expansion.open()
                with (
                    ui.expansion("Informationen", icon="help_outline")
                    .classes("w-full no-wrap")
                    .style("width: min(40vw, 400px)")
                ):
                    ui.label("Diese Prototyp-Applikation wurde vom Statistischen Amt & Amt für Informatik Kanton Zürich entwickelt.")
                ui.button(
                    "Anleitung öffnen",
                    on_click=lambda: ui.open(help_page, new_tab=True),
                ).props("no-caps")

            display_files(user_id=user_id)
            
            # Add WebSocket handshake error monitoring script
            ui.add_body_html("""
            <script>
                // Monitor for WebSocket errors on the client side
                window.addEventListener('error', function(event) {
                    if (event.message && event.message.includes('WebSocket')) {
                        // Report WebSocket errors to the server
                        fetch('/api/ws_error', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                message: event.message,
                                source: event.filename,
                                lineno: event.lineno,
                                colno: event.colno,
                                error: event.error ? event.error.toString() : null,
                                time: new Date().toISOString()
                            })
                        }).catch(e => console.error('Failed to report WebSocket error:', e));
                    }
                });
            </script>
            """)


if __name__ in {"__main__", "__mp_main__"}:
    # Clear debug log at startup
    try:
        with open(UI_DEBUG_LOG_PATH, "w") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] UI process started\n")
    except Exception as e:
        print(f"Error initializing UI debug log: {str(e)}")
    
    ui_debug_log("UI process initialized", important=True)
    ui_debug_log(f"DEVICE={os.getenv('DEVICE')}, ROOT={ROOT}, ONLINE={ONLINE}")
    
    # Log storage secret info (helps diagnose handshake failures)
    if STORAGE_SECRET:
        ui_debug_log(f"Storage secret configured: length={len(STORAGE_SECRET)}, first_chars={STORAGE_SECRET[:3]}...", important=True)
    else:
        ui_debug_log("WARNING: No storage secret configured!", important=True)
    
    # Create all required directories at startup
    for directory in ['data/in', 'data/out', 'data/worker', 'data/error']:
        os.makedirs(join(ROOT, directory), exist_ok=True)
    
    if ONLINE:
        ui.run(
            port=8080,
            title="TranscriboZH",
            storage_secret=STORAGE_SECRET,
            favicon=join(ROOT, "data", "logo.png"),
            
            # Debugging options for WebSocket connections
            show_server_exceptions=True,  # Show exceptions in browser
            reconnect_timeout=60,         # Increase reconnect timeout from default (15s)
            close_timeout=30,             # Increase close timeout for cleaner disconnects
            heartbeat=5,                  # More frequent heartbeats (5s instead of default)
            socket_ping_interval=5,       # Keep connection alive with frequent pings
            socket_ping_timeout=10,       # More time to respond to pings
        )

        # run command with ssl certificate
        # ui.run(port=443, reload=False, title="TranscriboZH", ssl_certfile=SSL_CERTFILE, ssl_keyfile=SSL_KEYFILE, storage_secret=STORAGE_SECRET, favicon=ROOT + "logo.png")
    else:
        ui.run(
            title="Transcribo",
            host="127.0.0.1",
            port=8080,
            storage_secret=STORAGE_SECRET,
            favicon=join(ROOT, "data", "logo.png"),
            
            # Debugging options for WebSocket connections
            show_server_exceptions=True,  # Show exceptions in browser
            reconnect_timeout=60,         # Increase reconnect timeout from default (15s)
            close_timeout=30,             # Increase close timeout for cleaner disconnects
            heartbeat=5,                  # More frequent heartbeats (5s instead of default)
            socket_ping_interval=5,       # Keep connection alive with frequent pings
            socket_ping_timeout=10,       # More time to respond to pings
        )
