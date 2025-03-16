import os
import torch
import pandas as pd
import time
import subprocess
import numpy as np
import whisperx
from whisperx.audio import SAMPLE_RATE, log_mel_spectrogram, N_SAMPLES
from dataclasses import replace

# Keep transformers import as it's needed for PyAnnote
from transformers import pipeline

def custom_ffmpeg_read(file_path, sampling_rate):
    """Read audio file using ffmpeg with video stream removal."""
    ffmpeg_command = [
        "ffmpeg", "-i", file_path,
        "-vn",  # Explicitly ignore video streams
        "-ac", "1", "-ar", str(sampling_rate),
        "-f", "f32le", "-hide_banner", "-loglevel", "quiet",
        "pipe:1"
    ]
    
    try:
        with subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            stdout, stderr = proc.communicate()
            if proc.returncode != 0:
                raise ValueError(f"FFmpeg error: {stderr.decode('utf-8')}")
            
            # Convert to numpy array
            audio = np.frombuffer(stdout, dtype=np.float32)
            if audio.shape[0] == 0:
                raise ValueError("No audio stream found in file or file is not a valid audio format")
            # Return a copy to ensure the array is writable (fixes PyTorch tensor conversion warnings)
            return audio.copy()
    except Exception as e:
        raise ValueError(f"Error reading audio file: {str(e)}")


from data.const import data_leaks

DEVICE = os.getenv("DEVICE")


def get_prompt(self, tokenizer, previous_tokens, without_timestamps, prefix):
    prompt = []

    if previous_tokens or prefix:
        prompt.append(tokenizer.sot_prev)
        if prefix:
            hotwords_tokens = tokenizer.encode(" " + prefix.strip())
            if len(hotwords_tokens) >= self.max_length // 2:
                hotwords_tokens = hotwords_tokens[: self.max_length // 2 - 1]
            prompt.extend(hotwords_tokens)
        if prefix and previous_tokens:
            prompt.extend(previous_tokens[-(self.max_length // 2 - 1) :])

    prompt.extend(tokenizer.sot_sequence)

    if without_timestamps:
        prompt.append(tokenizer.no_timestamps)

    return prompt


def detect_language(audio, model):
    model_n_mels = model.model.feat_kwargs.get("feature_size")
    segment = log_mel_spectrogram(
        audio[:N_SAMPLES],
        n_mels=model_n_mels if model_n_mels is not None else 80,
        padding=0 if audio.shape[0] >= N_SAMPLES else N_SAMPLES - audio.shape[0],
    )
    encoder_output = model.model.encode(segment)
    results = model.model.model.detect_language(encoder_output)
    language_token, language_probability = results[0][0]
    language = language_token[2:-2]
    return (language, language_probability)


def transcribe(
    audio,
    pipeline,
    diarize_model,
    device,
    num_speaker,
    add_language=False,
    hotwords=[],
    batch_size=4,
    multi_mode_track=None,
    language="de",
    model=None,  # Add model parameter with default None
):
    torch.cuda.empty_cache()

    # Define sample rate for audio processing
    SAMPLE_RATE = 16000  # Standard for whisper models
    
    # Convert audio given a file path.
    #audio = whisperx.load_audio(complete_name)

    start_time = time.time()

    if len(hotwords) > 0:
        model.options = replace(model.options, prefix=" ".join(hotwords))
    print("Transcribing...")
    print(audio)
    if DEVICE == "mps":
        import mlx_whisper

        decode_options = {"language": None, "prefix": " ".join(hotwords)}

        result1 = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo="mlx-community/whisper-large-v3-mlx",
            **decode_options,
        )
    else:
        try:
            print(f"Processing audio using custom ffmpeg_read with -vn flag support...")
            # First convert audio file using our custom function for MP4 support
            audio_array = custom_ffmpeg_read(audio, SAMPLE_RATE)
            print(f"Audio processed successfully, shape: {audio_array.shape}")
            
            # Then use WhisperX with the pre-processed audio
            print(f"Running WhisperX transcription with audio_array...")
            result1 = model.transcribe(audio_array, batch_size=batch_size, language=language)
            print(f"WhisperX transcription completed successfully")
        except Exception as e:
            print(f"Error during transcription: {str(e)}")
            raise

    print(f"Transcription took {time.time() - start_time:.2f} seconds.")
    if len(hotwords) > 0:
        model.options = replace(model.options, prefix=None)

    # Align whisper output.
    try:
        print(f"Loading alignment model for language: {result1['language']}...")
        model_a, metadata = whisperx.load_align_model(language_code=result1["language"], device=device)
        start_aligning = time.time()

        print("Aligning transcription with audio...")
        result2 = whisperx.align(
            result1["segments"],
            model_a,
            metadata,
            audio_array,
            device,
            return_char_alignments=False,
        )
        print(f"Alignment completed successfully")
    except Exception as e:
        print(f"Error during alignment: {str(e)}")
        # Fallback to use the result1 format directly to avoid breaking the pipeline
        result2 = {"segments": result1["segments"]}

    print(f"Alignment took {time.time() - start_aligning:.2f} seconds.")

    if add_language:
        start_language = time.time()
        print("Adding language...")
        for segment in result2["segments"]:
            start = max(0, (int(segment["start"]) * 16_000) - 8_000)
            end = min(len(audio_array), ((int(segment["end"]) + 1) * 16_000) + 8_000)
            segment_audio = audio_array[start:end]
            if DEVICE == "mps":
                ## This is a workaround to use the whisper model in mps, it doesn't have "detect language" method
                decode_options = {"language": None, "prefix": " ".join(hotwords)}
                language = mlx_whisper.transcribe(
                    segment_audio, path_or_hf_repo="mlx-community/whisper-large-v3-mlx", **decode_options
                )
                segment["language"] = language["language"]
            else:
                detected_language, language_probability = detect_language(segment_audio, model)
                segment["language"] = detected_language if language_probability > 0.85 else language
        print(f"Adding language took {time.time() - start_language:.2f} seconds.")

    # Diarize and assign speaker labels.
    start_diarize = time.time()
    print("Diarizing...")
    
    try:
        # Use the audio_array from our custom processing
        audio_data = {
            "waveform": torch.from_numpy(audio_array[None, :]),
            "sample_rate": SAMPLE_RATE,
        }

        if multi_mode_track is None:
            print(f"Running speaker diarization...")
            segments = diarize_model(audio_data, num_speakers=num_speaker)

            diarize_df = pd.DataFrame(segments.itertracks(yield_label=True), columns=["segment", "label", "speaker"])
            diarize_df["start"] = diarize_df["segment"].apply(lambda x: x.start)
            diarize_df["end"] = diarize_df["segment"].apply(lambda x: x.end)
            
            print(f"Assigning speakers to words...")
            result3 = whisperx.assign_word_speakers(diarize_df, result2)
            print(f"Speaker diarization completed successfully")
        else:
            for segment in result2["segments"]:
                segment["speaker"] = "SPEAKER_" + str(multi_mode_track).zfill(2)
            result3 = result2
    except Exception as e:
        print(f"Error during diarization: {str(e)}")
        # Fallback to simple structure if diarization fails
        result3 = result2

    print(f"Diarization took {time.time() - start_diarize:.2f} seconds.")
    print(f"Total time: {time.time() - start_time:.2f} seconds.")
    torch.cuda.empty_cache()
    if DEVICE == "mps":
        torch.mps.empty_cache()
    # Text cleanup.
    cleaned_segments = []
    for segment in result3["segments"]:
        if result1["language"] in data_leaks:
            for line in data_leaks[result1["language"]]:
                if line in segment["text"]:
                    segment["text"] = segment["text"].replace(line, "")
        segment["text"] = segment["text"].strip()

        if len(segment["text"]) > 0:
            cleaned_segments.append(segment)

    return cleaned_segments
