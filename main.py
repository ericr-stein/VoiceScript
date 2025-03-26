import os
import time
import shutil
import zipfile
import datetime
import base64
import asyncio
import logging
from os import listdir
from os.path import isfile, join
from functools import partial
from dotenv import load_dotenv
from nicegui import ui, events, app

# Configure logging
# Set to DEBUG level to capture more detailed information
logging.basicConfig(level=logging.DEBUG, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from data.const import LANGUAGES, INVERTED_LANGUAGES
from src.util import time_estimate
from src.help import (
    help as help_page,
)  # Renamed to avoid conflict with built-in help function

# Load environment variablesz
load_dotenv()

# Configuration
ONLINE = os.getenv("ONLINE") == "True"
STORAGE_SECRET = os.getenv("STORAGE_SECRET")
ROOT = os.getenv("ROOT")
WINDOWS = os.getenv("WINDOWS") == "True"
SSL_CERTFILE = os.getenv("SSL_CERTFILE")
SSL_KEYFILE = os.getenv("SSL_KEYFILE")
SUMMARIZATION = os.getenv("SUMMARIZATION") == "True"

if WINDOWS:
    os.environ["PATH"] += os.pathsep + "ffmpeg/bin"
    os.environ["PATH"] += os.pathsep + "ffmpeg"

BACKSLASHCHAR = "\\"
user_storage = {}


async def read_files(user_id):
    """Read in all files of the user and set the file status if known."""
    logger.info(f"[{user_id}] Starting read_files function")
    user_storage[user_id]["file_list"] = []
    in_path = join(ROOT, "data", "in", user_id)
    out_path = join(ROOT, "data", "out", user_id)
    error_path = join(ROOT, "data", "error", user_id)

    if os.path.exists(in_path):
        potential_files = []
        for f in listdir(in_path):
            # Basic filtering first
            if isfile(join(in_path, f)) and f != "hotwords.txt" and f != "language.txt" and not f.endswith(".processing"):
                potential_files.append(f)

        logger.info(f"[{user_id}] Found {len(potential_files)} potential files to process")
        
        # --- Prepare file status and estimate duration asynchronously ---
        estimate_tasks = {}
        for f in potential_files:
            file_status = [
                f,
                "Datei in Warteschlange...", # Default status
                0.0, # Progress
                0,   # Estimated time (will be filled)
                os.path.getmtime(join(in_path, f)), # Modification time
                False # Flag: duration calculated
            ]
            user_storage[user_id]["file_list"].append(file_status)

            if isfile(join(out_path, f + ".html")):
                file_status[1] = "Datei transkribiert"
                file_status[2] = 100.0
                file_status[5] = True # Mark as calculated (no estimate needed)
            else:
                # Create an async task to estimate time
                task = asyncio.create_task(update_estimate_for_file(in_path, f, file_status, ONLINE))
                estimate_tasks[f] = task

        # --- Wait for all estimates to complete ---
        if estimate_tasks:
            try:
                logger.info(f"[{user_id}] Waiting for {len(estimate_tasks)} estimates...")
                results = await asyncio.gather(*estimate_tasks.values(), return_exceptions=True)
                logger.info(f"[{user_id}] Estimates gathered successfully (or with exceptions).")
                # Log any exceptions from gather
                exceptions_found = False
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        filename = list(estimate_tasks.keys())[i]
                        logger.error(f"[{user_id}] Error gathering estimate for {filename}: {result}")
                        exceptions_found = True
                if exceptions_found:
                    logger.warning(f"[{user_id}] There were errors during estimate gathering.")

            except Exception as gather_err:
                logger.exception(f"[{user_id}] Major error in asyncio.gather for estimates: {gather_err}")
                # Potentially return or raise here if gather itself fails catastrophically
                return # Exit read_files if gather fails

        # --- Now calculate queue positions and wait times using the estimates ---
        logger.info(f"[{user_id}] Preparing file queue info...")
        files_in_queue = []
        all_user_files = []
        try: # Add try/except around accessing user_storage potentially from other users
            for u_id in user_storage:
                # Add safety check for file_list existence
                if "file_list" in user_storage[u_id]:
                    all_user_files.extend(user_storage[u_id]["file_list"])
                else:
                    logger.warning(f"[{user_id}] No 'file_list' found for user {u_id} during queue calculation.")
        except Exception as e:
            logger.exception(f"[{user_id}] Error accessing user_storage during queue calculation: {e}")

        logger.info(f"[{user_id}] Total files across users for queue calc: {len(all_user_files)}")

        for file_status in all_user_files:
            # Add validation for file_status structure
            if not isinstance(file_status, list) or len(file_status) < 3:
                logger.warning(f"[{user_id}] Skipping malformed file_status in queue calc: {file_status}")
                continue

            # Update status if currently processing (from listen updates)
            # Make this safer with .get()
            user_updates = user_storage.get(u_id, {}).get("updates", [])
            if user_updates and len(user_updates) > 0 and user_updates[0] == file_status[0]:
                file_status = user_updates # Careful: this modifies the loop variable, maybe assign to new var?

            if file_status[2] < 100.0:
                # Ensure estimate is populated
                if len(file_status) < 4 or not isinstance(file_status[3], (int, float)) or file_status[3] <= 0:
                    if len(file_status) <= 3: file_status.append(60) # Add default estimate
                    else: file_status[3] = 60 # Set default estimate
                    logger.warning(f"[{user_id}] Using default estimate (60s) for {file_status[0]} during queue calc.")
                files_in_queue.append(file_status)

        logger.info(f"[{user_id}] Sorting {len(files_in_queue)} files for queue timing...")
        try:
            # Sort the queue by modification time
            sorted_queue = sorted(files_in_queue, key=lambda x: x[4] if len(x) > 4 and isinstance(x[4], (int, float)) else 0) # Safer key access
        except Exception as sort_err:
            logger.exception(f"[{user_id}] Error sorting file queue: {sort_err}")
            sorted_queue = files_in_queue # Fallback to unsorted if error

        queue_size = len(sorted_queue)
        logger.info(f"[{user_id}] Calculated global queue size: {queue_size}")

        # Calculate wait times for the current user's files
        logger.info(f"[{user_id}] Updating status for user's file_list...")
        try: # Add try/except around the final loop
            if user_id in user_storage and "file_list" in user_storage[user_id]: # Check if user_id still valid
                for file_status in user_storage[user_id]["file_list"]:
                    # Add validation
                    if not isinstance(file_status, list) or len(file_status) < 3: continue

                    if file_status[2] < 100.0:
                        try: # Inner try for queue position logic
                            # Find the file in the global sorted queue
                            queue_position = next((i + 1 for i, f in enumerate(sorted_queue) if isinstance(f, list) and len(f)>0 and f[0] == file_status[0]), 0) # Safer check

                            # If currently processing by this user, show as position 1
                            user_updates = user_storage[user_id].get("updates", [])
                            if user_updates and len(user_updates) > 0 and user_updates[0] == file_status[0]:
                                queue_position = 1
                                if queue_size == 0 and queue_position == 1: queue_size = 1

                            # Calculate estimated wait time
                            # Safer calculation: check types and length before accessing index 4
                            estimated_wait_time = sum(f[3] for f in sorted_queue if isinstance(f, list) and len(f) > 4 and isinstance(f[4], (int, float)) and f[4] < (file_status[4] if len(file_status) > 4 and isinstance(file_status[4], (int, float)) else 0))
                            # Ensure file_status[3] exists and is numeric before adding
                            current_estimate = file_status[3] if len(file_status) > 3 and isinstance(file_status[3], (int, float)) else 60
                            wait_time_str = str(datetime.timedelta(seconds=round(estimated_wait_time + current_estimate)))

                            # Update status message
                            if queue_position > 0:
                                file_status[1] = f"Position {queue_position}/{queue_size} in der Warteschlange. Geschätzte Wartezeit: {wait_time_str}"
                            elif file_status[2] < 100 and file_status[2] >= 0: # Still queued but position unknown
                                logger.warning(f"[{user_id}] Could not determine queue position for {file_status[0]}")
                                file_status[1] = f"In Warteschlange (Position unbekannt). Geschätzte Wartezeit: {wait_time_str}"

                        except Exception as pos_err:
                            logger.exception(f"[{user_id}] Error calculating queue position/wait time for {file_status[0]}: {pos_err}")
                            file_status[1] = "Fehler bei Warteschlangenberechnung" # Update status on error

            else:
                logger.error(f"[{user_id}] User storage disappeared before final status update in read_files.")

        except Exception as final_loop_err:
            logger.exception(f"[{user_id}] Error in final status update loop of read_files: {final_loop_err}")

    # --- Error files handling ---
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

    logger.info(f"[{user_id}] Sorting final file_list...")
    try:
        if user_id in user_storage and "file_list" in user_storage[user_id]: # Check again
            user_storage[user_id]["file_list"].sort(key=lambda x: x[0] if isinstance(x, list) and len(x)>0 else "") # Safer sort key
        else:
            logger.error(f"[{user_id}] User storage disappeared before final sort in read_files.")
    except Exception as sort_final_err:
        logger.exception(f"[{user_id}] Error sorting final file_list: {sort_final_err}")

    logger.info(f"[{user_id}] Finished read_files function.")


async def update_estimate_for_file(in_path, filename, file_status, online):
    """Asynchronously estimates time and updates the file_status list directly."""
    try:
        # Ensure file_status has enough elements, add placeholders if needed
        while len(file_status) < 6:
            file_status.append(None)

        # Properly await the async time_estimate function
        estimated_time, run_time = await time_estimate(join(in_path, filename), online)
        if estimated_time == -1:
            estimated_time = 60  # Default estimate on error
            logger.warning(f"time_estimate failed for {filename}, using default 60s")
        file_status[3] = estimated_time
        file_status[5] = True  # Mark duration as calculated
    except Exception as e:
        logger.exception(f"Error in update_estimate_for_file for {filename}: {e}")
        # Ensure placeholders exist and set defaults
        while len(file_status) < 6:
            file_status.append(None)
        file_status[3] = 60  # Default estimate on error
        file_status[5] = True  # Mark as calculated (even if defaulted)


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


async def handle_added(e: events.GenericEventArguments, user_id, upload_element, refresh_file_view):
    """After a file was added, refresh the GUI."""
    upload_element.run_method("removeUploadedFiles")
    # Manually await read_files first to ensure data is updated
    await read_files(user_id)
    # Then call the UI refresh (now a synchronous function)
    refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=False)


def prepare_download(file_name, user_id):
    """Add offline functions to the editor before downloading."""
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
    """Simple download function for all files - matches original implementation style."""
    # Ensure output directory exists
    out_dir = join(ROOT, "data", "out", user_id)
    os.makedirs(out_dir, exist_ok=True)
    
    # Use a fixed filename as in the original implementation
    zip_file_path = join(out_dir, "transcribed_files.zip")
    
    # Create the zip file with all completed files
    with zipfile.ZipFile(zip_file_path, "w", allowZip64=True) as myzip:
        for file_status in user_storage[user_id]["file_list"]:
            if file_status[2] == 100.0:
                prepare_download(file_status[0], user_id)
                final_html = join(out_dir, file_status[0] + ".htmlfinal")
                if os.path.exists(final_html):
                    myzip.write(final_html, arcname=file_status[0] + ".html")
                    print(f"Added to zip: {file_status[0]}.html")
    
    # Download using the simplest form - exactly like the original code
    ui.download(zip_file_path)


async def delete_file(file_name, user_id, refresh_file_view):
    """Asynchronously delete a file and related files, then refresh the file view."""
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

    # Update data and refresh UI
    await read_files(user_id)  # Make sure data is updated after deletion
    refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=True)
    ui.notify(f"Datei '{file_name}' wurde entfernt")


async def listen(user_id, refresh_file_view):
    """Periodically check if a file is being transcribed and calculate its estimated progress."""
    worker_user_dir = join(ROOT, "data", "worker", user_id)
    currently_processing_file = None
    refreshed_queue = False
    refreshed_results = False
    
    if os.path.exists(worker_user_dir):
        worker_files = listdir(worker_user_dir)
        
        for f in worker_files:
            file_path = join(worker_user_dir, f)
            
            if isfile(file_path):
                try:
                    parts = f.split("_")
                    if len(parts) < 3:
                        continue
                        
                    estimated_time = float(parts[0])
                    start = float(parts[1])
                    file_name = "_".join(parts[2:])
                    currently_processing_file = file_name  # Mark this file as being processed
                    
                    progress = min(0.975, (time.time() - start) / max(1, estimated_time))  # Avoid division by zero
                    estimated_time_left = round(max(1, estimated_time - (time.time() - start)))

                    in_file = join(ROOT, "data", "in", user_id, file_name)
                    if os.path.exists(in_file):
                        # Show different message for post-processing phase vs normal transcription
                        if progress > 0.95:
                            status_message = f"Position 1/1 in der Warteschlange. Datei wird nachbearbeitet... (SRT-Datei wird erzeugt, Editor wird erstellt)"
                        else:
                            wait_time_delta = datetime.timedelta(seconds=estimated_time_left)
                            status_message = f"Position 1/1 in der Warteschlange. Datei wird transkribiert. Geschätzte Bearbeitungszeit: {wait_time_delta}"
                        
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
                                # Only update if progress is newer or file status changes significantly
                                if file_status[2] != progress * 100 or file_status[1] != status_message:
                                    user_storage[user_id]["file_list"][i] = user_storage[user_id]["updates"]
                                    updated = True
                                    refreshed_queue = True  # Mark queue for refresh
                                break
                    else:
                        # File no longer exists in input directory, remove worker file
                        logger.debug(f"Input file {file_name} no longer exists, removing worker file")
                        try:
                            os.remove(file_path)
                        except OSError as e:
                            logger.warning(f"Could not remove worker file {file_path}: {e}")
                except (ValueError, IndexError, FileNotFoundError, OSError) as e:
                    logger.warning(f"Could not process worker file {f}: {e}")
                    try:
                        os.remove(file_path)  # Clean up potentially corrupt worker file
                    except OSError:
                        pass

    # If no worker files were found OR the previously processing file is no longer processing
    if user_storage[user_id].get("file_in_progress") and user_storage[user_id]["file_in_progress"] != currently_processing_file:
        logger.debug(f"File {user_storage[user_id]['file_in_progress']} finished processing or worker file gone.")
        user_storage[user_id]["updates"] = []
        user_storage[user_id]["file_in_progress"] = None
        # Need to refresh lists as file status has changed (queued -> done/error)
        refreshed_queue = True
        refreshed_results = True
        # Trigger read_files to update queue positions and results list fully
        await read_files(user_id)  # <--- Call async read_files here
        
    elif currently_processing_file and user_storage[user_id].get("file_in_progress") != currently_processing_file:
        # A new file started processing
        user_storage[user_id]["file_in_progress"] = currently_processing_file
        refreshed_queue = True
        
    # If state changed requiring refresh, call the refresh function
    if refreshed_queue or refreshed_results:
        logger.debug(f"Refreshing UI: Queue={refreshed_queue}, Results={refreshed_results}")
        refresh_file_view(user_id=user_id, refresh_queue=refreshed_queue, refresh_results=refreshed_results)


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
        content = ""
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

        update_file = full_file_name + "update"
        with open(update_file, "w", encoding="utf-8") as f:
            f.write(content.strip())

        ui.notify("Änderungen gespeichert.")

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

    # Changed from async to sync since it's called in non-async contexts
    def refresh_file_view(user_id, refresh_queue, refresh_results):
        num_errors = len(user_storage[user_id]["known_errors"])
        # Since we're in a sync callback, we need to schedule the async read_files
        # as a task and not wait for it directly
        asyncio.create_task(read_files(user_id))
        if refresh_queue:
            display_queue.refresh(user_id=user_id)
        if refresh_results or num_errors < len(user_storage[user_id]["known_errors"]):
            display_results.refresh(user_id=user_id)

    @ui.refreshable
    def display_queue(user_id):
        logger.info(f"--- display_queue START for user {user_id} ---")
        try:
            # DEFENSIVE CHECK: Ensure user_storage and file_list exist and are valid
            if user_id not in user_storage or \
               not isinstance(user_storage.get(user_id), dict) or \
               "file_list" not in user_storage[user_id] or \
               not isinstance(user_storage[user_id]["file_list"], list):

                logger.warning(f"[{user_id}] display_queue: User storage or file_list not ready or invalid. Rendering empty.")
                # Optionally display a loading or empty message
                ui.label("Lade Warteschlange...")
                return # Exit early if data structure isn't ready

            file_list_snapshot = user_storage[user_id]["file_list"] # Work with a snapshot
            logger.info(f"[{user_id}] display_queue: Processing {len(file_list_snapshot)} items in file_list.")

            files_in_queue = [
                fs for fs in file_list_snapshot
                if isinstance(fs, list) and len(fs) > 2 and isinstance(fs[2], (int, float)) and 0 <= fs[2] < 100.0
            ]
            logger.info(f"[{user_id}] display_queue: Found {len(files_in_queue)} items in queue state.")

            if not files_in_queue:
                ui.label("Warteschlange ist leer.")
                logger.info(f"[{user_id}] display_queue: Queue is empty.")
                return # Exit if queue is empty after filtering

            # Sort and display (Add try/except around sorting for robustness)
            try:
                 sorted_queue_items = sorted(files_in_queue, key=lambda x: (x[2], -x[4] if len(x) > 4 and isinstance(x[4], (int, float)) else 0, x[0]))
            except Exception as sort_err:
                 logger.error(f"[{user_id}] display_queue: Error sorting queue items: {sort_err}")
                 sorted_queue_items = files_in_queue # Use unsorted as fallback

            for file_status in sorted_queue_items:
                logger.debug(f"[{user_id}] display_queue: Rendering item {file_status[0]}")

                # Check if updates exist and apply them safely
                current_update = user_storage[user_id].get("updates")
                if current_update and isinstance(current_update, list) and len(current_update) > 0 and current_update[0] == file_status[0]:
                    file_status_display = current_update
                else:
                    file_status_display = file_status

                # Validate structure before accessing
                if not isinstance(file_status_display, list) or len(file_status_display) < 3:
                    logger.warning(f"[{user_id}] display_queue: Malformed file_status, skipping render: {file_status_display}")
                    continue

                # --- UI Elements ---
                # Use try/except for individual UI elements if needed, though less likely here
                try:
                    status_text = file_status_display[1] if len(file_status_display) > 1 else "Status unbekannt"
                    progress_val = file_status_display[2] if len(file_status_display) > 2 and isinstance(file_status_display[2], (int, float)) else 0.0

                    ui.markdown(f"<b>{file_status_display[0].replace('_', BACKSLASHCHAR + '_')}:</b> {status_text}")
                    ui.button(
                        "Abbrechen",
                        on_click=lambda f=file_status_display[0], u=user_id, r=refresh_file_view: asyncio.create_task(delete_file(f, u, r)),
                        color="red-5",
                    ).props("no-caps")
                    ui.linear_progress(value=max(0.0, min(1.0, progress_val / 100.0)), show_value=False, size="10px").props("instant-feedback") # Clamp progress value
                    ui.separator()
                except Exception as element_err:
                     logger.exception(f"[{user_id}] display_queue: Error creating UI element for {file_status_display[0]}: {element_err}")


            logger.info(f"[{user_id}] display_queue: Finished rendering queue items.")

        except Exception as e:
            logger.exception(f"[{user_id}] !!! TOP LEVEL ERROR INSIDE display_queue !!!: {e}")
            try:
                ui.label(f"Schwerwiegender Fehler beim Anzeigen der Warteschlange: {e}")
            except Exception: pass
        logger.info(f"--- display_queue END for user {user_id} ---")

    @ui.refreshable
    def display_results(user_id):
        logger.info(f"--- display_results START for user {user_id} ---")
        try:
            # DEFENSIVE CHECK: Ensure user_storage and file_list exist and are valid
            if user_id not in user_storage or \
               not isinstance(user_storage.get(user_id), dict) or \
               "file_list" not in user_storage[user_id] or \
               not isinstance(user_storage[user_id]["file_list"], list):

                logger.warning(f"[{user_id}] display_results: User storage or file_list not ready or invalid. Rendering empty.")
                ui.label("Lade Ergebnisse...")
                return

            file_list_snapshot = user_storage[user_id]["file_list"]
            logger.info(f"[{user_id}] display_results: Processing {len(file_list_snapshot)} items.")

            any_file_ready = False
            # Add try/except around sorting
            try:
                sorted_items = sorted(file_list_snapshot, key=lambda x: (
                    x[2] if len(x) > 2 and isinstance(x[2], (int, float)) else -2,  # put errors/invalid items first
                    -x[4] if len(x) > 4 and isinstance(x[4], (int, float)) else 0,
                    x[0] if len(x) > 0 else ""
                ))
            except Exception as sort_err:
                logger.error(f"[{user_id}] display_results: Error sorting items: {sort_err}")
                sorted_items = file_list_snapshot  # Fallback

            for file_status in sorted_items:
                # Add validation
                if not isinstance(file_status, list) or len(file_status) < 3:
                    logger.warning(f"[{user_id}] display_results: Malformed file_status, skipping: {file_status}")
                    continue

                # Check for updates safely
                current_update = user_storage[user_id].get("updates")
                if current_update and isinstance(current_update, list) and len(current_update) > 0 and current_update[0] == file_status[0]:
                    file_status_display = current_update
                else:
                    file_status_display = file_status

                # Extract status code safely
                status_code = file_status_display[2] if len(file_status_display) > 2 and isinstance(file_status_display[2], (int, float)) else -2

                if status_code >= 100.0:
                    try:
                        any_file_ready = True
                        ui.markdown(f"<b>{file_status_display[0].replace('_', BACKSLASHCHAR + '_')}</b>")
                        with ui.row():
                            ui.button(
                                "Editor herunterladen (Lokal)",
                                on_click=lambda f=file_status_display[0], u=user_id: asyncio.create_task(download_editor(f, u)),
                            ).props("no-caps")
                            ui.button(
                                "Editor öffnen (Server)",
                                on_click=lambda f=file_status_display[0], u=user_id: asyncio.create_task(open_editor(f, u)),
                            ).props("no-caps")
                            ui.button(
                                "SRT-Datei",
                                on_click=lambda f=file_status_display[0], u=user_id: asyncio.create_task(download_srt(f, u)),
                            ).props("no-caps")
                            ui.button(
                                "Datei entfernen",
                                on_click=lambda f=file_status_display[0], u=user_id, r=refresh_file_view: asyncio.create_task(delete_file(f, u, r)),
                                color="red-5",
                            ).props("no-caps")
                        
                        if SUMMARIZATION:
                            with ui.row():
                                summary_create = ui.button(
                                    "Zusammenfassung erstellen",
                                    on_click=partial(
                                        lambda f, u: ui.notify("Zusammenfassungsfunktion noch nicht implementiert"),
                                        file_name=file_status_display[0], 
                                        user_id=user_id
                                    ),
                                ).props("no-caps")
                                summary_create.disable()
                                summary_download = ui.button(
                                    "Zusammenfassung herunterladen",
                                    on_click=partial(
                                        lambda f, u: ui.notify("Zusammenfassungsfunktion noch nicht implementiert"),
                                        file_name=file_status_display[0],
                                        user_id=user_id,
                                    ),
                                ).props("no-caps")
                                summary_download.disable()

                                # Safer path concatenation
                                summary_path = join(ROOT, "data", "out", user_id, file_status_display[0] + ".htmlsummary")
                                todo_path = join(ROOT, "data", "out", user_id, file_status_display[0] + ".todosummary")
                                
                                if os.path.isfile(summary_path):
                                    summary_download.enable()
                                if not os.path.isfile(todo_path):
                                    summary_create.enable()
                                else:
                                    ui.label("in Bearbeitung")
                        ui.separator()
                    except Exception as el_err:
                        logger.exception(f"[{user_id}] display_results: Error rendering completed item {file_status_display[0]}: {el_err}")

                elif status_code == -1:
                    try:
                        status_text = file_status_display[1] if len(file_status_display) > 1 else "Transkription fehlgeschlagen"
                        ui.markdown(f"<b>{file_status_display[0].replace('_', BACKSLASHCHAR + '_')}:</b> {status_text}")
                        ui.button(
                            "Datei entfernen",
                            on_click=lambda f=file_status_display[0], u=user_id, r=refresh_file_view: asyncio.create_task(delete_file(f, u, r)),
                            color="red-5",
                        ).props("no-caps")
                        ui.separator()
                    except Exception as el_err:
                        logger.exception(f"[{user_id}] display_results: Error rendering failed item {file_status_display[0]}: {el_err}")

            if any_file_ready:
                try:
                    ui.button(
                        "Alle Dateien herunterladen",
                        on_click=lambda u=user_id: asyncio.create_task(download_all(u)),
                    ).props("no-caps")
                except Exception as btn_err:
                    logger.error(f"[{user_id}] display_results: Error creating download all button: {btn_err}")

            logger.info(f"[{user_id}] display_results: Finished rendering results items.")

        except Exception as e:
            logger.exception(f"[{user_id}] !!! TOP LEVEL ERROR INSIDE display_results !!!: {e}")
            try:
                ui.label(f"Schwerwiegender Fehler beim Anzeigen der Ergebnisse: {e}")
            except Exception:
                pass
        logger.info(f"--- display_results END for user {user_id} ---")

    async def display_files(user_id):
        logger.info(f"[{user_id}] Starting display_files function")
        try:
            await read_files(user_id)  # We can await this directly here
            logger.info(f"[{user_id}] Finished read_files, data should be ready")
            
            with ui.card().classes("border p-4").style("width: min(60vw, 700px);"):
                logger.info(f"[{user_id}] Calling display_queue from display_files")
                display_queue(user_id=user_id)
                logger.info(f"[{user_id}] Returned from display_queue call")
                
                logger.info(f"[{user_id}] Calling display_results from display_files")
                display_results(user_id=user_id)
                logger.info(f"[{user_id}] Returned from display_results call")
        except Exception as e:
            logger.exception(f"[{user_id}] !!! ERROR IN display_files !!!: {e}")
            ui.label(f"Fehler beim Anzeigen der Dateien: {e}")

    if ONLINE:
        user_id = str(app.storage.browser.get("id", ""))
    else:
        user_id = "local"

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

    await read_files(user_id)  # Properly await the async function

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
                
                # Timer for checking file progress - properly using asyncio.create_task for async listen function
                ui.timer(
                    5,  # 5-second interval to reduce overhead
                    lambda: asyncio.create_task(listen(user_id=user_id, refresh_file_view=refresh_file_view)),
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


if __name__ in {"__main__", "__mp_main__"}:
    # Create all required directories at startup
    for directory in ['data/in', 'data/out', 'data/worker', 'data/error']:
        os.makedirs(join(ROOT, directory), exist_ok=True)
    
    if ONLINE:
        ui.run(
            port=8080,
            title="TranscriboZH",
            storage_secret=STORAGE_SECRET,
            favicon=join(ROOT, "data", "logo.png"),
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
        )
