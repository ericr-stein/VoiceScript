import os
import time
import shutil
import zipfile
import datetime
import base64
import logging
from os import listdir
from os.path import isfile, join, normpath, basename, dirname
from functools import partial
from dotenv import load_dotenv
from nicegui import ui, events, app

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
# Provide fallbacks for ROOT if it's not set
if ROOT is None:
    # Try to determine the root directory automatically
    if os.path.exists(os.path.join(os.path.dirname(__file__), "data", "logo.png")):
        # If executed directly
        ROOT = os.path.dirname(__file__)
    elif os.path.exists("data/logo.png"):
        # If executed from project directory
        ROOT = os.getcwd()
    else:
        # Final fallback - just use current directory
        ROOT = os.getcwd()
        print(f"WARNING: ROOT environment variable not set. Using {ROOT} as fallback.")

WINDOWS = os.getenv("WINDOWS") == "True"
SSL_CERTFILE = os.getenv("SSL_CERTFILE") 
SSL_KEYFILE = os.getenv("SSL_KEYFILE")

if WINDOWS:
    os.environ["PATH"] += os.pathsep + "ffmpeg/bin"
    os.environ["PATH"] += os.pathsep + "ffmpeg"

BACKSLASHCHAR = "\\"
user_storage = {}


def get_global_processing_queue():
    """Get all files currently in the processing queue across all users."""
    all_queued_files = []
    
    # Find all incomplete files across all users
    for u_id in user_storage:
        for file_status in user_storage[u_id].get("file_list", []):
            if 0 <= file_status[2] < 100.0:  # Incomplete files
                all_queued_files.append({
                    "user_id": u_id,
                    "filename": file_status[0],
                    "estimated_time": file_status[3],
                    "upload_time": file_status[4],
                    "progress": file_status[2]
                })
    
    # Sort by upload time (oldest first) to match worker's processing order
    all_queued_files.sort(key=lambda x: x["upload_time"])
    return all_queued_files


def read_files(user_id):
    """Read in all files of the user and set the file status if known."""
    user_storage[user_id]["file_list"] = []
    in_path = join(ROOT, "data", "in", user_id)
    out_path = join(ROOT, "data", "out", user_id)
    error_path = join(ROOT, "data", "error", user_id)

    if os.path.exists(in_path):
        for f in listdir(in_path):
            if isfile(join(in_path, f)) and f != "hotwords.txt" and f != "language.txt":
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

        # Get the global processing queue
        global_queue = get_global_processing_queue()
        queue_size = len(global_queue)
        
        # Update each file's wait time and queue position based on its position in the global queue
        for file_status in user_storage[user_id]["file_list"]:
            if file_status[2] < 100.0:  # Only for incomplete files
                wait_time = 0
                queue_position = 0
                file_found = False
                
                # Calculate position in queue and sum up processing times for all files ahead
                for i, queued_file in enumerate(global_queue):
                    if queued_file["user_id"] == user_id and queued_file["filename"] == file_status[0]:
                        queue_position = i + 1  # Position is 1-based for user display
                        file_found = True
                        # For the file itself, only count remaining time based on progress
                        if queued_file["progress"] > 0:
                            remaining_ratio = 1.0 - (queued_file["progress"] / 100.0)
                            wait_time += queued_file["estimated_time"] * remaining_ratio
                        else:
                            wait_time += queued_file["estimated_time"]
                        break
                    else:
                        # For files ahead in queue, count full estimated time
                        wait_time += queued_file["estimated_time"]
                        
                # Format and update the wait time and queue position display
                wait_time_str = str(datetime.timedelta(seconds=round(wait_time)))
                
                # Different message for actively processed file (first in queue) vs waiting
                if queue_position == 1 and file_found:
                    file_status[1] = f"Wird verarbeitet. Geschätzte Restzeit: {wait_time_str}"
                else:
                    file_status[1] = f"Position {queue_position} von {queue_size} in Warteschlange. Geschätzte Wartezeit: {wait_time_str}"

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
        # Prepare the final HTML file
        prepare_download(file_name, user_id)
        final_file_name = join(ROOT, "data", "out", user_id, file_name + ".htmlfinal")
        
        # Verify the file exists and has content
        if not os.path.exists(final_file_name):
            ui.notify(f"Error: File not found for download: {final_file_name}", color="negative")
            return
            
        file_size = os.path.getsize(final_file_name)
        if file_size == 0:
            ui.notify("Error: Generated file is empty", color="negative")
            return
            
        # Use direct src parameter instead of content - more reliable approach for this app
        download_filename = f"{os.path.splitext(file_name)[0]}.html"
        ui.download(
            src=final_file_name,  # Direct source path instead of loading content
            filename=download_filename
        )
        
        # Success notification
        ui.notify(f"Download started: {download_filename}", color="positive")
    except Exception as e:
        # Handle any unexpected errors
        ui.notify(f"Download error: {str(e)}", color="negative")


async def download_srt(file_name, user_id):
    """Simplified download function for SRT files using direct src parameter."""
    try:
        srt_file = join(ROOT, "data", "out", user_id, file_name + ".srt")
        
        # Verify the file exists
        if not os.path.exists(srt_file):
            ui.notify(f"Error: SRT file not found: {file_name}.srt", color="negative")
            return
            
        file_size = os.path.getsize(srt_file)
        if file_size == 0:
            ui.notify("Error: SRT file is empty", color="negative")
            return
            
        # Use direct src parameter instead of content - more reliable approach for this app
        download_filename = f"{os.path.splitext(file_name)[0]}.srt"
        ui.download(
            src=srt_file,  # Direct source path instead of loading content
            filename=download_filename
        )
        
        # Success notification
        ui.notify(f"Download started: {download_filename}", color="positive")
    except Exception as e:
        # Handle any unexpected errors
        ui.notify(f"Download error: {str(e)}", color="negative")


# We will use NiceGUI's built-in static file serving instead of a custom endpoint


async def open_editor(file_name, user_id):
    out_user_dir = join(ROOT, "data", "out", user_id)
    full_file_name = join(out_user_dir, file_name + ".html")
    with open(full_file_name, "r", encoding="utf-8") as f:
        content = f.read()

    # Use the static file serving path instead of secure endpoint
    video_path = f"/media/{user_id}/{file_name}.mp4"
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
    """Simplified download function for all files using direct src parameter."""
    try:
        zip_file_path = join(ROOT, "data", "out", user_id, "transcribed_files.zip")
        
        # Create a new zip file
        with zipfile.ZipFile(zip_file_path, "w", allowZip64=True) as myzip:
            count = 0
            for file_status in user_storage[user_id]["file_list"]:
                if file_status[2] == 100.0:
                    # Prepare each file
                    prepare_download(file_status[0], user_id)
                    final_html = join(ROOT, "data", "out", user_id, file_status[0] + ".htmlfinal")
                    
                    # Verify the file exists before adding it
                    if os.path.exists(final_html):
                        myzip.write(final_html, arcname=file_status[0] + ".html")
                        count += 1
                    else:
                        ui.notify(f"Warning: Could not find {file_status[0]}.htmlfinal", color="warning")
        
        # Check if we actually added any files
        if count == 0:
            ui.notify("No files were added to the zip archive", color="warning")
            if os.path.exists(zip_file_path):
                os.remove(zip_file_path)
            return
            
        # Check if the zip file was created correctly
        if not os.path.exists(zip_file_path) or os.path.getsize(zip_file_path) == 0:
            ui.notify("Error creating zip file", color="negative")
            return
            
        # Use direct src parameter instead of content - more reliable approach for this app
        ui.download(
            src=zip_file_path,  # Direct source path instead of loading content
            filename="transcribed_files.zip"
        )
        
        # Success notification
        ui.notify(f"Download started: transcribed_files.zip with {count} files", color="positive")
        
        # Clean up the temp file after a delay to ensure download completes
        # We'll add a small delay to ensure the file is fully downloaded before cleaning up
        await ui.run_javascript("setTimeout(() => {}, 5000)")  # 5 second delay
        if os.path.exists(zip_file_path):
            os.remove(zip_file_path)
            
    except Exception as e:
        # Handle any unexpected errors
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
                logging.info(f"Deleted file: {path}")
            except Exception as e:
                logging.error(f"Failed to delete {path}: {str(e)}")

    # Delete worker progress files that might be related to this file
    worker_user_dir = join(ROOT, "data", "worker", user_id)
    if os.path.exists(worker_user_dir):
        for f in os.listdir(worker_user_dir):
            if f.endswith(f"_{file_name}"):
                try:
                    os.remove(join(worker_user_dir, f))
                    logging.info(f"Deleted worker file: {join(worker_user_dir, f)}")
                except Exception as e:
                    logging.error(f"Failed to delete worker file {join(worker_user_dir, f)}: {str(e)}")

    refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=True)
    ui.notify(f"Datei '{file_name}' wurde entfernt")


def listen(user_id, refresh_file_view):
    """Periodically check if a file is being transcribed and calculate its estimated progress."""
    worker_user_dir = join(ROOT, "data", "worker", user_id)
    
    if os.path.exists(worker_user_dir):
        worker_files = listdir(worker_user_dir)
        
        for f in worker_files:
            file_path = join(worker_user_dir, f)
            
            if isfile(file_path):
                parts = f.split("_")
                if len(parts) < 3:
                    continue
                    
                estimated_time = float(parts[0])
                start = float(parts[1])
                file_name = "_".join(parts[2:])
                progress = min(0.975, (time.time() - start) / estimated_time)
                estimated_time_left = round(max(1, estimated_time - (time.time() - start)))

                in_file = join(ROOT, "data", "in", user_id, file_name)
                if os.path.exists(in_file):
                    # Show different message for post-processing phase vs normal transcription
                    if progress > 0.95:
                        status_message = "Datei wird nachbearbeitet... (SRT-Datei wird erzeugt, Editor wird erstellt)"
                    else:
                        status_message = f"Datei wird transkribiert. Geschätzte Bearbeitungszeit: {datetime.timedelta(seconds=estimated_time_left)}"
                    
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
                    os.remove(file_path)
                    
                refresh_file_view(
                    user_id=user_id,
                    refresh_queue=True,
                    refresh_results=(user_storage[user_id].get("file_in_progress") != file_name),
                )
                user_storage[user_id]["file_in_progress"] = file_name
                return

        # No files being processed
        if user_storage[user_id].get("updates"):
            user_storage[user_id]["updates"] = []
            user_storage[user_id]["file_in_progress"] = None
            refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=True)
        else:
            refresh_file_view(user_id=user_id, refresh_queue=True, refresh_results=False)


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

    # Get current user ID from browser storage
    user_id = str(app.storage.browser.get("id", "local")) if ONLINE else "local"

    # We don't use app.add_media_files anymore - instead we use the secure endpoint
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

        # Add download function script
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
        
        # Video sync is now directly integrated in viewer.py
    else:
        ui.label("Session abgelaufen. Bitte öffne den Editor erneut.")


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
            ui.linear_progress(value=file_status[2] / 100, show_value=False, size="10px").props("instant-feedback")
            
            # Add delete button for queue entries
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
            ui.separator()
        elif file_status[2] == -1:
            # Error files
            ui.markdown(f"<b>{file_status[0].replace('_', BACKSLASHCHAR + '_')}</b>")
            ui.markdown(f"**Fehler**: {file_status[1]}")
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
            
    # Add download all button if any files are ready
    if any_file_ready:
        ui.button(
            "Alle Dateien herunterladen",
            on_click=partial(download_all, user_id=user_id),
        ).props("no-caps")


@ui.page("/")
def main_page():
    """Main page for file upload and transcription."""
    # Get current user ID from browser storage
    user_id = str(app.storage.browser.get("id", "local")) if ONLINE else "local"
    
    # Initialize storage if needed
    if user_id not in user_storage:
        user_storage[user_id] = {"file_list": [], "known_errors": set()}
    
    # Add logo and header
    with ui.row().classes("items-center"):
        ui.image("data/logo.png").style("max-width: 200px")
        ui.label("Audio Transkription").classes("text-h4 q-ml-md")
    
    ui.separator()
    
    # Create tabs for different sections
    with ui.tabs().classes("w-full") as tabs:
        ui.tab("upload", "Datei hochladen")
        ui.tab("files", "Dateien")
        ui.tab("settings", "Einstellungen")
        ui.tab("help", "Hilfe")
        
    with ui.tab_panels(tabs, value="upload").classes("w-full"):
        with ui.tab_panel("upload"):
            ui.label("Bitte wählen Sie eine Audio- oder Videodatei zum Hochladen aus:")
            upload = ui.upload(
                label="Datei auswählen",
                auto_upload=True,
                max_files=1,
                max_file_size=12*1024*1024*1024,  # 12GB max
                on_upload=lambda e: handle_upload(e, user_id),
                on_rejected=handle_reject,
                on_added=lambda e: handle_added(e, user_id, upload, refresh_file_view)
            ).props("accept=audio/*,video/*")
            
            ui.button(
                "Ausgewählte Datei hochladen", 
                on_click=lambda: upload.upload()
            ).props("no-caps")
            
        with ui.tab_panel("files"):
            ui.label("Dateien in Bearbeitung:")
            display_queue(user_id=user_id)
            
            ui.label("Fertige Dateien:")
            display_results(user_id=user_id)
            
            # Setup periodic refresh
            ui.timer(5.0, lambda: listen(user_id, refresh_file_view))
            
        with ui.tab_panel("settings"):
            ui.label("Sprache:")
            user_storage[user_id]["language"] = ui.select(
                options=[(k, v) for k, v in LANGUAGES.items()],
                value=LANGUAGES.get(app.storage.user.get(f"{user_id}_language", "de"), LANGUAGES["de"])
            ).props("outlined")
            
            ui.button(
                "Sprache speichern", 
                on_click=lambda: update_language(user_id)
            ).props("no-caps")
            
            ui.label("Vokabular (Wörter, Namen oder Begriffe, die häufig in der Aufnahme vorkommen):")
            user_storage[user_id]["textarea"] = ui.textarea(
                value=app.storage.user.get(f"{user_id}_vocab", ""),
                placeholder="Namen und spezifische Begriffe, die im Text vorkommen (ein Begriff pro Zeile)"
            ).classes("w-full").props("outlined")
            
            ui.button(
                "Vokabular speichern", 
                on_click=lambda: update_hotwords(user_id)
            ).props("no-caps")
            
        with ui.tab_panel("help"):
            help_page()


# Configure static file serving for media files
app.add_static_files("/media", join(ROOT, "data", "out"))

# Setup secure storage
if STORAGE_SECRET:
    app.storage.user.use_secure_cookies = True
    app.storage.user.secret_key = STORAGE_SECRET
    app.storage.browser.use_secure_cookies = True 
    app.storage.browser.secret_key = STORAGE_SECRET

# Run the app with SSL if configured
if SSL_CERTFILE and SSL_KEYFILE:
    ui.run(ssl_certfile=SSL_CERTFILE, ssl_keyfile=SSL_KEYFILE)
else:
    ui.run()
