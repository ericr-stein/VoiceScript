import os
import shutil
import time
import fnmatch
import types
import ffmpeg
import torch
import zipfile
import logging
import whisperx

from os.path import isfile, join, normpath, basename, dirname
from dotenv import load_dotenv
from pyannote.audio import Pipeline
from src.metrics import (
    initialize_metrics, track_file_processed, track_queue_size,
    track_transcription_error, track_audio_duration, time_transcription
)

from src.viewer import create_viewer
from src.srt import create_srt
from src.transcription import transcribe, get_prompt
from src.util import time_estimate, isolate_voices

# Load model directly
from transformers import AutoProcessor, AutoModelForSpeechSeq2Seq, pipeline

# Load environment variables
load_dotenv()

# Configuration
ONLINE = os.getenv("ONLINE") == "True"
DEVICE = os.getenv("DEVICE")
ROOT = os.getenv("ROOT")
WINDOWS = os.getenv("WINDOWS") == "True"
BATCH_SIZE = int(os.getenv("BATCH_SIZE"))

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if WINDOWS:
    os.environ["PATH"] += os.pathsep + "ffmpeg/bin"
    os.environ["PATH"] += os.pathsep + "ffmpeg"
    os.environ["PYANNOTE_CACHE"] = join(ROOT, "models")
    os.environ["HF_HOME"] = join(ROOT, "models")


def mark_file_as_processing(file_path):
    """Create a simple state file to indicate a file is being processed."""
    state_file = file_path + ".processing"
    try:
        with open(state_file, "w") as f:
            f.write(str(int(time.time())))
        logger.info(f"Marked file as processing: {file_path}")
        return state_file
    except Exception as e:
        logger.error(f"Failed to mark file as processing: {file_path}, error: {str(e)}")
        return None

def should_process_file(file_path):
    """Check if a file should be processed based on state files."""
    # Get file info
    file = basename(file_path)
    user_id = normpath(dirname(file_path)).split(os.sep)[-1]
    
    # Skip if already successfully processed
    file_name_viewer = join(ROOT, "data", "out", user_id, file + ".html")
    if isfile(file_name_viewer):
        logger.debug(f"Skipping already processed file: {file_path}")
        return False
        
    # Skip if currently processing or previously failed
    processing_marker = file_path + ".processing"
    if os.path.exists(processing_marker):
        # Check if processing for too long (stuck)
        try:
            with open(processing_marker, "r") as f:
                start_time = int(f.read().strip())
                # If processing for more than 10 minutes, consider it failed
                if time.time() - start_time > 600:
                    logger.warning(f"File processing stuck for >10 min, marking as failed: {file_path}")
                    # Move to error state
                    report_error(
                        file_path,
                        join(ROOT, "data", "error", user_id, file),
                        user_id,
                        "Verarbeitung fehlgeschlagen oder steckengeblieben"
                    )
                else:
                    logger.debug(f"File currently being processed, skipping: {file_path}")
            return False
        except Exception as e:
            logger.error(f"Invalid processing state file for {file_path}, error: {str(e)}")
            try:
                # Clean up bad marker file
                os.remove(processing_marker)
                logger.info(f"Removed invalid processing marker for: {file_path}")
            except Exception as clean_e:
                logger.error(f"Failed to clean up invalid marker: {str(clean_e)}")
            # We'll let it be processed again
            
    return True

def report_error(file_name, file_name_error, user_id, text=""):
    """Report an error and move file to error directory with improved reliability."""
    logger.error(f"Error processing file {file_name}: {text}")
    error_dir = join(ROOT, "data", "error", user_id)
    os.makedirs(error_dir, exist_ok=True)
    
    # First ensure we can create the error text file
    error_file = file_name_error + ".txt"
    try:
        with open(error_file, "w") as f:
            f.write(text)
        logger.info(f"Created error file: {error_file}")
    except Exception as e:
        logger.error(f"Failed to create error file {error_file}: {str(e)}")
        
    # Then try to move the file, using copy+delete if move fails
    try:
        shutil.move(file_name, file_name_error)
        logger.info(f"Moved file to error directory: {file_name} -> {file_name_error}")
    except Exception as e:
        logger.error(f"Could not move file to error directory: {str(e)}")
        try:
            # Try copy+delete as fallback
            shutil.copy2(file_name, file_name_error)
            logger.info(f"Copied file to error directory: {file_name} -> {file_name_error}")
            os.remove(file_name)
            logger.info(f"Removed original file after copy: {file_name}")
        except Exception as e2:
            logger.error(f"Failed fallback file handling: {str(e2)}")
            
    # Always clean up the processing marker
    try:
        processing_marker = file_name + ".processing"
        if os.path.exists(processing_marker):
            os.remove(processing_marker)
            logger.info(f"Removed processing marker: {processing_marker}")
    except Exception as e:
        logger.error(f"Could not remove processing marker: {str(e)}")


def oldest_files(folder):
    matches = []
    times = []
    for root, _, filenames in os.walk(folder):
        for filename in fnmatch.filter(filenames, "*.*"):
            file_path = join(root, filename)
            matches.append(file_path)
            times.append(os.path.getmtime(file_path))
    return [m for _, m in sorted(zip(times, matches))]


@time_transcription
def transcribe_file(file_name, multi_mode=False, multi_mode_track=None, audio_files=None, language="de"):
    data = None
    estimated_time = 0
    progress_file_name = ""

    # First check if the file still exists - return early if not
    if not os.path.exists(file_name):
        logger.info(f"File no longer exists, cancelling processing: {file_name}")
        return None, estimated_time, progress_file_name

    file = basename(file_name)
    user_id = normpath(dirname(file_name)).split(os.sep)[-1]
    file_name_error = join(ROOT, "data", "error", user_id, file)
    file_name_out = join(ROOT, "data", "out", user_id, file + ".mp4")

    # Clean up worker directory
    if not multi_mode:
        worker_user_dir = join(ROOT, "data", "worker", user_id)
        if os.path.exists(worker_user_dir):
            try:
                shutil.rmtree(worker_user_dir)
            except OSError as e:
                logger.error(f"Could not remove folder: {worker_user_dir}. Error: {e}")

    # Create output directory
    if not multi_mode:
        output_user_dir = join(ROOT, "data", "out", user_id)
        os.makedirs(output_user_dir, exist_ok=True)

    # Estimate run time
    try:
        time.sleep(2)
        estimated_time, run_time = time_estimate(file_name, ONLINE)
        logger.info(f"DEBUG: Estimated transcription time: {estimated_time} seconds for file {file_name}")
        if run_time == -1:
            report_error(file_name, file_name_error, user_id, "Datei konnte nicht gelesen werden")
            return data, estimated_time, progress_file_name
    except Exception as e:
        logger.exception("Error estimating run time")
        report_error(file_name, file_name_error, user_id, "Datei konnte nicht gelesen werden")
        return data, estimated_time, progress_file_name

    if not multi_mode:
        worker_user_dir = join(ROOT, "data", "worker", user_id)
        os.makedirs(worker_user_dir, exist_ok=True)
        logger.info(f"DEBUG: Worker user directory created: {worker_user_dir}")
        progress_file_name = join(worker_user_dir, f"{estimated_time}_{int(time.time())}_{file}")
        try:
            with open(progress_file_name, "w") as f:
                f.write("")
            logger.info(f"DEBUG: Successfully created progress file: {progress_file_name}")
        except OSError as e:
            logger.error(f"Could not create progress file: {progress_file_name}. Error: {e}")

    # Track file being processed
    track_file_processed(file_name)
    
    # Check if file has a valid audio stream
    try:
        if not ffmpeg.probe(file_name, select_streams="a")["streams"]:
            report_error(
                file_name,
                file_name_error,
                user_id,
                "Die Tonspur der Datei konnte nicht gelesen werden",
            )
            return data, estimated_time, progress_file_name
    except ffmpeg.Error as e:
        logger.exception("ffmpeg error during probing")
        report_error(
            file_name,
            file_name_error,
            user_id,
            "Die Tonspur der Datei konnte nicht gelesen werden",
        )
        return data, estimated_time, progress_file_name

    # Process audio
    if not multi_mode:
        # Convert and filter audio
        exit_status = os.system(
            f'ffmpeg -y -i "{file_name}" -filter:v scale=320:-2 -af "lowpass=3000,highpass=200" "{file_name_out}"'
        )
        if exit_status == 256:
            exit_status = os.system(
                f'ffmpeg -y -i "{file_name}" -c:v copy -af "lowpass=3000,highpass=200" "{file_name_out}"'
            )
        if not exit_status == 0:
            logger.exception("ffmpeg error during audio processing")
            file_name_out = file_name  # Fallback to original file

    else:
        file_name_out = file_name

    # Load hotwords
    hotwords = []
    hotwords_file = join(ROOT, "data", "in", user_id, "hotwords.txt")
    if isfile(hotwords_file):
        with open(hotwords_file, "r") as h:
            hotwords = h.read().splitlines()

    # Transcribe
    try:
        data = transcribe(
            file_name_out,
            pipe,
            diarize_model,
            DEVICE,
            None,
            add_language=(
                False if DEVICE == "mps" else True
            ),  # on MPS is rather slow and unreliable, but you can try with setting this to true
            hotwords=hotwords,
            multi_mode_track=multi_mode_track,
            language=language,
            model=model,  # Pass the WhisperX model
        )
    except Exception as e:
        logger.exception("Transcription failed")
        report_error(file_name, file_name_error, user_id, "Transkription fehlgeschlagen")

    return data, estimated_time, progress_file_name
if __name__ == "__main__":
    WHISPER_DEVICE = "cpu" if DEVICE == "mps" else DEVICE
    if WHISPER_DEVICE == "cpu":
        compute_type = "float32"
    else:
        compute_type = "float16"

    torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    # Load models
    whisperx_model = (
        "tiny.en" if DEVICE == "mps" else "large-v3"
    )  # we can load a really small one for mps, because we use mlx_whisper later and only need whisperx for diarization and alignment
    if ONLINE:
        # Use WhisperX for transcription
        model = whisperx.load_model(whisperx_model, WHISPER_DEVICE, compute_type=compute_type)
        
        # Keep the transformers pipeline for backward compatibility
        model_id = os.getenv("ASR_MODEL_ID")
        pipe = pipeline(
            "automatic-speech-recognition",
            model_id,
            device=DEVICE,
            torch_dtype=torch_dtype,
        )
    else:
        # Use WhisperX for transcription (offline mode)
        model = whisperx.load_model(
            whisperx_model,
            WHISPER_DEVICE,
            compute_type=compute_type,
            download_root=join("models", "whisperx"),
        )
        
        # Keep the transformers pipeline for backward compatibility
        model_id = os.getenv("ASR_MODEL_ID")
        pipe = pipeline(
            "automatic-speech-recognition",
            model_id,
            device=DEVICE,
            torch_dtype=torch_dtype,
        )

    model.model.get_prompt = types.MethodType(get_prompt, model.model)
    
    # Check for valid Hugging Face token
    hf_token = os.getenv("HF_AUTH_TOKEN")
    if not hf_token or hf_token == "hf_putyourtokenhere" or hf_token == "hf_YOUR_ACTUAL_TOKEN":
        logger.error("""
        ===================================================================
        ERROR: Missing or invalid Hugging Face authentication token!
        
        The pyannote/speaker-diarization model requires a valid Hugging Face
        token that has accepted the model's license agreement.
        
        Please follow these steps:
        1. Create/login to your Hugging Face account at https://huggingface.co/
        2. Go to Settings → Access Tokens → New token
        3. Create a token with at least read access
        4. Visit https://huggingface.co/pyannote/speaker-diarization 
           and accept the user agreement for this model
        5. Update your .env file with: HF_AUTH_TOKEN = "your_actual_token"
        ===================================================================
        """)
        raise ValueError("Invalid Hugging Face token. Please check your .env file and update HF_AUTH_TOKEN.")
    
    try:
        diarize_model = Pipeline.from_pretrained(
            "pyannote/speaker-diarization", use_auth_token=hf_token
        ).to(torch.device(DEVICE))
    except Exception as e:
        if "401 Client Error: Unauthorized" in str(e):
            logger.error("""
            ===================================================================
            ERROR: Hugging Face authentication failed!
            
            Your token was rejected by Hugging Face. This could mean:
            1. The token is invalid or expired
            2. You haven't accepted the model's license agreement
            
            Please visit https://huggingface.co/pyannote/speaker-diarization
            and make sure you're logged in and have accepted the user agreement.
            Then check that your token in .env is correct and up to date.
            ===================================================================
            """)
        raise

    # Create necessary directories
    for directory in ["data/in/", "data/out/", "data/error/", "data/worker/"]:
        os.makedirs(join(ROOT, directory), exist_ok=True)
    
    # Initialize Prometheus metrics
    initialize_metrics(port=8000)

    disclaimer = (
        "This transcription software (the Software) incorporates the open-source model Whisper Large v3 "
        "(the Model) and has been developed according to and with the intent to be used under Swiss law. "
        "Please be aware that the EU Artificial Intelligence Act (EU AI Act) may, under certain circumstances, "
        "be applicable to your use of the Software. You are solely responsible for ensuring that your use of "
        "the Software as well as of the underlying Model complies with all applicable local, national and "
        "international laws and regulations. By using this Software, you acknowledge and agree (a) that it is "
        "your responsibility to assess which laws and regulations, in particular regarding the use of AI "
        "technologies, are applicable to your intended use and to comply therewith, and (b) that you will hold "
        "us harmless from any action, claims, liability or loss in respect of your use of the Software."
    )
    logger.info(disclaimer)
    logger.info("Worker ready")

    while True:
        try:
            # Get all files in input directory
            files_sorted_by_date = oldest_files(join(ROOT, "data", "in"))
            
            # Filter out non-processable files to get accurate queue size
            actual_queue = []
            for file_path in files_sorted_by_date:
                file = basename(file_path)
                user_id = normpath(dirname(file_path)).split(os.sep)[-1]
                
                # Skip config files
                if file == "hotwords.txt" or file == "language.txt":
                    continue
                    
                # Skip files that should not be processed (already processed, currently processing, etc.)
                if not isfile(file_path) or not should_process_file(file_path):
                    continue
                    
                # This file is in the queue
                actual_queue.append(file_path)
            
            logger.info(f"Found {len(actual_queue)} files in queue")
            
            # Track correct queue size for monitoring
            track_queue_size(len(actual_queue))
            
            # Process files from the filtered queue
            if not actual_queue:
                time.sleep(1)
                continue
                
            # Process the oldest file
            file_name = actual_queue[0]
            file = basename(file_name)
            user_id = normpath(dirname(file_name)).split(os.sep)[-1]
            
            # Mark file as processing to prevent reprocessing
            logger.info(f"Starting to process file: {file_name}")
            processing_marker = mark_file_as_processing(file_name)
            
            language_file = join(ROOT, "data", "in", user_id, "language.txt")
            if isfile(language_file):
                with open(language_file, "r") as h:
                    language = h.read()
            else:
                language = "de"

            # Check if it's a zip file
            if file_name.lower().endswith(".zip"):
                try:
                    zip_extract_dir = join(ROOT, "data", "worker", "zip")
                    shutil.rmtree(zip_extract_dir, ignore_errors=True)
                    os.makedirs(zip_extract_dir, exist_ok=True)

                    with zipfile.ZipFile(file_name, "r") as zip_ref:
                        zip_ref.extractall(zip_extract_dir)

                    multi_mode = True
                    data_parts = []
                    estimated_time = 0
                    data = []
                    file_parts = []

                    # Collect files from zip
                    for root, _, filenames in os.walk(zip_extract_dir):
                        audio_files = [fn for fn in filenames if fnmatch.fnmatch(fn, "*.*")]
                        for filename in audio_files:
                            file_path = join(root, filename)
                            est_time_part, _ = time_estimate(file_path, ONLINE)
                            estimated_time += est_time_part

                    progress_file_name = join(
                        ROOT,
                        "data",
                        "worker",
                        user_id,
                        f"{estimated_time}_{int(time.time())}_{file}",
                    )
                    with open(progress_file_name, "w") as f:
                        f.write("")

                    isolate_voices([join(root, filename) for filename in audio_files])

                    # Transcribe each file
                    for track, filename in enumerate(audio_files):
                        file_path = join(root, filename)
                        file_parts.append(f'-i "{file_path}"')
                        data_part, _, _ = transcribe_file(file_path, multi_mode=True, multi_mode_track=track, language=language)
                        data_parts.append(data_part)

                    # Merge data
                    while any(data_parts):
                        earliest = min(
                            [(i, dp[0]) for i, dp in enumerate(data_parts) if dp],
                            key=lambda x: x[1]["start"],
                            default=(None, None),
                        )
                        if earliest[0] is None:
                            break

                        data.append(earliest[1])
                        data_parts[earliest[0]].pop(0)

                    # Merge audio files
                    output_audio = join(ROOT, "data", "worker", "zip", "tmp.mp4")
                    ffmpeg_input = " ".join(file_parts)
                    ffmpeg_cmd = f'ffmpeg {ffmpeg_input} -filter_complex amix=inputs={len(file_parts)}:duration=first "{output_audio}"'
                    os.system(ffmpeg_cmd)

                    # Process merged audio
                    file_name_out = join(ROOT, "data", "out", user_id, file + ".mp4")
                    exit_status = os.system(
                        f'ffmpeg -y -i "{output_audio}" -filter:v scale=320:-2 -af "lowpass=3000,highpass=200" "{file_name_out}"'
                    )
                    if exit_status == 256:
                        exit_status = os.system(
                            f'ffmpeg -y -i "{output_audio}" -c:v copy -af "lowpass=3000,highpass=200" "{file_name_out}"'
                        )
                    if not exit_status == 0:
                        logger.exception("ffmpeg error during audio processing")
                        file_name_out = output_audio  # Fallback to original file

                    shutil.rmtree(zip_extract_dir, ignore_errors=True)
                except Exception as e:
                    logger.exception("Transcription failed for zip file")
                    report_error(
                        file_name,
                        join(ROOT, "data", "error", user_id, file),
                        user_id,
                        "Transkription fehlgeschlagen",
                    )
                    continue
            else:
                # Single file transcription
                data, estimated_time, progress_file_name = transcribe_file(file_name, language=language)

            if data is None:
                continue

            # Check if the file still exists before generating outputs
            if not os.path.exists(file_name):
                logger.info(f"File was deleted during processing, cancelling output generation: {file_name}")
                if progress_file_name and os.path.exists(progress_file_name):
                    os.remove(progress_file_name)
                continue

            # Generate outputs
            try:
                file_name_out = join(ROOT, "data", "out", user_id, file + ".mp4")
                
                # Track audio duration for completed transcriptions
                if data and len(data) > 0:
                    audio_duration = data[-1].get("end", 0)  # Get duration from last segment end time
                    track_audio_duration(audio_duration)

                srt = create_srt(data)
                viewer = create_viewer(data, file_name_out, True, False, ROOT, language)

                file_name_srt = join(ROOT, "data", "out", user_id, file + ".srt")
                file_name_viewer = join(ROOT, "data", "out", user_id, file + ".html")
                with open(file_name_viewer, "w", encoding="utf-8") as f:
                    f.write(viewer)
                with open(file_name_srt, "w", encoding="utf-8") as f:
                    f.write(srt)

                logger.info(f"Estimated Time: {estimated_time}")
            except Exception as e:
                logger.exception("Error creating editor")
                report_error(
                    file_name,
                    join(ROOT, "data", "error", user_id, file),
                    user_id,
                    "Fehler beim Erstellen des Editors",
                )

            if progress_file_name and os.path.exists(progress_file_name):
                os.remove(progress_file_name)
                
            # Clean up processing marker after successful completion
            try:
                if os.path.exists(file_name + ".processing"):
                    os.remove(file_name + ".processing")
                    logger.info(f"Removed processing marker after successful transcription: {file_name}")
            except Exception as e:
                logger.error(f"Could not remove processing marker: {str(e)}")
                
            logger.info(f"Successfully processed file: {file_name}")
                
            if DEVICE == "mps":
                print("Exiting worker to prevent memory leaks with MPS...")
                exit(0)  # Due to memory leak problems, we restart the worker after each transcription

            # Process one file at a time
            time.sleep(1)
        except Exception as e:
            logger.exception("Error in main processing loop")
            time.sleep(1)
